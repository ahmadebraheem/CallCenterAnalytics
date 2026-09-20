"""Loader contract tests: publication, replay and failure handling without a live server."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('analytics_load', ROOT/'analytics/scripts/load.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    catalog = [dict(name=name, columns=[dict(name='ID', clickhouse_type='Nullable(Int64)')])
               for name in ['interaction_resource_fact', 'interaction_outcome_fact',
                            'dim_site', 'dim_lob', 'dim_vq', 'dim_agent', 'dim_customer']]
    monkeypatch.setattr(loader, 'CATALOG', catalog)
    for table in catalog:
        path = tmp_path/table['name']/'part.parquet' if table['name'].endswith('_fact') else tmp_path/(table['name']+'.parquet')
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({'ID': [1, 2]}), path)
    (tmp_path/'_manifest.json').write_text(json.dumps({'config': {'timezone': 'America/Chicago'},
                                                     'rows': 2, 'outcome_rows': 2}))
    return tmp_path


class FakeClickHouse:
    def __init__(self, previous=(), fail_table=None):
        self.previous = previous
        self.fail_table = fail_table
        self.batches = {}
        self.published = []

    def query(self, sql, parameters=None):
        if 'SELECT DISTINCT fingerprint' in sql:
            return SimpleNamespace(result_rows=self.previous)
        if sql.startswith('DESCRIBE'):
            name = sql.split('.')[-1]
            return SimpleNamespace(result_rows=loader.raw_columns(next(t for t in loader.CATALOG if t['name'] == name)))
        name = sql.split('FROM ')[1].split()[0]
        return SimpleNamespace(first_row=[sum(len(t) for t in self.batches.get(name, []))])

    def insert_arrow(self, name, data):
        if name.endswith(self.fail_table or '!'):
            raise RuntimeError('simulated network failure')
        self.batches.setdefault(name, []).append(data)

    def insert(self, table, rows, column_names):
        self.published.extend(rows)


def test_complete_load_publishes_after_seven_tables(dataset):
    ch = FakeClickHouse()
    loader.load(ch, dataset, 'demo')
    assert len(ch.batches) == 7
    assert len(ch.published) == 1
    assert set(json.loads(ch.published[0][-1]).values()) == {2}


def test_failure_never_publishes_partial_load(dataset):
    ch = FakeClickHouse(fail_table='dim_customer')
    with pytest.raises(RuntimeError):
        loader.load(ch, dataset, 'demo')
    assert ch.batches
    assert not ch.published


def test_completed_replay_is_noop_and_changed_dataset_rejected(dataset):
    fingerprint = loader.discover(dataset)[2]
    ch = FakeClickHouse(previous=[(fingerprint,)])
    loader.load(ch, dataset, 'demo')
    assert not ch.batches and not ch.published
    ch.previous = [('different',)]
    with pytest.raises(ValueError, match='different files'):
        loader.load(ch, dataset, 'demo')


def test_manifest_mismatch_fails_before_inserts(dataset):
    path = dataset/'_manifest.json'
    manifest = json.loads(path.read_text())
    manifest['rows'] = 99
    path.write_text(json.dumps(manifest))
    ch = FakeClickHouse()
    with pytest.raises(ValueError, match='Manifest row count'):
        loader.load(ch, dataset, 'demo')
    assert not ch.batches


def test_missing_dimension_rejected(dataset):
    (dataset/'dim_agent.parquet').unlink()
    with pytest.raises(ValueError, match='Missing Parquet'):
        loader.discover(dataset)


def test_local_timestamps_remain_wall_clock_strings():
    from datetime import datetime, timezone
    local = datetime(2026, 8, 1, 12, 30)
    utc = datetime(2026, 8, 1, 17, 30, tzinfo=timezone.utc)
    table = pa.table({'LOCAL': pa.array([local, None], type=pa.timestamp('ms')),
                      'UTC': pa.array([utc, None], type=pa.timestamp('ms', tz='UTC'))})
    config = {'columns': [{'name': 'LOCAL'}, {'name': 'UTC'}]}
    result = loader.prepare_batch(table.to_batches()[0], config, 'demo', uuid.uuid4(), 'file', 'America/Chicago')
    assert result['LOCAL'].to_pylist() == ['2026-08-01 12:30:00.000', None]
    assert result['UTC'].to_pylist() == [utc, None]


def test_source_catalog_matches_all_generator_columns():
    import yaml
    sources = yaml.safe_load((ROOT/'analytics/dbt/models/staging/sources.yml').read_text())['sources'][0]['tables']
    assert len(loader.CATALOG) == 7
    for table in loader.CATALOG:
        source = next(s for s in sources if s['name'] == table['name'])
        assert [c['name'] for c in source['columns']][:len(table['columns'])] == [c['name'] for c in table['columns']]
