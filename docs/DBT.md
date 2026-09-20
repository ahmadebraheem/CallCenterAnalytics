# dbt deployment and raw source loading

The optional analytics stack installs **dbt 2.0.6**, the latest `dbt` PyPI release
checked on 2026-09-20. This is the new v2 distribution with bundled adapters, not the
older Python `dbt-core` / `dbt-clickhouse` installation. ClickHouse support in v2 is
currently beta. The image pins the release so rebuilds do not silently upgrade it.

The first deployment provides all seven raw sources, seven staging views, source
column documentation, null/key tests, and the loader. Reporting marts and scheduled
execution are later steps. **dbt sources describe existing tables; they do not load
Parquet themselves.** Use the bootstrap and loader commands below.

## Files and connection

- `analytics/Dockerfile` and `requirements.txt`: on-demand dbt/loader image.
- `analytics/dbt`: project, environment-based profile, sources, staging and SQL tests.
- `analytics/catalog.json`: generated raw column definitions for all seven tables.
- `analytics/scripts/load.py`: bootstrap and bounded-batch ingestion.
- `infra/clickhouse/compose.analytics.yaml`: overlay on the existing ClickHouse stack.

Your tailnet endpoint is `http://callcenter-clickhouse:8123`. The Docker-host deployment
shares the existing Tailscale service's network namespace and uses `127.0.0.1:8123`
inside the containers. It publishes no ports and needs no second Tailscale enrollment.
For a local Python/dbt installation on a tailnet device, the profile defaults to
`callcenter-clickhouse`. No database passwords are checked into Git.

The dbt profile defaults to development (`callcenter_dbt_dev`); `--target prod` uses
`callcenter_dbt_prod`. Both are separate from raw `callcenter_analytics`. The SQL-managed
`dbt` user reads raw data and manages objects in the two dbt databases. It has no
global administration grant. The reader account can read production outputs. The
loader uses `ingest`, now granted SELECT as well as INSERT for reconciliation/replay.

dbt runs one thread at a time. Its container and the loader are each capped at 1 GiB
RAM / 1 CPU and run only when invoked. Run them sequentially. This memory is **additional**
to the existing ClickHouse 3 GiB budget; allow at least that headroom on the host.

## Deploy on the verified Docker host

Use the existing checkout and existing `infra/clickhouse/.env`. Fetch/copy these new
files there first. Do not replace existing passwords or delete volumes.

1. Add `CLICKHOUSE_DBT_PASSWORD` with a new distinct strong password (at least 16,
   preferably 32+ random characters) to `.env`. Keep it private.
2. Set `DATA_DIR` to the generated output directory **on the Docker host**. The default
   is `../../out`, relative to `infra/clickhouse`. Set `DBT_TARGET=dev` initially.
3. From `infra/clickhouse`, run:

```text
docker compose -f compose.yaml -f compose.analytics.yaml --profile analytics config --quiet
docker compose -f compose.yaml -f compose.analytics.yaml build dbt
docker compose up -d --wait --wait-timeout 180
docker compose restart clickhouse
docker compose up -d --wait --wait-timeout 180
docker compose -f compose.yaml -f compose.analytics.yaml run --rm bootstrap
docker compose -f compose.yaml -f compose.analytics.yaml run --rm dbt debug
docker compose -f compose.yaml -f compose.analytics.yaml run --rm dbt parse
```

The restart applies the updated XML grants for `ingest` and `reader`; it briefly
interrupts database connections. Bootstrap is explicit and also works against an
existing data volume. It creates missing databases/tables, creates or rotates the
`dbt` user's password, and applies its grants/settings. It does not drop or replace
existing source tables. Do not run `up` for every analytics service: these are jobs.

Bootstrap uses the admin password only in its own container. dbt receives only its
own password, and the loader only the ingestion password. The image build needs
outbound package-download access; runtime may also download dbt's bundled adapter
components as required by v2. Credentials are never build arguments.

## Load all seven sources

The directory must contain `_manifest.json`, both fact directories (daily partitions
or single-file output), and these five dimension files:

- `dim_site.parquet`
- `dim_lob.parquet`
- `dim_vq.parquet`
- `dim_agent.parquet`
- `dim_customer.parquet`

Run the loader once per complete generated dataset:

```text
docker compose -f compose.yaml -f compose.analytics.yaml run --rm loader load --dataset-id august-2026-v1
docker compose -f compose.yaml -f compose.analytics.yaml run --rm dbt build
docker compose -f compose.yaml -f compose.analytics.yaml run --rm dbt build --target prod
```

Choose a meaningful unique dataset ID. Include `_dataset_id` in joins because separate
generator runs can reuse interaction, outcome, agent and dimension identifiers.
Use only one loader at a time, including across hosts. The completion ledger does not
provide a distributed lock or a database uniqueness constraint.

The loader checks all seven files/schemas, verifies fact counts against the manifest,
hashes input contents, and inserts in batches of at most 8192 rows. It then reconciles
each table's loaded count and verifies inputs did not change. Only after all checks
pass does it publish a row in `_dataset_loads`. Staging views expose only completed
load IDs. An empty but valid dataset is allowed; it is not evidence of business volume.

An identical rerun for a completed dataset ID performs no inserts. A changed dataset
using an already completed ID fails with instructions to use a new ID. A failed load
leaves unpublished rows; retrying creates a new attempt ID and keeps those old rows
out of staging. Administrative cleanup of failed attempts is a later maintenance
operation. Direct queries against raw sources can see unpublished rows; consume the
staging views for complete data. Do not edit input files while loading.

If existing raw tables were created manually, the loader refuses incompatible schemas
instead of changing or deleting them. Plan an explicit migration before loading.
The new bootstrap catalog includes five ingestion metadata columns on every table:
`_dataset_id`, `_load_id`, `_source_file`, `_ingested_at`, `_source_timezone`.

## Timestamp and type contract

Generator nullability and numeric widths are retained in raw tables. UTC timestamp
fields become `Nullable(DateTime64(3, 'UTC'))`. Naive local timestamp fields become
nullable strings containing their original wall-clock values. They are not relabeled
as UTC. `_source_timezone` records the original generator timezone for future explicit
conversion. `CALL_DATE` remains the root interaction's business date.

The two facts are `interaction_resource_fact` and `interaction_outcome_fact`. Each
source has a corresponding `stg_<table>` view, as do all five dimensions. Views preserve
source column names in this first deployment. `_dataset_loads` is an additional
technical source, not an eighth generated business table.

The initial raw MergeTree sorting key is `(_dataset_id, _load_id)` so completion-filtered
loads can be located together; no business-date partitions or business-data TTL are
introduced here. Reporting-specific sorting/partitioning comes with the marts.

## Checks and artifacts

`dbt build` runs source ledger uniqueness/not-null checks, creates the staging views,
and tests staging keys for nulls and uniqueness within a dataset. Source descriptions
cover every generator column. Source freshness thresholds are deliberately absent:
historical synthetic event dates are not a measure of ingestion freshness.

The overlay persists dbt outputs in `callcenter-clickhouse_dbt-target` and logs in
`callcenter-clickhouse_dbt-logs`. dbt v2 uses commands such as `compile --write-catalog`
for catalog artifacts; do not use the old v1 `dbt docs generate` workflow.

For development without Docker, install `analytics/requirements.txt` into an isolated
environment, supply `DBT_ENV_SECRET_CLICKHOUSE_PASSWORD`, and run:

```text
dbt debug --project-dir analytics/dbt --profiles-dir analytics/dbt
dbt parse --project-dir analytics/dbt --profiles-dir analytics/dbt
dbt build --project-dir analytics/dbt --profiles-dir analytics/dbt
```

For the direct Python loader, set `CLICKHOUSE_HOST`, `CLICKHOUSE_USER`, and
`CLICKHOUSE_PASSWORD`, then run `python analytics/scripts/load.py load --directory out
--dataset-id august-2026-v1` on one line. Bootstrap requires the admin account and
`CLICKHOUSE_DBT_PASSWORD` as well.

Regenerate source definitions when generator schemas change (also needs generator
dependencies): `python analytics/scripts/generate_sources.py`. Review resulting type
changes and migrate existing raw tables deliberately; regeneration is not a migration.
Loader tests: `python -m pytest analytics/tests -q` (requires pytest and analytics deps).

## Deployment verification (2026-09-21)

Deployed against the existing local Docker Desktop stack `callcenter-clickhouse`.
The image built successfully and `dbt debug` passed all checks, including Git and
the ClickHouse connection. Both development and production builds created seven
views and passed all 25 dbt tests. The live raw database remains empty, ready for
the first generated dataset; bootstrap did not populate demonstration data there.

An isolated ClickHouse instance loaded all seven tables from a small generated
dataset (149 resource legs and 92 outcomes plus dimensions). Its dbt build passed
all 25 tests, and repeating the loader skipped all inserts. The isolated instance
was removed after verification. Seven loader contract tests and all 28 existing
generator tests also passed. Configuration reload applied the new live grants
without restarting the server.

## Release references

- [dbt 2 installation](https://docs.getdbt.com/docs/local/install-dbt)
- [ClickHouse connection for dbt v2](https://docs.getdbt.com/docs/local/connect-data-platform/clickhouse-setup)
- [dbt release metadata](https://pypi.org/pypi/dbt/json)
