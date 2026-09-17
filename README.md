# CallCenterAnalytics – synthetic Genesys Info Mart data generator

`gim_synth` produces realistic, fully synthetic **voice** call-centre data shaped like the Genesys
Info Mart `INTERACTION_RESOURCE_FACT` table (one row per resource "leg" of an interaction), written
as **Parquet**, one partition per day, with the supporting dimension tables.

It is meant for building and testing reporting, analytics, forecasting and data-engineering work
without touching production data.

* Any date range (a month by default, a year works the same way), configurable timezone
* 30 000+ interactions per weekday by default, ~1.3 M fact rows for a 31-day month in under a minute
* Realistic arrival patterns **plus** extreme behaviour: flash-crowd bursts where thousands of calls
  hit in a few minutes, and lulls where the phones go dead
* Queue dynamics that react to load: waits, abandons, service level and callback offers all degrade
  when arrivals exceed staffed capacity and recover afterwards
* Every common contact-centre scenario: normal / short / long calls, multiple holds, short abandons,
  queue abandons, abandon while ringing, RONA, overflow, IVR containment, after-hours, voicemail,
  callback requested and fulfilled, blind / warm / external / multi-hop transfers, consults,
  conferences, system drops, outbound manual, dialer campaigns (incl. dialer drops), internal
  agent-to-agent calls, direct DID calls
* Unbalanced queues (monster VQs plus a long tail of niche ones) and **Predictive Behavioural
  Routing** on the sales/retention queues, so call distribution across agents is lopsided
  (`ROUTING_METHOD`, `PBR_SCORE`)
* Full lineage keys for multi-leg interactions: `INTERACTION_ID`, `ROOT_INTERACTION_ID`,
  `PARENT_INTERACTION_ID` / `PARENT_CALL_ID` (consults), `RELATED_INTERACTION_ID` (callbacks),
  `PREVIOUS_CALL_ID` and a tree-wide `SEGMENT_SEQ`
* Deterministic for a given seed; every row is checked against ~35 invariants before it is written

Documentation:

| Document | Contents |
|---|---|
| this README | concepts, quick start, configuration, model description, output layout |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | every column of the fact table and the dimension tables |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | every scenario the generator produces and exactly how it is encoded |

---

## 1. Quick start

```bash
pip install -r requirements.txt          # numpy, pyarrow, PyYAML (pytest for tests)

# one month (31 days from 2026-08-01), 30k baseline calls per weekday, ./out
python -m gim_synth generate

# custom range / volume / destination
python -m gim_synth generate --start 2026-01-01 --days 90 --calls-per-day 45000 --seed 7 --out ./q1

# override anything via YAML (see configs/example.yaml)
python -m gim_synth generate --config configs/example.yaml

# show the full effective configuration (all knobs, with defaults)
python -m gim_synth print-config

# quick statistics of a generated dataset
python -m gim_synth summarize --out ./out

python -m pytest -q                       # tests
```

`generate` options: `--config`, `--start`, `--days`, `--calls-per-day`, `--seed`, `--timezone`,
`--out`, `--compression` (zstd default), `--single-file` (one parquet file instead of daily
partitions), `--no-validate`, `--quiet`.

Python API:

```python
from gim_synth import load_config, generate

cfg = load_config("configs/example.yaml", overrides={"days": 7, "output": {"directory": "./week"}})
stats = generate(cfg, log=print)
```

---

## 2. Output layout

```
out/
├── interaction_resource_fact/
│   ├── call_date=2026-08-01/part-0.parquet      # hive-style partition per CALL_DATE
│   ├── call_date=2026-08-02/part-0.parquet
│   └── ...
├── dim_site.parquet
├── dim_lob.parquet
├── dim_vq.parquet                                # queues / virtual queues / route points / DNIS
├── dim_agent.parquet                             # roster incl. shift, skills, days off
├── dim_customer.parquet                          # customer ids, ANI, segment
└── _manifest.json                                # effective config, per-day stats & events, counts
```

Read it with anything that speaks Parquet, e.g.

```python
import pyarrow.dataset as ds
t = ds.dataset("out/interaction_resource_fact", format="parquet", partitioning="hive").to_table()
```

The `_manifest.json` lists for each day the base volume, the realised arrivals, legs, interactions,
abandon %, service-level %, peak calls-per-minute and the random **events** that shaped the day
(bursts, mega bursts, lulls, spike/quiet days) so you can find the interesting days quickly.

---

## 3. Grain: INTERACTION_RESOURCE_FACT

One row = one **resource participation** in a call, the same grain as Genesys Info Mart's
`INTERACTION_RESOURCE_FACT` (IRF):

* a customer call answered by one agent → 1 row
* a call abandoned in queue → 1 row (resource type `Queue`, no agent)
* a call contained in the IVR / after-hours announcement → 1 row (resource type `IVR`)
* a call answered then blind-transferred to another queue and answered there → 2 rows
* a warm transfer → 3 rows: first agent, the consult leg (its own `INTERACTION_ID`, `CALL_TYPE=Consult`,
  pointing back via `PARENT_INTERACTION_ID` / `PARENT_CALL_ID`), the receiving agent
* a conference → 3 rows: initiator, the short consult, the joined agent (`RESOURCE_ROLE=ConferenceJoined`)
* an internal agent-to-agent call → 2 rows (initiator and receiver)
* a callback → the original inbound leg (`CALLBACK_REQUESTED_FLAG=1`) plus, later, a new outbound
  interaction (`CALLBACK_FLAG=1`, `RELATED_INTERACTION_ID` = the original)

### Lineage keys

| Column | Meaning |
|---|---|
| `IRF_ID` | surrogate key of the row |
| `CALL_ID` | Genesys ConnID-style 16-hex id of this leg; unique per row |
| `INTERACTION_ID` | id of the interaction this leg belongs to. Transfers, conferences and RONA legs share the customer interaction id. Consults get their **own** interaction id |
| `ROOT_INTERACTION_ID` | id of the customer interaction at the root of the tree – group by this to get the whole call including consults |
| `PARENT_INTERACTION_ID`, `PARENT_CALL_ID` | on consult legs only: the customer interaction and the specific leg that initiated the consult |
| `RELATED_INTERACTION_ID` | on callback legs only: the inbound interaction in which the callback was requested |
| `PREVIOUS_CALL_ID` | the `CALL_ID` of the leg that led to this one (previous agent for transfers, the initiating agent leg for consults/conferences, the initiator row for internal calls) |
| `SEGMENT_SEQ` | 1-based position of the leg inside the **root interaction tree**, in the order the legs were created (root=1, consult=2, transferred-to agent=3, ...) |
| `TRANSFER_COUNT` | number of transfers that happened before this leg started |

`CALL_DATE` is the date of the root interaction so an entire tree always lives in one partition
even when late legs spill past midnight (their `ARRIVE_TIME` is on the next day).

---

## 4. How the data is generated

### 4.1 Reference data (`gim_synth/refdata.py`)

* **Sites** – Dallas, Phoenix, Toronto (onshore) and Manila (offshore, takes most night shifts).
* **LOBs** – Sales, Retention, CustomerService, TechSupport, Billing, Collections. Each LOB has its
  own disposition codes, outcome mix (how often calls transfer / consult / conference ...) and
  transfer targets.
* **VQs** – 18 virtual queues with a deliberately **unbalanced** volume split, like a real centre:
  three monster queues carry ~60 % of inbound traffic (`VQ_CustServ_General` 27 %, `VQ_Sales_New`
  19 %, `VQ_Tech_Tier1` 15 %), five mid-size queues 3–10 % each (`VQ_Billing_Payments`,
  `VQ_Retention_Cancel`, `VQ_Sales_Upgrade`, `VQ_CustServ_Account`, `VQ_Retention_Save_Offers`),
  four small ones 0.8–2 % (`VQ_Tech_Tier2`, `VQ_Collections_Inbound`, `VQ_Billing_Disputes` Mon-Fri,
  `VQ_Retention_Loyalty`) and a long tail of niche queues with a few dozen calls a day
  (`VQ_Sales_Spanish`, `VQ_Tech_Enterprise`, `VQ_Retention_VIP`, `VQ_CustServ_Accessibility`,
  the two overflow queues). Each VQ has an ACD queue DN, route point, DNIS, skill, opening
  hours/days, talk-time distribution, hold probability, ACW, base ASA, customer patience,
  service-level threshold and an optional overflow VQ. Because staffing is derived per queue, the
  big queues absorb the bursts (and suffer the abandons) while the small, multi-skilled-covered
  queues run at high service levels – change `vqs[].weight` to reshape the split.
* **Routing method** – each VQ is either classic ACD (uniform pick among eligible agents) or
  **PBR** (`pbr_enabled`, default on the sales and retention queues): agents are picked with
  probability ∝ exp(`pbr_skew` × quality), raised to `pbr_premium_boost` for VIP/Enterprise
  customers. Result: on PBR queues a minority of agents handle most calls while others starve;
  `ROUTING_METHOD` and `PBR_SCORE` on every leg make the effect analysable
  (see docs/SCENARIOS.md §8c).
* **Agents** – the roster is *sized from the workload*: expected daily calls per VQ × AHT ÷
  occupancy ÷ shift length, spread across shift starts proportional to the forecast intraday
  volume, with two days off per agent (weekends more likely). Each agent has a site, team, tenure
  band, primary skill (+ secondary skill within the LOB with 35% probability), a personal speed
  factor that scales talk and ACW time, and a PBR score (percentile rank of a tenure-shifted latent
  quality) used by PBR queues. Every open hour of every VQ is guaranteed at least
  `min_agents_per_open_hour` eligible agents.
* **Customers** – a pool (400k by default) with a skewed pick distribution so some customers call
  repeatedly (`REPEAT_CALL_7D_FLAG`, `FIRST_CALL_FLAG`), each with an ANI and a segment
  (Consumer / SMB / Enterprise / VIP).

### 4.2 Arrival model (`gim_synth/arrivals.py`)

1. **Daily volume** = `calls_per_day` × weekday factor (Mon 1.18 … Sun 0.38) × log-normal noise,
   optionally × a *spike day* (1.5–3×, e.g. bill run / outage) or *quiet day* (0.15–0.5×, holiday).
2. **Intraday curve** – mixture of gaussians (peaks ~10:30 and ~14:30, lunch dip) over a small
   night floor, with per-minute gamma jitter.
3. **Bursts** – Poisson number of gaussian bumps per day, each dumping 3–25 % of the day's volume
   into a 3–25 minute window; plus a `mega_prob` chance of a **flash crowd**: 0.5–1.5× the whole
   day's volume inside ~4 minutes (thousands of calls per minute).
4. **Lulls** – Poisson number of windows (10–60 min) where intensity drops to 0–10 % (carrier
   outage, IVR down).
5. Realised arrivals per minute are Poisson draws from the resulting intensity; arrivals are spread
   uniformly inside their minute.

All of this is configurable; set `bursts.rate_per_day: 0` and `mega_prob: 0` for smooth data.

### 4.3 Queue / staffing model (`expected_wait_curve`)

For every VQ and minute of the day:

* offered load = trailing-window (12 min) mean of arrivals into the VQ
* capacity = eligible agents on shift × 60 / AHT × target occupancy
* ρ = offered / capacity; expected wait = base ASA while ρ < knee, then grows exponentially,
  capped at 30 min; a backlog term makes the wait decay gradually after a burst
* each call draws a **wait** (gamma around the expected wait) and a **patience** (log-normal per
  VQ). Patience < wait ⇒ abandon (short abandon when under 5 s), otherwise the call is answered
  after `QUEUE_TIME = wait` and a ring time.

This is what makes bursts hurt: a flood of calls pushes ρ far above 1, waits explode, abandon rate
and callback offers jump, service level collapses, then everything recovers as the window drains.
Staffing follows the *forecast* curve, never the bursts – exactly like a real centre.

### 4.4 Scenario engine (`gim_synth/scenarios.py`)

Each arrival is assigned an interaction type (inbound 86 %, outbound manual 4 %, dialer 4 %,
internal 3 %, direct DID 3 %). Inbound calls go through IVR → (self-service | after-hours |
queue) → (callback offer | abandon | RONA | abandon-while-ringing | answered) → an LOB-specific
**outcome** (normal, short, long, multi-hold, system drop, blind transfer, warm transfer, consult,
conference, multi-hop, external transfer). Transfers recurse into the target VQ's queue model, so
a transferred call can itself be abandoned, overflowed or transferred again (depth-limited).
See [docs/SCENARIOS.md](docs/SCENARIOS.md) for the complete list and how each is encoded.

### 4.5 Timing model

Per leg the durations are drawn first and the timestamps derived from them, so they always
reconcile:

```
ARRIVE_TIME
  + IVR_TIME + QUEUE_TIME + RING_TIME  = ANSWER_TIME      (null when not answered)
  + TALK_TIME + HOLD_TIME              = END_TIME
  + ACW_TIME                           = ACW_END_TIME     (null when ACW_TIME = 0)
DURATION    = END_TIME - ARRIVE_TIME   (seconds; excludes ACW)
HANDLE_TIME = TALK_TIME + HOLD_TIME + ACW_TIME
```

Talk time is log-normal per VQ (heavy right tail, capped at 2 h for the "long call" scenario),
scaled by the agent's speed factor; holds are gamma; ACW is gamma; ring 2–20 s.
Times are stored to the second. `ARRIVE_TIME` etc. are naive local business time,
`ARRIVE_TIME_UTC` is the UTC instant (computed from the day's midnight offset; on a DST switch day
legs after the switch are off by one hour).

### 4.6 Validation (`gim_synth/validate.py`)

Before a day is written, ~35 invariants are asserted on the Arrow table (unique ids, timestamp
ordering, `DURATION`/`HANDLE_TIME` arithmetic, abandon/answer exclusivity, lineage rules such as
"`SEGMENT_SEQ`>1 ⇒ `PREVIOUS_CALL_ID` set" and "`PARENT_INTERACTION_ID` only on Consult legs",
resource semantics, service-level flag consistency). A violation aborts the run with the failing
checks listed (`--no-validate` skips this).

---

## 5. Configuration

**[`configs/full_config.yaml`](configs/full_config.yaml)** is the complete, annotated reference:
every knob with its default value, a one-line explanation and a tuning cheat-sheet at the top
(abandons, bursts, PBR, L1/L2 tech support, transfers, volume split). It loads to exactly the
defaults (guarded by a test), so copy it and change what you need. `configs/example.yaml` is a
shorter override-only example.

`python -m gim_synth print-config` dumps the effective configuration (defaults + your file) as
plain YAML. A YAML passed with `--config` is deep-merged over the defaults; CLI flags win over
both. Lists (`sites`, `lobs`, `vqs`) are replaced wholesale when present. Unknown keys raise an
error.

| Section | Purpose |
|---|---|
| `start_date`, `days`, `timezone`, `seed`, `calls_per_day`, `weekday_factors`, `daily_noise_sigma`, `short_abandon_threshold_s` | run scope and daily volume |
| `intraday` | peaks (hour, sigma, weight), night floor, per-minute jitter |
| `bursts`, `lulls`, `day_events` | extreme behaviour (see 4.2) |
| `mix` | share of inbound / outbound manual / dialer / internal / direct DID |
| `ivr` | IVR time distribution, containment rate, after-hours voicemail share |
| `inbound` | short-abandon and abandon-while-ringing probability, RONA, overflow, callback offer/accept/connect rates, max transfer depth, after-hours leakage |
| `queue_model` | occupancy target, load window, knee, growth, max wait, backlog decay, wait shape |
| `staffing` | occupancy, roster factor, shift length, secondary skill rate, minimum agents per open hour |
| `outbound` | manual and dialer result mixes, campaigns, internal / DID answer rates |
| `customers` | pool size, repeat-caller skew, segment mix |
| `output` | directory, compression, partitioning, dimensions, validation |
| `sites`, `lobs`, `vqs` | the reference model – add / rename / retune queues and LOBs here; per VQ: hours, AHT, holds, ACW, base ASA, patience, SL threshold, overflow, `pbr_enabled` / `pbr_skew` / `pbr_premium_boost`, and optional per-queue overrides `short_abandon_prob`, `abandon_while_ringing_prob`, `rona_prob`, `ivr_contained_prob`, `wait_scale` |
| `vq_weights` | shortcut `{VQ name: relative weight}` to reshape the volume split without re-declaring `vqs` |
| `vq_overrides` | shortcut `{VQ name: {field: value}}` to change any per-VQ field (e.g. `pbr_enabled`, hours, AHT) without re-declaring `vqs` |

Examples:

```yaml
# heavier chaos
bursts: {rate_per_day: 3, max_frac: 0.5, mega_prob: 0.2, mega_max_frac: 3.0}
lulls:  {rate_per_day: 2, min_intensity: 0.0, max_intensity: 0.02}

# smooth textbook days
bursts: {rate_per_day: 0, mega_prob: 0}
lulls:  {rate_per_day: 0}
day_events: {spike_day_prob: 0, quiet_day_prob: 0}

# understaffed centre
staffing: {roster_factor: 0.8}

# make one queue even more dominant and starve a niche one
vq_weights: {VQ_CustServ_General: 0.4, VQ_Retention_VIP: 0.0005}

# turn PBR on/off or retune any per-VQ field without re-declaring the vqs list
vq_overrides:
  VQ_Tech_Tier1: {pbr_enabled: true, pbr_skew: 1.0}     # 0 = uniform, 1.2 = extreme concentration
  VQ_Sales_New: {pbr_enabled: false}
  VQ_Billing_Payments: {close_hour: 23, service_level_s: 30}

# L1 vs L2 tech support: impatient L1 callers with more short abandons and RONA,
# patient L2 callers on a slow, long-AHT queue
vq_overrides:
  VQ_Tech_Tier1: {patience_median_s: 90, short_abandon_prob: 0.03, rona_prob: 0.04}
  VQ_Tech_Tier2: {patience_median_s: 400, wait_scale: 1.5, talk_median_s: 900, service_level_s: 90}

# one big file, no validation, gzip
output: {partition_by_day: false, validate: false, compression: gzip}
```

Changing `days` is all that is needed to go beyond a month; callbacks that fall after the last
generated day are dropped (count reported in the manifest). Memory is flat because one day is
generated, validated and written at a time; runtime is roughly 1.2 s per 30k-call day.

---

## 6. Typical numbers (defaults, seed 42, Aug 2026)

* ~1.3 M fact rows / ~1.05 M interactions for 31 days, ~90 MB zstd Parquet
* normal weekdays: 30–45k interactions, abandon 2–8 %, service level 60–77 %
* burst / spike days: 80–140k interactions, peaks of 4–8k calls per minute, abandon 30–45 %,
  service level 20–40 %, thousands of callback requests
* quiet days / weekends: 7–20k interactions
* VQ split (inbound legs, typical week): `VQ_CustServ_General` 28 %, `VQ_Tech_Tier1` 16 %,
  `VQ_Sales_New` 15 %, `VQ_Billing_Payments` 10 %, `VQ_Retention_Cancel` 8 % … down to
  `VQ_Retention_VIP` 0.3 % and `VQ_CustServ_Accessibility` 0.1 % (≈35 calls/day)

* Agent load: ACD queues top-decile/bottom-decile ≈ 10×; PBR queues ≈ 30×, busiest agent 1 200+
  calls a week while the least-favoured get a handful

Run `python -m gim_synth summarize --out ./out` to see the distribution of results, resource
roles, transfer types, LOBs, sites, the per-VQ volume / abandon / service-level table and the
ACD-vs-PBR agent load comparison for your own output.

---

## 7. Known simplifications

* Agent concurrency is not enforced (an agent may appear on overlapping legs); staffing acts
  through the queue model, not by blocking individual agents.
* Transfer inflow is approximated in roster sizing (+12 %) rather than fed back into the target
  VQ's load curve.
* `ARRIVE_TIME_UTC` uses the day's midnight offset (DST switch days are off by an hour after the switch).
* Surrogate keys (`IRF_ID`, `INTERACTION_ID`) are simple counters; `CALL_ID` is a random 64-bit hex.
* Only the `voice` media type is generated.
