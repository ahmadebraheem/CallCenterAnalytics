"""Checks against real generated Parquet, without requiring a database."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'analytics/scripts'))
import load as loader
import checks
from gim_synth.config import load_config
from gim_synth.generator import generate


@pytest.fixture(scope='module')
def original(tmp_path_factory):
    out = tmp_path_factory.mktemp('source')
    generate(load_config(overrides={'start_date': '2026-08-03', 'days': 2, 'calls_per_day': 100,
                                   'customers': {'pool_size': 100}, 'output': {'directory': str(out)}}))
    return out


@pytest.fixture
def dataset(original, tmp_path):
    out = tmp_path/'out'
    shutil.copytree(original, out)
    return out


def fact_path(out):
    return sorted((out/'interaction_resource_fact').rglob('*.parquet'))[0]


def change_column(path, column, transform):
    data = pq.ParquetFile(path).read()
    index = data.schema.get_field_index(column)
    values = transform(data[column].to_pylist())
    data = data.set_column(index, column, pa.array(values, type=data.schema.field(column).type))
    pq.write_table(data, path)


def test_complete_dataset_and_manifest_counts(dataset):
    result = checks.validate(dataset, loader.CATALOG)
    manifest = json.loads((dataset/'_manifest.json').read_text())
    assert len(result['tables']) == 7
    assert result['tables']['interaction_resource_fact']['rows'] == manifest['rows']
    assert result['tables']['interaction_outcome_fact']['rows'] == manifest['outcome_rows']


@pytest.mark.parametrize('table,column', [('dim_site','SITE'), ('dim_agent','AGENT_ID'),
                                        ('dim_customer','CUSTOMER_ID')])
def test_null_keys_rejected(dataset, table, column):
    change_column(dataset/(table+'.parquet'), column, lambda x: [None]+x[1:])
    with pytest.raises(ValueError, match='Null required key'):
        checks.validate(dataset, loader.CATALOG)


def test_duplicate_key_within_file(dataset):
    p=fact_path(dataset); data=pq.ParquetFile(p).read()
    pq.write_table(pa.concat_tables([data, data.slice(0,1)]), p)
    with pytest.raises(ValueError, match='Duplicate key'):
        checks.validate(dataset, loader.CATALOG)


def test_duplicate_key_across_files(dataset):
    p=fact_path(dataset); data=pq.ParquetFile(p).read().slice(0,1)
    pq.write_table(data, p.with_name('extra.parquet'))
    with pytest.raises(ValueError, match='Duplicate key'):
        checks.validate(dataset, loader.CATALOG)


def test_identical_file_copy_rejected(dataset):
    p=fact_path(dataset); shutil.copyfile(p, p.with_name('copy.parquet'))
    with pytest.raises(ValueError, match='Duplicate nonempty files'):
        checks.validate(dataset, loader.CATALOG)


def test_fingerprint_ignores_manifest_volatile_fields(dataset):
    before=loader.discover(dataset)[2]
    p=dataset/'_manifest.json'; m=json.loads(p.read_text()); m['generated_at']='later'; m['elapsed_s']=99
    p.write_text(json.dumps(m))
    assert loader.discover(dataset)[2] == before


def test_fingerprint_changes_with_content(dataset):
    before=loader.discover(dataset)[2]
    change_column(dataset/'dim_customer.parquet','ANI',lambda x:['new-number']+x[1:])
    assert loader.discover(dataset)[2] != before


def test_duplicate_call_id(dataset):
    change_column(fact_path(dataset),'CALL_ID',lambda x:[x[1]]+x[1:])
    with pytest.raises(ValueError, match='Duplicate key.*call_ids'):
        checks.validate(dataset, loader.CATALOG)


def test_invalid_timestamp_timezone(dataset):
    p=fact_path(dataset); data=pq.ParquetFile(p).read(); index=data.schema.get_field_index('ARRIVE_TIME')
    data=data.set_column(index,'ARRIVE_TIME',data['ARRIVE_TIME'].cast(pa.timestamp('ms',tz='UTC')))
    pq.write_table(data,p)
    with pytest.raises(ValueError, match='Type mismatch'):
        checks.validate(dataset, loader.CATALOG)


def test_nonfinite_amount(dataset):
    p=sorted((dataset/'interaction_outcome_fact').rglob('*.parquet'))[0]
    change_column(p,'AMOUNT',lambda x:[float('nan')]+x[1:])
    with pytest.raises(ValueError, match='Non-finite'):
        checks.validate(dataset, loader.CATALOG)


def test_extra_parquet_file(dataset):
    pq.write_table(pa.table({'extra':[1]}),dataset/'unrecognized.parquet')
    with pytest.raises(ValueError, match='Unexpected Parquet'):
        checks.validate(dataset, loader.CATALOG)


def test_empty_dataset_is_valid(dataset):
    for path in dataset.rglob('*.parquet'):
        pq.write_table(pq.ParquetFile(path).read().slice(0,0),path)
    p=dataset/'_manifest.json'; m=json.loads(p.read_text()); m['rows']=0;m['outcome_rows']=0;p.write_text(json.dumps(m))
    result=checks.validate(dataset,loader.CATALOG)
    assert all(t['rows']==0 for t in result['tables'].values())


def test_single_file_fact_layout(dataset):
    for name in ['interaction_resource_fact','interaction_outcome_fact']:
        paths=sorted((dataset/name).rglob('*.parquet'))
        data=pa.concat_tables([pq.ParquetFile(p).read() for p in paths])
        for p in paths:
            p.unlink()
        pq.write_table(data,dataset/name/'all.parquet')
    assert checks.validate(dataset,loader.CATALOG)['status']=='validated'


def test_source_changes_during_validation(dataset,monkeypatch):
    original_discover=checks.discover; calls=0
    def changing(*args):
        nonlocal calls
        calls+=1
        result=original_discover(*args)
        return result if calls==1 else (result[0],result[1],'changed')
    monkeypatch.setattr(checks,'discover',changing)
    with pytest.raises(ValueError,match='Input changed'):
        checks.validate(dataset,loader.CATALOG)


def test_manifest_mismatch(dataset):
    p=dataset/'_manifest.json'; m=json.loads(p.read_text()); m['rows']+=1; p.write_text(json.dumps(m))
    with pytest.raises(ValueError, match='Manifest row count'):
        checks.validate(dataset, loader.CATALOG)


def test_missing_dimension(dataset):
    (dataset/'dim_agent.parquet').unlink()
    with pytest.raises(ValueError, match='Missing Parquet'):
        loader.discover(dataset)


def test_type_mismatch(dataset):
    p=fact_path(dataset); data=pq.ParquetFile(p).read()
    data=data.set_column(0,'IRF_ID',pa.array(data['IRF_ID'].to_pylist(), type=pa.float64()))
    pq.write_table(data,p)
    with pytest.raises(ValueError, match='Type mismatch'):
        checks.validate(dataset, loader.CATALOG)


def test_orphan_dimension_reference(dataset):
    change_column(fact_path(dataset),'AGENT_ID',lambda x:['missing-agent']+x[1:])
    with pytest.raises(ValueError, match='Orphan reference'):
        checks.validate(dataset, loader.CATALOG)


def test_orphan_outcome_reference(dataset):
    p=sorted((dataset/'interaction_outcome_fact').rglob('*.parquet'))[0]
    change_column(p,'IRF_ID',lambda x:[-999]+x[1:])
    with pytest.raises(ValueError, match='Orphan outcome IRF_ID'):
        checks.validate(dataset, loader.CATALOG)


def test_business_duration_invariant(dataset):
    change_column(fact_path(dataset),'DURATION',lambda x:[x[0]+10]+x[1:])
    with pytest.raises(ValueError, match='DURATION'):
        checks.validate(dataset, loader.CATALOG)


def test_partition_date_mismatch(dataset):
    from datetime import timedelta
    change_column(fact_path(dataset),'CALL_DATE',lambda x:[x[0]+timedelta(days=1)]+x[1:])
    with pytest.raises(ValueError, match='Partition date mismatch'):
        checks.validate(dataset, loader.CATALOG)


def test_dry_run_never_connects(dataset, monkeypatch, tmp_path):
    monkeypatch.setattr(loader,'client',lambda: pytest.fail('dry run connected'))
    monkeypatch.setattr(sys,'argv',['load.py','load','--directory',str(dataset),'--dataset-id','x','--dry-run',
                                    '--report',str(tmp_path/'report.json')])
    assert loader.main() == 0
    assert json.loads((tmp_path/'report.json').read_text())['status'] == 'validated'


def test_local_timestamps_remain_wall_clock_strings():
    from datetime import datetime,timezone
    local=datetime(2026,8,1,12,30); utc=datetime(2026,8,1,17,30,tzinfo=timezone.utc)
    table=pa.table({'LOCAL':pa.array([local,None],type=pa.timestamp('ms')),
                    'UTC':pa.array([utc,None],type=pa.timestamp('ms',tz='UTC'))})
    result=loader.prepare_batch(table.to_batches()[0],{'columns':[{'name':'LOCAL'},{'name':'UTC'}]},
                                'demo',uuid.uuid4(),'file','America/Chicago')
    assert result['LOCAL'].to_pylist()==['2026-08-01 12:30:00.000',None]
    assert result['UTC'].to_pylist()==[utc,None]


def test_source_catalog_matches_all_columns():
    import yaml
    sources=yaml.safe_load((ROOT/'analytics/dbt/models/staging/sources.yml').read_text())['sources'][0]['tables']
    assert len(loader.CATALOG)==7
    for table in loader.CATALOG:
        source=next(s for s in sources if s['name']==table['name'])
        assert [c['name'] for c in source['columns']][:len(table['columns'])]==[c['name'] for c in table['columns']]
