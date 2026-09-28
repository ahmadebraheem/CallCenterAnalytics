"""Bootstrap raw sources and load complete generator datasets in bounded Arrow batches.

Completion is published only after all seven tables reconcile. A server-side lock
serializes CLI writers. Interrupted writes retain their lock until explicit recovery.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import math
import sys
from contextlib import contextmanager
from pathlib import Path
import uuid

import clickhouse_connect
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks

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


def create_tables(ch):
    ch.command(f'CREATE DATABASE IF NOT EXISTS {DATABASE}')
    for table in CATALOG:
        columns = ', '.join(f'`{name}` {typ}' for name, typ in raw_columns(table))
        ch.command(f"CREATE TABLE IF NOT EXISTS {DATABASE}.{table['name']} ({columns}) "
                   'ENGINE = MergeTree ORDER BY (_dataset_id, _load_id)')
        check_schema(ch, table)
    ch.command(f"""CREATE TABLE IF NOT EXISTS {DATABASE}._dataset_loads (
        dataset_id String, load_id String, fingerprint String,
        completed_at DateTime64(3, 'UTC'), source_timezone String, row_counts String
    ) ENGINE = MergeTree ORDER BY (dataset_id, load_id)""")


def bootstrap(ch):
    password = os.environ['CLICKHOUSE_DBT_PASSWORD']
    if len(password) < 16:
        raise ValueError('CLICKHOUSE_DBT_PASSWORD must have at least 16 characters')
    create_tables(ch)
    for database in [DATABASE, 'callcenter_dbt_dev', 'callcenter_dbt_prod']:
        ch.command(f'CREATE DATABASE IF NOT EXISTS {database}')
    # Hash avoids SQL interpolation of password text and avoids logging plaintext.
    digest = hashlib.sha256(password.encode()).hexdigest()
    ch.command(f"CREATE USER IF NOT EXISTS dbt IDENTIFIED WITH sha256_hash BY '{digest}'")
    ch.command(f"ALTER USER dbt IDENTIFIED WITH sha256_hash BY '{digest}'")
    ch.command('GRANT SELECT ON callcenter_analytics.* TO dbt')
    for database in ['callcenter_dbt_dev', 'callcenter_dbt_prod']:
        ch.command(f'GRANT ALL ON {database}.* TO dbt')
    ch.command('ALTER USER dbt SETTINGS max_threads = 2, max_memory_usage = 536870912, '
               'max_execution_time = 600, max_concurrent_queries_for_user = 1')
    print('Raw tables, completion ledger, output databases and dbt account are ready.', file=sys.stderr)


def discover(directory):
    return checks.discover(directory, CATALOG)


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


def lock_owner(ch):
    rows = ch.query('SELECT comment FROM system.tables WHERE database = {db:String} '
                    "AND name = '_loader_lock'", parameters={'db': DATABASE}).result_rows
    return rows[0][0] if rows else None


def unlock(ch, owner):
    if lock_owner(ch) != owner:
        raise ValueError('Lock owner mismatch; refusing to unlock')
    ch.command(f'DROP TABLE {DATABASE}._loader_lock SYNC')


@contextmanager
def writer_lock(ch):
    owner = str(uuid.uuid4())
    try:
        # A single-server CREATE without IF NOT EXISTS is the atomic arbiter.
        ch.command(f"CREATE TABLE {DATABASE}._loader_lock (unused UInt8) ENGINE=Memory COMMENT '{owner}'")
    except Exception as exc:
        raise RuntimeError('Cannot acquire loader lock. Another loader may be active; '
                           'use status to inspect it. No automatic lock stealing.') from exc
    state = {'writing': False}
    print(f'Loader lock owner: {owner}', file=sys.stderr, flush=True)
    try:
        yield state
    except BaseException:
        if state['writing']:
            print(f'Load interrupted; lock {owner} retained. Stop/confirm all requests have finished, '
                  'then use unlock --owner TOKEN --confirm-stopped before retrying.', file=sys.stderr)
        else:
            unlock(ch, owner)
        raise
    else:
        unlock(ch, owner)


def completed(ch, dataset):
    return ch.query(f'SELECT dataset_id, load_id, fingerprint FROM {DATABASE}._dataset_loads '
                    'WHERE dataset_id = {dataset:String}', parameters={'dataset': dataset}).result_rows


def verify_data(ch, dataset, report, load_id):
    """Check all physical rows for this dataset, not just its visible load attempt."""
    counts = {}
    for table in CATALOG:
        name = table['name']; key = checks.KEYS[name]
        expected = report['tables'][name]
        params = {'dataset': dataset, 'load': str(load_id), 'zone': report['timezone']}
        where = '_dataset_id = {dataset:String}'
        summary = ch.query(f'SELECT count(), uniqExact(`{key}`), countIf(isNull(`{key}`)), '
                           'countIf(_load_id != {load:String}), countIf(_source_timezone != {zone:String}) '
                           f'FROM {DATABASE}.{name} WHERE {where}', parameters=params).first_row
        if tuple(summary) != (expected['rows'], expected['rows'], 0, 0, 0):
            raise ValueError(f'Count/key/load identity mismatch in {name}: {summary}')
        if name == 'interaction_resource_fact':
            distinct = ch.query(f'SELECT uniqExact(CALL_ID), countIf(isNull(CALL_ID)) FROM {DATABASE}.{name} '
                                f'WHERE {where}', parameters=params).first_row
            if tuple(distinct) != (expected['rows'], 0):
                raise ValueError('Duplicate or null CALL_ID in database')
        expressions = [f'countIf(isNull(`{c["name"]}`))' for c in table['columns']]
        metric_names = checks.METRICS.get(name, [])
        expressions += [f'sum(toFloat64(`{m}`))' for m in metric_names]
        row = ch.query(f'SELECT {", ".join(expressions)} FROM {DATABASE}.{name} WHERE {where}', parameters=params).first_row
        for index, col in enumerate(table['columns']):
            if row[index] != expected['null_counts'].get(col['name'], 0):
                raise ValueError(f'Null-count mismatch: {name}.{col["name"]}')
        for index, metric in enumerate(metric_names, len(table['columns'])):
            if not math.isclose(float(row[index]), expected['totals'].get(metric, 0), rel_tol=1e-9, abs_tol=1e-5):
                raise ValueError(f'Total mismatch: {name}.{metric}')
        actual_files = dict(ch.query(f'SELECT _source_file, count() FROM {DATABASE}.{name} WHERE {where} '
                                     'GROUP BY _source_file', parameters=params).result_rows)
        if actual_files != {k: v for k, v in expected['files'].items() if v}:
            raise ValueError(f'Per-file row-count mismatch: {name}')
        counts[name] = expected['rows']
    return counts


def pending_rows(ch, dataset):
    return {t['name']: ch.query(f'SELECT count() FROM {DATABASE}.{t["name"]} '
                                'WHERE _dataset_id = {dataset:String}',
                                parameters={'dataset': dataset}).first_row[0] for t in CATALOG}


def load(ch, directory, dataset, *, retry=False, report=None):
    directory = Path(directory).resolve()
    if not dataset.strip():
        raise ValueError('Dataset ID must not be empty')
    report = report or checks.validate(directory, CATALOG)
    manifest, files, fingerprint = discover(directory)
    if fingerprint != report['fingerprint']:
        raise ValueError('Input changed after validation')
    with writer_lock(ch) as state:
        for table in CATALOG:
            check_schema(ch, table)
        previous = completed(ch, dataset)
        if previous:
            if len(previous) != 1 or previous[0][2] != fingerprint:
                raise ValueError('Dataset ID already published with different files or duplicate ledger entries')
            verify_data(ch, dataset, report, previous[0][1])
            return {**report, 'status': 'skipped', 'dataset_id': dataset, 'load_id': previous[0][1]}
        other = ch.query(f'SELECT dataset_id FROM {DATABASE}._dataset_loads '
                         'WHERE fingerprint = {fingerprint:String} LIMIT 1',
                         parameters={'fingerprint': fingerprint}).result_rows
        if other:
            raise ValueError(f'Identical content is already published as dataset {other[0][0]}')
        pending = pending_rows(ch, dataset)
        if any(pending.values()):
            if not retry:
                raise ValueError('Incomplete physical rows exist; use --retry with ALTER DELETE permission to clean this dataset first')
            state['writing'] = True
            # No completed load exists for this dataset; never delete a published one.
            for name, count in pending.items():
                if count:
                    ch.command(f'ALTER TABLE {DATABASE}.{name} DELETE WHERE _dataset_id = {{dataset:String}}',
                               parameters={'dataset': dataset}, settings={'mutations_sync': 2})
            if any(pending_rows(ch, dataset).values()):
                raise ValueError('Retry cleanup failed; refusing to insert')
        load_id = str(uuid.uuid4())
        state['writing'] = True
        for table in CATALOG:
            name = table['name']
            for path in files[name]:
                for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
                    data = prepare_batch(batch, table, dataset, load_id,
                                         path.relative_to(directory).as_posix(), manifest['config']['timezone'])
                    ch.insert_arrow(f'{DATABASE}.{name}', data)
            print(f'Inserted {name}', file=sys.stderr, flush=True)
        counts = verify_data(ch, dataset, report, load_id)
        if discover(directory)[2] != fingerprint:
            raise ValueError('Input changed during loading; attempt remains unpublished')
        ch.insert(f'{DATABASE}._dataset_loads', [[dataset, load_id, fingerprint,
                  datetime.now(timezone.utc), report['timezone'], json.dumps(counts, sort_keys=True)]],
                  column_names=['dataset_id', 'load_id', 'fingerprint', 'completed_at', 'source_timezone', 'row_counts'])
        if completed(ch, dataset) != [(dataset, load_id, fingerprint)]:
            raise ValueError('Completion ledger verification failed')
        return {**report, 'status': 'loaded', 'dataset_id': dataset, 'load_id': load_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('bootstrap', help='Create raw sources plus dbt account (admin)')
    commands.add_parser('init', help='Create and check raw tables only (admin)')
    commands.add_parser('status', help='Show lock owner and completed datasets')
    releasing = commands.add_parser('unlock', help='Manually release a failed writer lock after stopping its requests')
    releasing.add_argument('--owner', required=True)
    releasing.add_argument('--confirm-stopped', action='store_true', required=True)
    for command in ['validate', 'load', 'verify']:
        sub = commands.add_parser(command)
        sub.add_argument('--directory', type=Path, default=Path('/data') if Path('/data').exists() else Path('out'))
        sub.add_argument('--report', type=Path, help='Write a JSON report outside the input directory')
        if command != 'validate':
            sub.add_argument('--dataset-id', required=True)
        if command == 'load':
            sub.add_argument('--create-tables', action='store_true', help='Create missing raw tables first (admin)')
            sub.add_argument('--retry', action='store_true', help='Delete unpublished rows of this dataset before retrying (ALTER DELETE required)')
            sub.add_argument('--dry-run', action='store_true', help='Validate files only; no connection or database changes')
    args = parser.parse_args()
    report = None
    try:
        if getattr(args, 'report', None) and (args.report.resolve() == args.directory.resolve() or args.directory.resolve() in args.report.resolve().parents):
            raise ValueError('Report must be outside the input directory')
        if args.command in ['validate', 'load', 'verify']:
            report = checks.validate(args.directory, CATALOG)
        if args.command == 'validate' or getattr(args, 'dry_run', False):
            result = report
        else:
            ch = client()
            try:
                if args.command == 'bootstrap':
                    bootstrap(ch); result = {'status': 'bootstrapped'}
                elif args.command == 'init':
                    create_tables(ch); result = {'status': 'initialized'}
                elif args.command == 'status':
                    result = {'lock_owner': lock_owner(ch), 'completed': ch.query(
                        f'SELECT dataset_id, load_id, fingerprint FROM {DATABASE}._dataset_loads').result_rows}
                elif args.command == 'unlock':
                    unlock(ch, args.owner); result = {'status': 'unlocked'}
                elif args.command == 'load':
                    if args.create_tables:
                        create_tables(ch)
                    result = load(ch, args.directory, args.dataset_id, retry=args.retry, report=report)
                else:
                    previous = completed(ch, args.dataset_id)
                    if len(previous) != 1 or previous[0][2] != report['fingerprint']:
                        raise ValueError('No single matching completed dataset')
                    for table in CATALOG:
                        check_schema(ch, table)
                    verify_data(ch, args.dataset_id, report, previous[0][1])
                    result = {**report, 'status': 'verified', 'dataset_id': args.dataset_id}
            finally:
                ch.close()
        text = json.dumps(result, indent=2)
        if getattr(args, 'report', None):
            args.report.write_text(text+'\n', encoding='utf-8')
        print(text)
        return 0
    except Exception as exc:
        # Do not dump client exception text: SQL/errors can contain credentials.
        error = {'status': 'failed', 'error': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}
        print(json.dumps(error), file=sys.stderr)
        if getattr(args, 'report', None) and args.directory.resolve() not in args.report.resolve().parents and args.report.resolve() != args.directory.resolve():
            args.report.write_text(json.dumps(error, indent=2)+'\n', encoding='utf-8')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
