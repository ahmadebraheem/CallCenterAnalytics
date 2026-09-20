"""Bootstrap raw sources and load complete generator datasets in bounded Arrow batches.

Run one loader at a time. Completion is published only after all seven tables reconcile.
Failed attempts stay invisible to staging and may be retried under a new load UUID.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

import clickhouse_connect
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
DATABASE = 'callcenter_analytics'


def client():
    return clickhouse_connect.get_client(
        host=os.getenv('CLICKHOUSE_HOST', 'callcenter-clickhouse'), port=8123,
        username=os.getenv('CLICKHOUSE_USER', 'ingest'),
        password=os.environ['CLICKHOUSE_PASSWORD'],
        connect_timeout=15, send_receive_timeout=600,
        settings={'async_insert': 0, 'max_threads': 2},
    )


def raw_columns(table):
    return [(c['name'], c['clickhouse_type']) for c in table['columns']] + [
        ('_dataset_id', 'String'), ('_load_id', 'String'),
        ('_source_file', 'String'), ('_ingested_at', "DateTime64(3, 'UTC')"),
        ('_source_timezone', 'String'),
    ]


def bootstrap(ch):
    password = os.environ['CLICKHOUSE_DBT_PASSWORD']
    if len(password) < 16:
        raise ValueError('CLICKHOUSE_DBT_PASSWORD must have at least 16 characters')
    for database in [DATABASE, 'callcenter_dbt_dev', 'callcenter_dbt_prod']:
        ch.command(f'CREATE DATABASE IF NOT EXISTS {database}')
    # Existing tables are checked before any load; incompatible schemas are not replaced.
    for table in CATALOG:
        columns = ', '.join(f'`{name}` {typ}' for name, typ in raw_columns(table))
        ch.command(f"CREATE TABLE IF NOT EXISTS {DATABASE}.{table['name']} ({columns}) "
                   'ENGINE = MergeTree ORDER BY (_dataset_id, _load_id)')
    ch.command(f"""CREATE TABLE IF NOT EXISTS {DATABASE}._dataset_loads (
        dataset_id String, load_id String, fingerprint String,
        completed_at DateTime64(3, 'UTC'), source_timezone String, row_counts String
    ) ENGINE = MergeTree ORDER BY (dataset_id, load_id)""")
    # Hash avoids SQL interpolation of password text and avoids logging plaintext.
    digest = hashlib.sha256(password.encode()).hexdigest()
    ch.command(f"CREATE USER IF NOT EXISTS dbt IDENTIFIED WITH sha256_hash BY '{digest}'")
    ch.command(f"ALTER USER dbt IDENTIFIED WITH sha256_hash BY '{digest}'")
    ch.command('GRANT SELECT ON callcenter_analytics.* TO dbt')
    for database in ['callcenter_dbt_dev', 'callcenter_dbt_prod']:
        ch.command(f'GRANT ALL ON {database}.* TO dbt')
    ch.command('ALTER USER dbt SETTINGS max_threads = 2, max_memory_usage = 536870912, '
               'max_execution_time = 600, max_concurrent_queries_for_user = 1')
    print('Raw tables, completion ledger, output databases and dbt account are ready.')


def discover(directory):
    manifest_path = directory / '_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = {}
    digest = hashlib.sha256(manifest_path.read_bytes())
    for table in CATALOG:
        name = table['name']
        paths = sorted((directory / name).rglob('*.parquet')) if name.endswith('_fact') else [directory / f'{name}.parquet']
        if not paths or any(not p.is_file() for p in paths):
            raise ValueError(f'Missing Parquet input for {name}')
        files[name] = paths
        for path in paths:
            digest.update(path.relative_to(directory).as_posix().encode())
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
    return manifest, files, digest.hexdigest()


def check_schema(ch, table):
    actual = [(row[0], row[1]) for row in ch.query(
        f"DESCRIBE TABLE {DATABASE}.{table['name']}").result_rows]
    if actual != raw_columns(table):
        raise ValueError(f"Existing schema differs for {table['name']}; migrate explicitly before loading")


def prepare_batch(batch, table, dataset, load_id, source_file, source_timezone):
    data = pa.Table.from_batches([batch])
    expected = [c['name'] for c in table['columns']]
    if data.column_names != expected:
        raise ValueError(f"Parquet columns differ for {table['name']}")
    for i, field in enumerate(data.schema):
        # Preserve naive local wall-clock values as strings, never reinterpret as UTC.
        if pa.types.is_timestamp(field.type) and field.type.tz is None:
            data = data.set_column(i, field.name, pc.cast(data.column(i), pa.string()))
    count = len(data)
    for name, value, typ in [
        ('_dataset_id', dataset, pa.string()), ('_load_id', str(load_id), pa.string()),
        ('_source_file', source_file, pa.string()),
        ('_ingested_at', datetime.now(timezone.utc), pa.timestamp('ms', tz='UTC')),
        ('_source_timezone', source_timezone, pa.string()),
    ]:
        data = data.append_column(name, pa.array([value] * count, type=typ))
    return data


def load(ch, directory, dataset):
    if not dataset.strip():
        raise ValueError('Dataset ID must not be empty')
    manifest, files, fingerprint = discover(directory)
    previous = ch.query(f'SELECT DISTINCT fingerprint FROM {DATABASE}._dataset_loads '
                        'WHERE dataset_id = {dataset:String}', parameters={'dataset': dataset}).result_rows
    if previous:
        if {r[0] for r in previous} == {fingerprint}:
            print(f'{dataset}: already loaded; no inserts performed')
            return
        raise ValueError('Dataset ID already published with different files; use a new dataset ID')
    zone = manifest['config']['timezone']
    expected_counts = {name: sum(pq.ParquetFile(p).metadata.num_rows for p in paths)
                       for name, paths in files.items()}
    # Verify the generator's run-level fact counts before writing anything.
    stats = manifest
    for name, key in [('interaction_resource_fact', 'rows'), ('interaction_outcome_fact', 'outcome_rows')]:
        if expected_counts[name] != stats[key]:
            raise ValueError(f'Manifest row count mismatch for {name}')
    for table in CATALOG:
        check_schema(ch, table)
        expected = [c['name'] for c in table['columns']]
        for path in files[table['name']]:
            if pq.ParquetFile(path).schema_arrow.names != expected:
                raise ValueError(f'Unexpected columns in {path.name}')
    load_id = uuid.uuid4()
    for table in CATALOG:
        name = table['name']
        for path in files[name]:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
                data = prepare_batch(batch, table, dataset, load_id,
                                     path.relative_to(directory).as_posix(), zone)
                ch.insert_arrow(f'{DATABASE}.{name}', data)
        count = ch.query(f'SELECT count() FROM {DATABASE}.{name} WHERE _load_id = {{load:String}}',
                         parameters={'load': str(load_id)}).first_row[0]
        if count != expected_counts[name]:
            raise ValueError(f'Loaded count mismatch for {name}; attempt remains unpublished')
        print(f'{name}: verified {count} rows', flush=True)
    if discover(directory)[2] != fingerprint:
        raise ValueError('Input files changed during loading; attempt remains unpublished')
    ch.insert(f'{DATABASE}._dataset_loads', [[dataset, str(load_id), fingerprint,
              datetime.now(timezone.utc), zone, json.dumps(expected_counts, sort_keys=True)]],
              column_names=['dataset_id', 'load_id', 'fingerprint', 'completed_at', 'source_timezone', 'row_counts'])
    print(f'{dataset}: all seven sources published as load {load_id}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('bootstrap')
    loading = commands.add_parser('load')
    loading.add_argument('--directory', type=Path, default=Path('/data'))
    loading.add_argument('--dataset-id', required=True)
    args = parser.parse_args()
    ch = client()
    try:
        bootstrap(ch) if args.command == 'bootstrap' else load(ch, args.directory, args.dataset_id)
    finally:
        ch.close()


if __name__ == '__main__':
    main()
