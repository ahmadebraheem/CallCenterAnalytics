# Checked Parquet ingestion

`analytics/scripts/load.py` validates and loads all seven generated tables into
`callcenter_analytics`. Input files are read only. A completed load is published in
`_dataset_loads`; dbt staging views expose only published attempts.

## Local CLI

Install `analytics/requirements.txt` in a virtual environment. From the repository
root, validate the actual files without connecting to ClickHouse:

```text
python analytics/scripts/load.py validate --directory out --report validation.json
```

Set `CLICKHOUSE_HOST` (default `callcenter-clickhouse`), `CLICKHOUSE_USER`, and
`CLICKHOUSE_PASSWORD` in your environment. The HTTP port is 8123. Do not put
passwords in command arguments. Use an admin account for the initial table creation:

```text
python analytics/scripts/load.py init
```

Switch to the configured `ingest` credentials to load and verify:

```text
python analytics/scripts/load.py load --directory out --dataset-id august-2026 --report load.json
python analytics/scripts/load.py verify --directory out --dataset-id august-2026 --report verification.json
python analytics/scripts/load.py status
```

Alternatively, an admin can combine creation and ingestion with `load --create-tables`.
`init` creates raw tables only; `bootstrap` also configures the dbt account and requires
`CLICKHOUSE_DBT_PASSWORD`. Existing incompatible raw schemas are refused.

`load --dry-run` performs offline validation without opening a database connection.
Reports must be outside the input directory. Commands emit JSON to stdout and progress
to stderr, return 0 on success and 1 on failure, and write a failure report when
`--report` is supplied. Do not modify input files during validation or ingestion.

## Docker Compose

From `infra/clickhouse`, use the existing private `.env` and set `DATA_DIR` to the
generated directory on the Docker host. Rebuild the image after updating this code:

```text
docker compose -f compose.yaml -f compose.analytics.yaml build dbt
docker compose -f compose.yaml -f compose.analytics.yaml run --rm bootstrap init
docker compose -f compose.yaml -f compose.analytics.yaml run --rm loader validate
docker compose -f compose.yaml -f compose.analytics.yaml run --rm loader load --dataset-id august-2026 > load.json
docker compose -f compose.yaml -f compose.analytics.yaml run --rm loader verify --dataset-id august-2026
```

Apply the updated `users.d/accounts.xml` grants to the server: `ingest` needs SELECT
and INSERT on raw data plus CREATE TABLE and DROP TABLE on the specific
`callcenter_analytics._loader_lock` table. ClickHouse normally reloads this mounted
configuration; if necessary, restart ClickHouse during a suitable interruption window.
It does not need permission to create arbitrary tables. The loader runs with 1 GiB
RAM and one CPU, additional to the database's 3 GiB budget. Run dbt after loading.
Use host stdout redirection for persistent reports; files written inside a disposable
container disappear unless separately mounted.

## Checks and duplicate protection

Before any inserts, validation checks:

- All seven sources, exact Arrow schemas and timestamp types, manifest fact counts,
  expected date range and daily partition dates; unexpected Parquet files are rejected.
- Non-null, unique primary keys across files; unique resource `CALL_ID`; unique
  outcome `(IRF_ID, OUTCOME_SEQ)` pairs and required primary outcomes.
- Dimension references, outcome-to-resource references, and the generator's resource
  and outcome business invariants, including timestamps and durations.
- Finite numeric metrics, duplicate nonempty file contents, and source-file hashes.

Cross-file checks use temporary SQLite storage; inserts contain at most 8,192 rows.
Valid empty datasets are accepted. Local wall-clock timestamps remain strings while
UTC fields retain their UTC timestamp type.

A server-side table lock serializes participating CLI writers across hosts. After
inserting, the loader checks physical row counts, unique/non-null keys, attempt IDs,
timezones, counts per source file, null counts for every column, and selected numeric
totals. Source hashes are checked again before publishing the completion marker.
These checks reconcile the load; they are not a cryptographic comparison of every
database cell. Source business rules and references are checked before loading.

Repeating a completed dataset ID verifies its stored data and skips all inserts.
Changed content under a completed ID is rejected. The same content fingerprint under
another ID is also rejected. Fingerprints include Parquet bytes and source timezone,
but exclude volatile manifest timings. Re-encoding Parquet changes the fingerprint;
overlapping or semantically identical data in independently encoded datasets is not
automatically deduplicated. Keys are scoped to a dataset, so include `_dataset_id` in
joins. Renaming source files can fail the per-file reconciliation on replay.

The lock is cooperative and applies to this single ClickHouse server. Manual SQL and
older loaders can bypass it; raw MergeTree tables have no uniqueness constraint.
Older completion records using the previous fingerprint format fail closed and need
an explicit migration before replay; the CLI does not rewrite them automatically.

## Recovering an interrupted attempt

Once writes begin, a failure retains the lock, including when an insert succeeded but
its acknowledgement was lost. `status` shows its owner token. Before unlocking, stop
the old loader and confirm its server queries have finished (an administrator can
inspect `system.processes`). There is no automatic timeout or lock stealing.

```text
python analytics/scripts/load.py status
python analytics/scripts/load.py unlock --owner OWNER_TOKEN --confirm-stopped
```

Use an admin connection for explicit recovery:

```text
python analytics/scripts/load.py load --directory out --dataset-id august-2026 --retry
```

`--retry` synchronously deletes only unpublished physical rows for that dataset,
verifies they are gone, then reloads. It requires ALTER DELETE privileges that the
normal ingest account does not have. Completed datasets are never deleted by retry;
they follow the verified skip path. Without `--retry`, pending rows cause an error.
Direct raw-table queries can see unpublished rows; consume dbt staging views for
completed data.

For recovery with Docker, run this local Python command using admin environment
credentials, or use the image in an explicitly configured admin container with the
data directory mounted read-only. The Compose `bootstrap` service has no data mount.

## Automated checks

```text
python -m pytest analytics/tests tests -q
```

`analytics/tests/integration_loader.py DIRECTORY` exercises a real ClickHouse server
using the connection environment variables above and creates/drops only a randomly
named test database. Run it on a disposable server with admin credentials. It checks
lock exclusion, replay, duplicate content, physical duplicate/metric corruption,
failed-insert recovery, and a lost completion acknowledgement.
