"""Read-only Parquet checks. SQLite keeps cross-file keys/joins off the Python heap."""
from collections import Counter
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gim_synth.config import load_config
from gim_synth.validate import validate_table, validate_outcomes

KEYS = {'dim_site': 'SITE', 'dim_lob': 'LOB', 'dim_vq': 'VQ_ID',
        'dim_agent': 'AGENT_ID', 'dim_customer': 'CUSTOMER_ID',
        'interaction_resource_fact': 'IRF_ID', 'interaction_outcome_fact': 'OUTCOME_ID'}
LEG_FIELDS = ['IRF_ID', 'ANSWER_TIME', 'END_TIME', 'ACW_END_TIME', 'DISPOSITION',
              'AGENT_ID', 'ANSWERED_FLAG', 'N_CUSTOMER', 'CALL_TYPE']
METRICS = {'interaction_resource_fact': ['DURATION', 'TALK_TIME', 'HOLD_TIME', 'ACW_TIME', 'HANDLE_TIME'],
           'interaction_outcome_fact': ['AMOUNT']}


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def discover(directory, catalog):
    directory = Path(directory).resolve()
    manifest = json.loads((directory/'_manifest.json').read_text(encoding='utf-8'))
    zone = manifest['config']['timezone']
    ZoneInfo(zone)
    files, signatures, hashes = {}, {}, {}
    for table in catalog:
        name = table['name']
        paths = sorted((directory/name).rglob('*.parquet')) if name.endswith('_fact') else [directory/f'{name}.parquet']
        if not paths or any(not p.is_file() for p in paths):
            raise ValueError(f'Missing Parquet input for {name}')
        signatures[name] = []
        for path in paths:
            digest = file_hash(path)
            # Empty daily partitions can legitimately have identical Parquet bytes.
            if pq.ParquetFile(path).metadata.num_rows:
                if digest in hashes:
                    raise ValueError(f'Duplicate nonempty files: {hashes[digest]} and {path}')
                hashes[digest] = path
            signatures[name].append(digest)
        files[name] = paths
    expected_paths = {p for paths in files.values() for p in paths}
    if set(directory.rglob('*.parquet')) != expected_paths:
        raise ValueError('Unexpected Parquet files outside the seven source tables')
    # Ignore paths, generated_at and elapsed time: renaming/copying the same input
    # must not bypass duplicate detection. Re-encoding Parquet can change this hash.
    canonical = {'timezone': zone, 'tables': {k: sorted(v) for k, v in signatures.items()}}
    fingerprint = 'v2:' + hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    return manifest, files, fingerprint


def expected_arrow(column):
    typ = column['clickhouse_type'].removeprefix('Nullable(')
    if column['clickhouse_type'].startswith('Nullable('):
        typ = typ[:-1]
    # These raw String fields represent naive generator timestamps.
    if column['name'] in {'ARRIVE_TIME', 'ANSWER_TIME', 'END_TIME', 'ACW_END_TIME',
                          'INTERVAL_15MIN', 'OUTCOME_TIME', 'RECORDED_TIME', 'FOLLOW_UP_DUE_TIME'}:
        return pa.timestamp('ms')
    return {'Int8': pa.int8(), 'Int16': pa.int16(), 'Int32': pa.int32(), 'Int64': pa.int64(),
            'Float32': pa.float32(), 'Float64': pa.float64(), 'String': pa.string(),
            'Date': pa.date32(), "DateTime64(3, 'UTC')": pa.timestamp('ms', tz='UTC')}[typ]


def check_input_schema(schema, table):
    if schema.names != [c['name'] for c in table['columns']]:
        raise ValueError(f"Column mismatch: {table['name']}")
    for field, column in zip(schema, table['columns']):
        if field.type != expected_arrow(column):
            raise ValueError(f"Type mismatch: {table['name']}.{field.name}: {field.type}")


def insert_unique(db, table, values):
    if any(v is None for v in values):
        raise ValueError(f'Null required key in {table}')
    try:
        db.executemany(f'INSERT INTO "{table}" VALUES (?)', ((v,) for v in values))
    except sqlite3.IntegrityError as exc:
        raise ValueError(f'Duplicate key across rows/files in {table}') from exc


def validate(directory, catalog):
    directory = Path(directory).resolve()
    manifest, files, fingerprint = discover(directory, catalog)
    cfg = load_config(overrides=manifest['config'])
    configured_days = {str(date.fromisoformat(cfg.start_date)+timedelta(days=i)) for i in range(cfg.days)}
    tables = {t['name']: t for t in catalog}
    dims = {}
    result = {'status': 'validated', 'directory': str(directory), 'fingerprint': fingerprint,
              'timezone': cfg.timezone, 'tables': {}}
    with tempfile.TemporaryDirectory(prefix='parquet-checks-') as temp:
        db = sqlite3.connect(str(Path(temp)/'keys.sqlite'))
        try:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA cache_size=-16384')
            for name in KEYS:
                db.execute(f'CREATE TABLE "{name}" (k PRIMARY KEY NOT NULL) WITHOUT ROWID')
            db.execute('CREATE TABLE call_ids (k TEXT PRIMARY KEY NOT NULL) WITHOUT ROWID')
            db.execute('CREATE TABLE outcome_seq (irf INTEGER, seq INTEGER, PRIMARY KEY(irf, seq)) WITHOUT ROWID')
            db.execute('CREATE TABLE legs (IRF_ID INTEGER PRIMARY KEY, ANSWER_TIME INTEGER, END_TIME INTEGER, '
                       'ACW_END_TIME INTEGER, DISPOSITION TEXT, AGENT_ID TEXT, ANSWERED_FLAG INTEGER, N_CUSTOMER INTEGER, CALL_TYPE TEXT)')
            leg_schema = None
            for name, key in KEYS.items():
                table = tables[name]
                nulls = Counter({c['name']: 0 for c in table['columns']})
                totals, per_file = Counter(), {}
                count = 0
                for path in files[name]:
                    parquet = pq.ParquetFile(path)
                    check_input_schema(parquet.schema_arrow, table)
                    per_file[path.relative_to(directory).as_posix()] = parquet.metadata.num_rows
                    for batch in parquet.iter_batches(batch_size=8192):
                        data = pa.Table.from_batches([batch])
                        count += len(data)
                        insert_unique(db, name, data[key].to_pylist())
                        for field in data.schema:
                            col = data[field.name]
                            nulls[field.name] += col.null_count
                            if pa.types.is_floating(field.type) and pc.any(pc.invert(pc.is_finite(col))).as_py():
                                raise ValueError(f'Non-finite numeric value: {name}.{field.name}')
                        for metric in METRICS.get(name, []):
                            totals[metric] += pc.sum(pc.cast(data[metric], pa.float64())).as_py() or 0
                        references = [('SITE', 'dim_site', 'SITE'), ('HOME_SITE', 'dim_site', 'SITE'),
                                      ('LOB', 'dim_lob', 'LOB'), ('AGENT_ID', 'dim_agent', 'AGENT_ID'),
                                      ('CUSTOMER_ID', 'dim_customer', 'CUSTOMER_ID'),
                                      ('VQ_NAME', 'dim_vq', 'VQ_NAME'), ('VQ_ID', 'dim_vq', 'VQ_ID'),
                                      ('PRIMARY_VQ_NAME', 'dim_vq', 'VQ_NAME')]
                        for col, dim, dimkey in references:
                            if col in data.column_names and dim in dims and dim != name:
                                bad = pc.and_(pc.is_valid(data[col]), pc.invert(pc.is_in(data[col], value_set=dims[dim][dimkey])))
                                if pc.any(bad).as_py():
                                    raise ValueError(f'Orphan reference: {name}.{col} -> {dim}.{dimkey}')
                        failures = []
                        if name.endswith('_fact'):
                            days = data['CALL_DATE'].to_pylist()
                            if any(v is None or str(v) not in configured_days for v in days):
                                raise ValueError(f'CALL_DATE outside manifest range: {path}')
                            if path.parent.name.startswith('call_date=') and any(str(v) != path.parent.name[10:] for v in days):
                                raise ValueError(f'Partition date mismatch: {path}')
                        if name == 'interaction_resource_fact':
                            insert_unique(db, 'call_ids', data['CALL_ID'].to_pylist())
                            levels = dict(zip(dims['dim_vq']['VQ_NAME'].to_pylist(), dims['dim_vq']['SERVICE_LEVEL_S'].to_pylist()))
                            failures = validate_table(data, cfg, levels)
                            selected = data.select(LEG_FIELDS)
                            leg_schema = selected.schema
                            cols = [pc.cast(selected[f.name], pa.int64()).to_pylist() if pa.types.is_timestamp(f.type)
                                    else selected[f.name].to_pylist() for f in selected.schema]
                            db.executemany('INSERT INTO legs VALUES (?,?,?,?,?,?,?,?,?)', zip(*cols))
                        elif name == 'interaction_outcome_fact':
                            ids = data['IRF_ID'].to_pylist(); seqs = data['OUTCOME_SEQ'].to_pylist()
                            if any(i is None or s is None or s < 1 for i, s in zip(ids, seqs)):
                                raise ValueError('Missing outcome IRF_ID or invalid OUTCOME_SEQ')
                            try:
                                db.executemany('INSERT INTO outcome_seq VALUES (?,?)', zip(ids, seqs))
                            except sqlite3.IntegrityError as exc:
                                raise ValueError('Duplicate (IRF_ID, OUTCOME_SEQ)') from exc
                            rows = []
                            unique_ids = list(set(ids))
                            for start in range(0, len(unique_ids), 500):
                                part = unique_ids[start:start+500]
                                rows.extend(db.execute('SELECT * FROM legs WHERE IRF_ID IN ('+','.join('?'*len(part))+')', part))
                            if len(rows) != len(unique_ids):
                                raise ValueError('Orphan outcome IRF_ID')
                            facts = pa.Table.from_arrays([pa.array([r[i] for r in rows], type=f.type)
                                                        for i, f in enumerate(leg_schema)], schema=leg_schema)
                            failures = [f for f in validate_outcomes(data, facts)
                                        if f != 'outcomes: secondary outcome without a primary']
                        if failures:
                            raise ValueError(f'{path}: '+ '; '.join(failures))
                if name.startswith('dim_'):
                    dims[name] = pq.ParquetFile(files[name][0]).read()
                    if name == 'dim_vq':
                        overflow = dims[name]['OVERFLOW_VQ_NAME']
                        if pc.any(pc.and_(pc.is_valid(overflow), pc.invert(pc.is_in(overflow, value_set=dims[name]['VQ_NAME'])))).as_py():
                            raise ValueError('Orphan OVERFLOW_VQ_NAME')
                result['tables'][name] = {'rows': count, 'files': per_file, 'null_counts': dict(nulls), 'totals': dict(totals)}
                print(f'Validated {name}: {count:,} rows', file=sys.stderr, flush=True)
            if db.execute('SELECT 1 FROM outcome_seq s WHERE s.seq > 1 AND NOT EXISTS '
                          '(SELECT 1 FROM outcome_seq p WHERE p.irf=s.irf AND p.seq=1) LIMIT 1').fetchone():
                raise ValueError('Secondary outcome without a primary across files')
            for name, key in [('interaction_resource_fact', 'rows'), ('interaction_outcome_fact', 'outcome_rows')]:
                if result['tables'][name]['rows'] != manifest[key]:
                    raise ValueError(f'Manifest row count mismatch: {name}')
        finally:
            db.close()
    if discover(directory, catalog)[2] != fingerprint:
        raise ValueError('Input changed during validation')
    return result
