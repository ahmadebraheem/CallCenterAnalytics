# AGENTS.md — working on this repository

Instructions and background for AI agents (and humans) picking this repo up cold. Read this
before changing anything; it records what exists, how to run it, which invariants must not
break, and why the design is the way it is.

## 1. What this repository is

A **synthetic Genesys Info Mart voice-call data generator** plus an optional analytics stack.
There is no real customer data anywhere: everything is generated from a seeded RNG, and a run is
reproducible from `seed` + config alone.

Three independent components:

| Component | Path | Status | Purpose |
|---|---|---|---|
| Generator | `gim_synth/`, `configs/`, `tests/` | core | writes Parquet: 2 fact tables + 5 dimensions |
| ClickHouse stack | `infra/clickhouse/` | optional | single-node ClickHouse reachable over Tailscale |
| dbt + loader | `analytics/` | optional | loads the Parquet into ClickHouse, 7 staging views |

The generator never depends on the other two. The other two consume its output and its schema
definitions, so **generator schema changes ripple into `analytics/`** (see §5).

## 2. Repository map

```
gim_synth/
  config.py      all configuration dataclasses + YAML loading/validation (start here, 700 lines)
  catalogue.py   resolves which LOBs / VQs exist: catalogue.mode default | manual | auto
  refdata.py     builds the 5 dimensions (sites, LOBs, VQs, agent roster, customers)
  arrivals.py    arrival model: weekday factors, intraday curve, bursts, lulls, day events
  scenarios.py   LegBuilder: every call scenario and every fact/outcome row is created here (largest file)
  schema.py      Arrow schemas: SCHEMA (resource fact), OUTCOME_SCHEMA (outcome fact)
  dictionary.py  column descriptions for all 7 tables; source of _data_dictionary.csv
  generator.py   per-day orchestration: generate -> validate -> write -> manifest
  validate.py    ~50 invariant checks run per day (abort on failure)
  writer.py      dict rows -> Arrow tables -> Parquet (partitioned or single file)
  cli.py         generate | print-config | summarize | dictionary | catalogue
configs/
  full_config.yaml        annotated reference of EVERY knob; must equal the defaults (tested)
  example.yaml            short override-only example
  catalogue_manual.yaml   catalogue.mode: manual example
  catalogue_auto.yaml     catalogue.mode: auto example
docs/
  DATA_DICTIONARY.md  the 7 tables, relationships, every column
  SCENARIOS.md        every scenario and how it is encoded (§11 = business outcomes)
  CLICKHOUSE.md       infra stack: resources, accounts, Tailscale, backup/restore
  DBT.md              dbt 2.x deployment, loader contract, timestamp/type contract
analytics/
  dbt/                project, profiles, 7 sources, 7 staging views, grain tests
  scripts/load.py     bootstrap + bounded-batch loader with a completion ledger
  scripts/generate_sources.py  regenerates catalog.json + sources/staging FROM gim_synth schemas
infra/clickhouse/     compose.yaml (+ compose.analytics.yaml overlay), server/user XML
```

## 3. Setup and commands (all verified on a clean VM)

```bash
pip install -r requirements.txt            # numpy, pyarrow, PyYAML, pytest
python -m pytest -q                        # 28 generator tests (~4 s)

python -m gim_synth generate               # default: 31 days, 30k calls/weekday -> ./out
python -m gim_synth generate --config configs/example.yaml --days 2 --out /tmp/x --quiet
python -m gim_synth print-config           # effective config (defaults + your YAML) as YAML
python -m gim_synth summarize --out ./out   # distributions, abandon %, SL %, outcome rates
python -m gim_synth dictionary [--table dim_vq] [--format csv]
python -m gim_synth catalogue --config my.yaml   # which LOBs/VQs a config resolves to
```

Analytics (needs `clickhouse-connect`, and `dbt==2.0.6` for dbt itself):

```bash
pip install clickhouse-connect==1.8.0
python -m pytest analytics/tests -q        # 7 loader contract tests, no server needed
python analytics/scripts/generate_sources.py   # idempotent; expect an empty git diff
```

Scale guidance: a 31-day default run is ~1.3 M fact rows + ~0.7 M outcome rows in ~50 s and
~1.2 s per 30k-call day. For a quick check use `--days 1 --calls-per-day 3000` and a small
`customers.pool_size`; do not generate a full month just to test a code path.

Never point `--out` at a tracked directory. The default `./out` is git-ignored.

## 4. The data contract

Seven tables, 158 columns, documented per column in `docs/DATA_DICTIONARY.md` and emitted as
`_data_dictionary.csv` on every run:

- `interaction_resource_fact` — one row per **resource leg** (agent, queue, IVR, external,
  dialer) of a voice interaction. This is the Genesys `INTERACTION_RESOURCE_FACT` grain: a call
  that queues, gets answered, is warm-transferred after a consult produces several rows linked by
  `ROOT_INTERACTION_ID` / `SEGMENT_SEQ` / `PREVIOUS_CALL_ID` / `PARENT_INTERACTION_ID`.
- `interaction_outcome_fact` — business outcomes (Sale, Saved, Cancelled, Resolved,
  PaymentTaken, NoChange …) recorded on legs where an agent spoke to a customer. One primary
  outcome per leg (equal to that leg's `DISPOSITION`) plus an optional `CrossSell` secondary.
  Join on `IRF_ID`.
- `dim_site`, `dim_lob`, `dim_vq`, `dim_agent`, `dim_customer`.

Properties that consumers rely on, so treat them as the public contract:

- **Determinism**: same seed + same config ⇒ byte-identical data.
- **Timestamps**: naive columns are local business time (`timezone`, default
  `America/New_York`); only `*_UTC` columns are instants. Never relabel a naive column as UTC.
  `CALL_DATE` is the root interaction's business date, not the UTC calendar day.
- **Timing consistency**: `ARRIVE_TIME ≤ ANSWER_TIME ≤ END_TIME ≤ ACW_END_TIME`, and
  `ANSWER_TIME ≤ OUTCOME_TIME ≤ RECORDED_TIME ≤ ACW_END_TIME` for outcomes.
- **Nullability is meaningful** (`_data_dictionary.csv` states it per column, and a test asserts
  that columns marked non-nullable really contain no nulls). Do not fill nulls with defaults.
- **Unbalanced on purpose**: a few monster VQs, a long tail of niche queues; PBR queues
  concentrate calls on high-scoring agents. Do not "fix" these into uniform distributions.

## 5. If you change X, update Y

This repo has several deliberately redundant files kept in sync by tests. Breaking one of these
links is the most likely way to make the suite fail.

| Change | Also update |
|---|---|
| A fact/outcome column (`schema.py`) | the `new_row` / `new_outcome_row` initialiser in the same file and whatever populates it in `scenarios.py`, `dictionary.py` `COLUMN_DESCRIPTIONS`, `docs/DATA_DICTIONARY.md`, an invariant in `validate.py` if one applies, then rerun `analytics/scripts/generate_sources.py` |
| A dimension column (`refdata.py`) | same as above (the dictionary is built from the live dimension schemas) |
| Any config field (`config.py`) | `configs/full_config.yaml` — `test_full_config_yaml_matches_defaults` asserts the YAML loads to **exactly** the defaults — plus its comment, the §5 table in `README.md`, and `validate()` if the field has a valid range |
| Default LOBs / VQs (`_default_lobs`, `_default_vqs`) | `configs/full_config.yaml` `lobs:` / `vqs:` blocks (same equality test), and the numbers quoted in `README.md` §6 and in the table inventory at the top of `docs/DATA_DICTIONARY.md` |
| A `catalogue.py` auto-fill rule | the `catalogue:` block in `full_config.yaml` (it documents what each `auto` resolves to), `README.md` §5.1, and the catalogue tests |
| Anything in `gim_synth/schema.py` or `dictionary.py` | `python analytics/scripts/generate_sources.py` and review the ClickHouse type diff; the loader **refuses** incompatible existing raw tables, so a deployed schema change needs a deliberate migration, not a silent regeneration |
| Scenario behaviour (`scenarios.py`) | `docs/SCENARIOS.md`, and the invariant list in `validate.py` if new states become possible |

## 6. Conventions

- **Config-driven, never hardcoded.** Anything a user might want to tune belongs in
  `config.py` with a default, a comment, and an entry in `full_config.yaml`. Probability tables
  are relative and normalised, so they need not sum to 1. Unknown YAML keys must keep raising.
- **Validation is on by default** (`output.validate: true`) and aborts the run on the first
  failing day. Add invariants for new behaviour rather than relaxing existing ones. If a new
  invariant fails, the generator is usually wrong, not the invariant.
- **Comments explain constraints and intent, not mechanics.** Do not narrate what the next line
  does, do not leave "changed this because…" notes; those belong in the commit message or PR.
- **Docstrings** on modules and non-obvious functions explain the model being simulated (why a
  log-normal, what the log-odds shift means), since the domain is the hard part here.
- Tests live in `tests/test_generator.py` (module-scoped fixture generates one small dataset for
  most tests) and `analytics/tests/`. Keep new tests fast: small day counts and customer pools.
- Python ≥ 3.9, standard library plus numpy / pyarrow / PyYAML only for the generator. Do not
  add a dependency to the generator for convenience.

## 7. Git and PR workflow

- Work on a branch off `main`; do not commit directly to `main`. Names seen here:
  `<area>/<short-desc>` (e.g. `infra/clickhouse-tailscale`) and, for Cursor cloud agents,
  `cursor/<short-desc>-<agent-suffix>`.
- One commit per logical change with a descriptive subject and a body listing what moved.
- Open a PR (draft by default) with a summary and a testing section stating what you actually
  ran. **Do not merge unless the user explicitly asks.** Merges so far have all been merge
  commits (no squash, no rebase), so history keeps the individual commits.
- Never force-push or amend already-pushed commits.
- Secrets: `infra/clickhouse/.env` is git-ignored and must stay that way. No passwords in Git,
  in Compose files, or in build arguments. Do not paste expanded `docker compose config` output.

## 8. History and design decisions

Chronological, with the reasoning, so you do not re-litigate settled choices:

1. **Generator core** (PR #1). Requirements were: a month of data (extensible), ≥30k calls/day,
   realistic randomness, every call-centre scenario (abandons, short/long calls, consults,
   transfers, conferences, RONA, callbacks, outbound, internal, direct DID), Parquet output, full
   Markdown documentation. Grain was chosen by the user as **interaction resource fact** (one row
   per leg), with parent keys and `SEGMENT_SEQ` for transfers/consults. Voice only.
2. **Bursty traffic.** Explicit requirement that "at one moment a billion calls come in and the
   other barely any": hence `bursts` (including a `mega_prob` flash-crowd), `lulls`, and
   `day_events` on top of the weekday/intraday curve. Keep these tunable to zero for smooth
   textbook days.
3. **Unbalanced VQs.** Also explicit: three monster queues carry ~60 % of inbound, then mid-size,
   then a long tail of niche queues (a few dozen calls/day). `vq_weights` exists to reshape the
   split without re-declaring the list.
4. **PBR (Predictive Behavioural Routing).** Requested because a PBR queue skews which agents get
   calls. Implemented as per-VQ `pbr_enabled` / `pbr_skew` (agent weight = exp(skew × agent
   quality z-score)) / `pbr_premium_boost` for VIP/Enterprise, surfaced as `ROUTING_METHOD` and
   `PBR_SCORE`. The same latent agent quality also drives outcomes (point 6), which is why good
   agents both receive more PBR calls and convert better.
5. **`full_config.yaml` + per-VQ knobs** (PR #2, part 1). Request: "knobs for all the various
   parameters to tune the data like L1/L2 abandons, PBR". Every parameter is now documented in
   one annotated file that loads to exactly the defaults, plus per-queue overrides
   (`short_abandon_prob`, `abandon_while_ringing_prob`, `rona_prob`, `ivr_contained_prob`,
   `wait_scale`) and the `vq_overrides` shortcut.
6. **Business outcomes** (PR #2, part 2). Request: outcomes alongside the calls with matching
   timestamps and agents — sales, retention, no change. Modelled as a **separate fact table**
   rather than extra columns, because outcomes are a different grain (a leg can have a primary
   plus a cross-sell) and this mirrors how Genesys separates call events from business results.
   `LOBConfig.dispositions` was replaced by `business_outcomes`, so a leg's `DISPOSITION` is the
   primary `BUSINESS_RESULT` — one source of truth. Probabilities bend in log-odds space by agent
   quality (`agent_lift`), customer segment (`segment_lift`) and queue wait
   (`wait_penalty_per_min`); short calls never sell.
7. **Data dictionary** (PR #2, part 3). Request: how many tables, and dictionaries for them.
   Answer: 7 tables / 158 columns. Descriptions live in code (`dictionary.py`), types come from
   the live Arrow schemas, so the dictionary cannot drift; it is written as `_data_dictionary.csv`
   on every run, printable via the CLI, and mirrored in `docs/DATA_DICTIONARY.md`.
8. **Catalogue modes** (PR #3). Request: define in YAML which queues you want and how many VQs,
   with `auto` and manual options plus comments telling you what to enter. Hence
   `catalogue.mode: default | manual | auto`, resolved on the plain dict before dataclasses are
   built so `default` stays byte-identical (tested). In `manual`, `{name, lob}` is the only
   requirement and every other field may be omitted or set to `auto`; in `auto`, `n_vqs` + `lobs`
   (+ shares/skew/PBR/hours shares) design the whole catalogue with deterministic names. Library
   LOBs bring realistic behaviour; unknown LOB names get generic behaviour.
9. **ClickHouse over Tailscale** (PR #4) and **dbt 2.x + Parquet loader** (PR #5), added by
   another contributor. Notable constraints: dbt **v2** with its bundled ClickHouse adapter (do
   not install `dbt-core`/`dbt-clickhouse` v1, and do not use the v1 `dbt docs generate`
   workflow); the loader is idempotent per `--dataset-id` via a completion ledger, and staging
   views expose only completed loads; naive local timestamp columns are stored as **strings** in
   ClickHouse to avoid a false UTC label.

## 9. Gotchas

- `configs/full_config.yaml` is not decoration: it is asserted equal to the defaults. Adding a
  config field without adding it there fails the suite.
- Generated Parquet is large; `out/`, `*.parquet` and dbt artefacts are git-ignored. Never commit
  generated data.
- `catalogue.mode: manual|auto` drops the built-in `lobs`/`vqs` before merging your YAML; in
  `default` mode a supplied list replaces the built-in one wholesale (use `vq_weights` /
  `vq_overrides` for nudges).
- The `analytics/` loader requires exactly the 7 expected files/schemas and reconciles counts
  against `_manifest.json`; a generator schema change invalidates already-loaded raw tables.
- Docker is not available in every agent session, so the infra stack usually cannot be verified
  end-to-end. Say so instead of implying it was tested (`docs/CLICKHOUSE.md` records its own
  verification status).
- The Parquet output carries both local and UTC columns for the same event. Analytics code must
  pick `*_UTC` for instants and preserve `_source_timezone`.
