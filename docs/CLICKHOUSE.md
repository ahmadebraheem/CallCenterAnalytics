# ClickHouse over Tailscale

This optional stack lives in `infra/clickhouse`. It runs independently of the synthetic
data generator and creates `callcenter_analytics`. It does not create the seven data
tables or import Parquet automatically. The optional [dbt and loading overlay](DBT.md)
now provides explicit bootstrap and load commands for all seven tables. There is one
server, without replication or Keeper.

## Resources and settings

ClickHouse is capped at **3 GiB RAM and 2 CPUs**. This is the database container budget;
allow additional RAM for the OS, Docker and Tailscale (capped separately at 256 MiB /
0.5 CPU). If the whole machine has only 3 GB RAM, lower the database container and
server limits before starting. On Windows, Docker Desktop's Linux VM needs enough
memory for these containers plus its own overhead.

The checked-in baseline uses:

- 2 GiB tracked server memory, leaving 1 GiB beneath the container limit for overhead.
  The ClickHouse memory tracker is not a hard process RSS cap; Docker enforces that cap.
- 512 MiB per query, two query threads, one insert thread, at most four concurrent
  queries overall and two per user (one for ingestion).
- GROUP BY and sort spill thresholds of 128 MiB. These help those operations use disk;
  large joins and other operations can still hit the memory limit.
- 120-second query timeout, 600 seconds for ingestion, 64K-row / 16 MiB insert block
  targets and disabled parallel input parsing. Timeouts are checked during execution,
  and block targets are not hard limits on total import memory.
- 128 MiB mark cache and no uncompressed cache. Upstream merge pool defaults remain;
  their thread count does not represent a reserved CPU count.
- UTC server timezone. No business-data TTL or automatic business-data deletion.
- Seven-day query-log TTL, warning/error output through Docker with 3 x 10 MiB
  log rotation per service. High-frequency diagnostic logs are disabled. TTL cleanup
  happens during background merges, so retention is approximate.

Server settings are in `config.d/zz-small-server.xml`; query profiles and accounts
are in `users.d/accounts.xml`. Resource caps are in `compose.yaml`. Change related
limits together when resizing. Profile values are starting defaults, not a complete
quota/security policy; fuller hardening can add setting constraints.

## Networking and accounts

Tailscale runs in userspace mode, without privileged mode, a TUN device or host port
publishing. ClickHouse shares its network namespace and listens on `127.0.0.1`.
Tailscale Serve forwards tailnet TCP ports **8123** (HTTP) and **9000** (native) to
that loopback address. Funnel is not enabled. Tailnet traffic is encrypted by Tailscale;
ClickHouse TLS is not configured separately.

Tailnet grants/ACLs still control which devices can connect. Permit the intended
clients to these two ports. The configuration does not change your tailnet policy.
Since Serve is a proxy, ClickHouse sees loopback as the client address; enforce
device-level access at Tailscale and use distinct database accounts for auditability.

- `admin`: schema management and administration; created by the official image.
- `ingest`: SELECT and INSERT on `callcenter_analytics.*`; SELECT supports loader
  replay checks and row-count reconciliation.
- `reader`: SELECT on `callcenter_analytics.*` and `callcenter_dbt_prod.*`, with the read-only profile.
- `dbt`: SQL-managed by the optional analytics bootstrap; reads raw sources and manages
  objects in `callcenter_dbt_dev` and `callcenter_dbt_prod`.

The stock unauthenticated `default` user is removed by the image's named-user setup.
The ingestion and reader accounts are XML-managed; edit their configuration rather
than using SQL ALTER USER. Passwords come from `.env`, which is already ignored by
the repository. Environment secrets are visible to Docker administrators; a secret
manager is a later hardening step. Never share expanded `docker compose config` output.

## Start

Requires Docker Engine with Compose v2.20+ or Docker Desktop in Linux-container mode,
a supported ClickHouse CPU, and a Tailscale account. Images are pinned to ClickHouse
`26.8.7.19` and Tailscale `v1.102.2` for repeatable setup; update them deliberately.

From the repository root in PowerShell:

```powershell
cd infra/clickhouse
Copy-Item .env.example .env
```

On Linux/macOS, use `cp .env.example .env` instead. Edit `.env` and fill all three
passwords with distinct random values (32+ characters recommended). Single-quote
values containing `$` so Compose treats them literally. Set `TS_AUTHKEY` to a
non-ephemeral auth key from the Tailscale admin console; choose a unique `TS_HOSTNAME`
if running multiple copies. Do not commit this file. Approve the device in Tailscale
if your tailnet requires device approval.

Run the following from `infra/clickhouse`:

```text
docker compose config --quiet
docker compose pull
docker compose up -d --wait --wait-timeout 180
docker compose ps
docker compose exec tailscale tailscale status
docker compose exec tailscale tailscale serve status
```

Tailscale identity persists in `callcenter-clickhouse_tailscale-state`. After successful
enrollment, `TS_AUTHKEY` can be cleared from `.env`; `TS_AUTH_ONCE=true` reuses the saved
identity. A lost state volume or expired/revoked node identity requires enrollment again.
With an empty key on first boot, inspect `docker compose logs tailscale` for an
interactive login URL. Keep login URLs private.

The ClickHouse data volume is `callcenter-clickhouse_clickhouse-data`, including SQL
metadata and access state. Keep the Compose project name stable to reuse the volumes.
No database files are stored in the source tree. Docker Desktop stores named volumes
inside its Linux VM. Use `docker volume inspect` to inspect their location on Linux.

## Verify access

Local administrator session (prompts for the password):

```text
docker compose exec clickhouse clickhouse-client --host 127.0.0.1 --user admin --password
```

Then run:

```sql
SELECT version(), timezone();
SHOW DATABASES;
SHOW GRANTS FOR ingest;
SHOW GRANTS FOR reader;
SELECT name, value FROM system.settings
WHERE name IN ('max_threads', 'max_memory_usage', 'async_insert');
```

From another device connected to the same tailnet, with MagicDNS enabled:

```text
curl --fail --user reader "http://callcenter-clickhouse:8123/?query=SELECT%201"
clickhouse-client --host callcenter-clickhouse --port 9000 --user reader --password
```

Use `curl.exe` in Windows PowerShell if `curl` resolves to an alias. Curl prompts for
the password. Use the actual MagicDNS name shown in Tailscale or its `100.x.y.z` address
if the requested hostname was renamed or MagicDNS is unavailable. Localhost on the
Docker host is not a published database endpoint.

For a permissions/persistence smoke check, create a disposable table as admin:

```sql
CREATE TABLE callcenter_analytics.connection_check (id UInt64)
ENGINE = MergeTree ORDER BY id;
```

Connect as `ingest`, insert one row, confirm SELECT works and CREATE TABLE is denied.
Connect as `reader`, confirm the row is visible and INSERT is denied. Restart
ClickHouse, verify the row remains, then drop this disposable table as admin.
Also confirm that a client outside the tailnet cannot reach the database.

## Later ingestion of generator output

Use [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and `gim_synth/schema.py` to create explicit
types for the two facts and five dimensions. Start with MergeTree for append-only
facts. Decide sorting keys from expected filters (for example date and queue), and
whether monthly partitions are useful for the retained volume. Do not partition by
high-cardinality interaction or agent IDs. Dimensions need their own reload/update policy.

The Parquet files contain **both local wall-clock and UTC columns**. Use
`ARRIVE_TIME_UTC` / `OUTCOME_TIME_UTC` as authoritative instants, typically stored as
`DateTime64(3, 'UTC')`. Preserve local fields and the generator's configured timezone
from `_manifest.json`; do not silently interpret naive local fields as UTC. `CALL_DATE`
is a business/root-interaction date, not necessarily the UTC timestamp's calendar day.
Preserve nullable fields from the Arrow schema rather than replacing nulls with defaults.

Once the destination schema exists, send one Parquet file per request from a tailnet
client. Example, from the repository root (use `curl.exe` on Windows):

```text
curl --fail-with-body --user ingest --data-binary @out/interaction_resource_fact/call_date=2026-08-01/part-0.parquet "http://callcenter-clickhouse:8123/?query=INSERT%20INTO%20callcenter_analytics.interaction_resource_fact%20FORMAT%20Parquet"
```

Synchronous inserts are explicitly enabled (`async_insert=0`) for these already-batched
loads. For a later stream of small writes, request `async_insert=1` and keep
`wait_for_async_insert=1`. Start with one import at a time on this small server.

Do not blindly retry imports: MergeTree does not enforce row-key uniqueness. Define
run identity, file tracking and replay/replacement rules before loading repeated
generator runs. Check counts against `_manifest.json` and reader queries after loading.

## Operations and recovery

```text
docker compose logs --tail 100 clickhouse tailscale
docker compose restart clickhouse
docker compose stop
docker compose start
docker compose down
```

`down` retains named volumes; **`down -v` deletes the database and Tailscale identity**.
After changing environment variables or configuration, run `docker compose up -d`
to apply changes; use `--force-recreate` when needed. Recreate both services together
when updating Tailscale because ClickHouse shares its network namespace.

For a simple offline backup, pause ingestion and stop ClickHouse, leaving Tailscale
running. The following PowerShell commands archive the entire stopped data volume:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose stop clickhouse
docker compose run --rm --no-deps --entrypoint tar -v "${PWD}/backups:/backup" clickhouse -czf /backup/clickhouse-data.tgz -C /var/lib/clickhouse .
docker compose start clickhouse
```

Use a unique filename per backup rather than overwriting the previous archive. Check
each command's exit status. On Bash use `mkdir -p backups` and the same Docker commands.
Copy the archive off the host and store `.env` securely alongside a copy of the exact
configuration and image versions. Backups contain sensitive data and access metadata.
The local `backups/` directory is ignored by Git. A persistent volume is not a backup.

To restore, provision a separate stack with an **empty data volume**, the same
ClickHouse version/configuration/passwords, and a newly enrolled Tailscale identity.
Keep its ClickHouse service stopped. Place the archive in its `backups/` directory,
and run the same helper command with `-xzf` instead of `-czf`. Start ClickHouse and
verify grants, table counts and representative queries before switching clients.
Do not extract over a running or nonempty database. Test this process before relying
on it for important data; automated backups are a later addition.

If startup fails, first inspect logs and available memory/disk. Tailscale health only
proves it has a tailnet IP; the remote access check verifies policy and forwarding.
For insert memory failures, reduce file/batch sizes before raising limits. Disk space
must cover data, background merges and temporary spills, as well as any local backup.

Later hardening: tighter tailnet grants/tags, secret management, settings constraints,
off-host scheduled backups and tested restores, disk/memory alerts, image digest pins,
upgrade rehearsal, retention policy and optional database TLS.

## References and validation status

- [Official ClickHouse Docker setup](https://clickhouse.com/docs/get-started/setup/self-managed/docker)
- [ClickHouse settings](https://clickhouse.com/docs/reference/settings/session-settings)
- [Tailscale Docker configuration](https://tailscale.com/docs/features/containers/docker)
- [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve)

This addition was checked statically. Docker was unavailable in the authoring session,
so image pulls, server startup, account enforcement, backup/restore and tailnet access
remain to be verified on the deployment host using the checks above.
