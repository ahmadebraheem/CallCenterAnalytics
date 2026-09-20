"""Regenerate the raw schema catalog and dbt sources from generator schemas/descriptions.

Run from the repository root with the generator's dependencies installed.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pyarrow as pa
import yaml
from gim_synth.config import load_config
from gim_synth.dictionary import COLUMN_DESCRIPTIONS, TABLE_DESCRIPTIONS
from gim_synth.refdata import build_refdata
from gim_synth.schema import SCHEMA, OUTCOME_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def ch_type(field):
    typ = field.type
    mapping = {'int8': 'Int8', 'int16': 'Int16', 'int32': 'Int32', 'int64': 'Int64',
               'float': 'Float32', 'double': 'Float64', 'string': 'String', 'date32[day]': 'Date'}
    if pa.types.is_timestamp(typ):
        result = "DateTime64(3, 'UTC')" if typ.tz else 'String'
    else:
        result = mapping[str(typ)]
    return f'Nullable({result})' if field.nullable else result


def main():
    cfg = load_config(overrides={'calls_per_day': 100, 'customers': {'pool_size': 10}})
    dims = build_refdata(cfg).dimension_tables()
    schemas = {'interaction_resource_fact': SCHEMA, 'interaction_outcome_fact': OUTCOME_SCHEMA,
               **{name: table.schema for name, table in dims.items()}}
    catalog, sources, models = [], [], []
    keys = {'interaction_resource_fact': 'IRF_ID', 'interaction_outcome_fact': 'OUTCOME_ID',
            'dim_site': 'SITE', 'dim_lob': 'LOB', 'dim_vq': 'VQ_ID',
            'dim_agent': 'AGENT_ID', 'dim_customer': 'CUSTOMER_ID'}
    staging = ROOT/'dbt/models/staging'
    staging.mkdir(parents=True, exist_ok=True)
    tests = ROOT/'dbt/tests'
    tests.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas.items():
        columns = [{'name': f.name, 'clickhouse_type': ch_type(f),
                    'description': COLUMN_DESCRIPTIONS[name][f.name]} for f in schema]
        catalog.append({'name': name, 'description': TABLE_DESCRIPTIONS[name], 'columns': columns})
        sources.append({'name': name, 'description': TABLE_DESCRIPTIONS[name],
                        'columns': [{'name': c['name'], 'description': c['description']} for c in columns] + [
                            {'name': '_dataset_id', 'description': 'User-supplied dataset identity; include in joins.'},
                            {'name': '_load_id', 'description': 'Attempt UUID; only completed attempts are staged.'},
                            {'name': '_source_file', 'description': 'Relative Parquet path.'},
                            {'name': '_ingested_at', 'description': 'UTC batch ingestion time.'},
                            {'name': '_source_timezone', 'description': 'Generator timezone from manifest.'}]})
        (staging/f'stg_{name}.sql').write_text(
            "-- Only publish complete, reconciled dataset attempts.\n"
            "select *\nfrom {{ source('gim_raw', '"+name+"') }}\n"
            "where _load_id in (select load_id from {{ source('gim_raw', '_dataset_loads') }})\n", encoding='utf-8')
        models.append({'name': 'stg_'+name, 'description': 'Completed loads: '+TABLE_DESCRIPTIONS[name],
                       'columns': [{'name': keys[name], 'data_tests': ['not_null']},
                                   {'name': '_dataset_id', 'data_tests': ['not_null']}]})
        (tests/f'unique_{name}.sql').write_text(
            "select _dataset_id, `"+keys[name]+"`, count() as rows\n"
            "from {{ ref('stg_"+name+"') }}\n"
            "group by _dataset_id, `"+keys[name]+"` having count() > 1\n", encoding='utf-8')
    sources.append({'name': '_dataset_loads', 'description': 'Completion ledger; rows are written only after all seven tables reconcile.',
                    'columns': [{'name': 'load_id', 'data_tests': ['not_null', 'unique']},
                                {'name': 'dataset_id', 'data_tests': ['not_null', 'unique']}]})
    (ROOT/'catalog.json').write_text(json.dumps(catalog, indent=2)+'\n', encoding='utf-8')
    (staging/'sources.yml').write_text(yaml.safe_dump({'version': 2, 'sources': [
        {'name': 'gim_raw', 'schema': 'callcenter_analytics', 'tables': sources}]}, sort_keys=False), encoding='utf-8')
    (staging/'models.yml').write_text(yaml.safe_dump({'version': 2, 'models': models}, sort_keys=False), encoding='utf-8')
    print('Generated seven raw table definitions, all source columns, staging views and grain tests.')


if __name__ == '__main__':
    main()
