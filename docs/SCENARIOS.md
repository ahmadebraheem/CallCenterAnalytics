# Scenarios

Every leg carries a `SCENARIO` label so a dataset can be sliced by the situation that produced it.
This document lists all scenarios, when they occur and how they are encoded in the fact table.
Probabilities are the defaults; all are configurable (`python -m gim_synth print-config`).

Notation: `→` a new leg; ⟨same interaction⟩ means the leg shares `INTERACTION_ID` with the root,
⟨own interaction⟩ means it has its own `INTERACTION_ID` with `PARENT_INTERACTION_ID` set.

## 1. Interaction types (per arrival)

| Type | Share | Entry point |
|---|---|---|
| Inbound customer call to a VQ | 86 % | §2 → §3 → §4 |
| Outbound manual (agent dials) | 4 % | §6 |
| Outbound dialer campaign | 4 % | §6 |
| Internal agent-to-agent | 3 % | §7 |
| Direct DID call to an agent | 3 % | §7 |
| Callback (outbound) | derived from callback requests | §5 |

## 2. Inbound – before the queue

| Scenario | When | Encoding |
|---|---|---|
| `ivr_self_service` | 8 % of inbound: the customer completes in the IVR | `RESOURCE_TYPE=IVR`, `CALL_RESULT=SelfService`, `IVR_TIME` 30–100 s, no VQ, `ROUTE_POINT`/`DNIS` set, `N_AGENT=0`, `N_CUSTOMER=1` |
| `after_hours` | Call to a VQ outside its opening hours/days (closed VQs receive 4 % of their normal weight) | `RESOURCE_TYPE=IVR`, `CALL_RESULT=AfterHours`, VQ fields set, `IVR_TIME` = IVR + announcement |
| `after_hours_voicemail` | 35 % of after-hours calls leave a voicemail | `CALL_RESULT=Voicemail`, `TECHNICAL_RESULT=Diverted`, `IVR_TIME` includes message length |

Every inbound call spends an IVR time (normal, mean 22 s, min 3 s) before queuing; it is stored in
`IVR_TIME` of the root leg.

## 3. Inbound – in the queue

The wait is drawn from the VQ's expected-wait curve for that minute (load driven, see README
§4.3) and compared with the customer's patience.

| Scenario | When | Encoding |
|---|---|---|
| `callback_requested` | Expected wait ≥ 240 s and customer accepts (35 %) | `RESOURCE_TYPE=Queue`, `CALL_RESULT=CallbackRequested`, `TECHNICAL_RESULT=Diverted`, `CALLBACK_REQUESTED_FLAG=1`, `QUEUE_TIME` 15–60 s, `SERVICE_LEVEL_FLAG` null. Schedules §5. |
| `abandon_short` | Patience < 5 s (1.5 % forced + natural tail) | `RESOURCE_TYPE=Queue`, `CALL_RESULT=ShortAbandon`, `ABANDON_FLAG=1`, `SHORT_ABANDON_FLAG=1`, `SERVICE_LEVEL_FLAG` null |
| `abandon_queue` | Patience < wait | `RESOURCE_TYPE=Queue`, `CALL_RESULT=Abandoned`, `TECHNICAL_RESULT_REASON=AbandonedWhileQueued`, `ABANDON_FLAG=1`, `QUEUE_TIME` = time waited, `SERVICE_LEVEL_FLAG=0`, `DISCONNECT_REASON=Customer` |
| overflow (flag, not a separate leg) | Wait > 120 s, VQ has an overflow target, 60 % | The leg is re-homed to the overflow VQ: `VQ_NAME`=overflow queue, `ORIGINAL_VQ_NAME`=original, `OVERFLOW_FLAG=1`, `ROUTE_POINT`/`DNIS` stay those of the original. The call then abandons or is answered in the overflow queue. |
| `rona` | 2 % of answered-to-be calls: the selected agent does not pick up | `RESOURCE_TYPE=Agent` with the alerted agent, `CALL_RESULT=RONA`, `TECHNICAL_RESULT=Redirected`, `RING_TIME=25`, `ANSWERED_FLAG=0`, `RONA_FLAG=1`, `SERVICE_LEVEL_FLAG=0`. → next leg ⟨same interaction⟩ re-queued with priority (`PREVIOUS_CALL_ID` = RONA leg). |
| `abandon_after_rona` | Customer gives up during the re-queue | As `abandon_queue`, `SEGMENT_SEQ=2` |
| `abandon_ringing` | 0.6 %: customer drops while the agent phone rings | `RESOURCE_TYPE=Agent`, `CALL_RESULT=Abandoned`, `TECHNICAL_RESULT_REASON=AbandonedWhileRinging`, `ABANDON_FLAG=1`, `N_AGENT=1`, `RING_TIME` partial |

Otherwise the call is answered (`QUEUE_TIME` = wait, `RING_TIME` 2–20 s) and an outcome from §4
is drawn using the LOB's `outcome_weights`.

All of the probabilities above are global (`inbound.*`, `ivr.contained_prob`) but can be overridden
per queue with `vqs[].short_abandon_prob`, `abandon_while_ringing_prob`, `rona_prob`,
`ivr_contained_prob`; `vqs[].wait_scale` multiplies every drawn wait for that queue (a cheap way to
make one queue – e.g. L2 tech support – consistently slower and more abandon-prone). See
`configs/full_config.yaml`.

## 4. Inbound – answered outcomes

Common encoding of the answered agent leg: `RESOURCE_TYPE=Agent`, `RESOURCE_ROLE=Received`
(`ReceivedTransfer` when it arrived via a transfer), `ANSWERED_FLAG=1`, `N_AGENT=1`, `N_CUSTOMER=1`,
`SERVICE_LEVEL_FLAG` = 1/0 against the VQ threshold, `DISPOSITION` = the primary business outcome
(§11, also written to `interaction_outcome_fact`), `DISCONNECT_REASON` Customer (65 %) / Agent
(35 %) unless stated.

| Scenario | Typical LOB weight | Behaviour | Encoding specifics |
|---|---|---|---|
| `normal` | 48–60 % | Talk log-normal around the VQ median × agent speed; one hold with the VQ's hold probability; ACW gamma | `CALL_RESULT=Answered` |
| `short` | 4–10 % | Talk 4–30 s (wrong number, quick FAQ), little ACW | `CALL_RESULT=Answered`, mostly `DISCONNECT_REASON=Customer` |
| `long` | 2–9 % | Talk log-normal around 30 min (cap 2 h), 1–4 holds, 1.5× ACW | `CALL_RESULT=Answered` |
| `multi_hold` | 6–14 % | Talk × 1.3, 2–5 holds | `HOLD_COUNT` 2–5 |
| `system_drop` | 2 % | Call drops after 5–120 s | `CALL_RESULT=SystemError`, `TECHNICAL_RESULT=Failed`, `DISCONNECT_REASON=System`, no disposition |
| `blind_transfer` | 5–9 % | Agent talks ~55 % of a normal call then cold-transfers. 75 % → another VQ (queue model applies again: the transferred call can abandon, overflow, RONA or transfer again), 25 % → directly to an agent | Leg: `CALL_RESULT=Transferred`, `TRANSFER_FLAG=1`, `TRANSFER_TYPE=Blind`, `TRANSFER_TO` = VQ name or agent id, `DISCONNECT_REASON=Transfer`, `DISPOSITION=Transferred`. → receiving leg(s) ⟨same interaction⟩ with `TRANSFER_IN_FLAG=1`, `RESOURCE_ROLE=ReceivedTransfer`, `TRANSFER_COUNT`+1, `PREVIOUS_CALL_ID` = transferring leg |
| `blind_transfer_to_agent` | (child of above) | Direct ring to an agent; 92 % answered, 8 % customer hangs up while ringing | Answered: `Answered`; else `Abandoned` / `AbandonedWhileRinging`, `ABANDON_FLAG=1`. No VQ fields, `DNIS` = agent DID |
| `transfer_to_closed_vq` | (child, rare) | Transfer target closed and no 24x7 queue available | `RESOURCE_TYPE=IVR`, `CALL_RESULT=AfterHours`, `TRANSFER_IN_FLAG=1` |
| `warm_transfer` | 4–7 % | Agent puts the customer on hold, consults an agent in the target VQ (~75 s), then completes the transfer | Initiating leg: `CALL_RESULT=Transferred`, `TRANSFER_FLAG=1`, `TRANSFER_TYPE=Warm`, `TRANSFER_TO` = receiving agent, `CONSULT_FLAG=1`, `HOLD_TIME` includes the consult. → `warm_transfer_consult` ⟨own interaction⟩ then → `warm_transfer_received` ⟨same interaction⟩ |
| `warm_transfer_consult` | (child) | The consult call between the two agents | `CALL_TYPE=Consult`, `RESOURCE_ROLE=ReceivedConsult`, `CALL_RESULT=Consulted`, `PARENT_INTERACTION_ID`/`PARENT_CALL_ID` = initiating interaction/leg, `PREVIOUS_CALL_ID` = initiating leg, `CONSULT_RECEIVED_FLAG=1`, `N_AGENT=2`, `N_CUSTOMER=0`, `ANI` = initiating agent id, `DNIS` = consulted agent DID, no customer fields |
| `warm_transfer_received` | (child) | The consulted agent takes the customer | `RESOURCE_ROLE=ReceivedTransfer`, `TRANSFER_IN_FLAG=1`, `TRANSFER_TYPE=Warm`, `QUEUE_TIME=RING_TIME=0` (already connected), `ANSWER_TIME=ARRIVE_TIME`, `SERVICE_LEVEL_FLAG` null, `TRANSFER_COUNT`+1 |
| `consult_only` | 4–8 % | Agent consults a colleague (same VQ 50 % / target VQ 50 %) and returns to the customer | Agent leg: `CONSULT_FLAG=1`, `HOLD_TIME` includes the consult, `CALL_RESULT=Answered`. → consult leg ⟨own interaction⟩ as above with `SCENARIO=consult_only` |
| `conference` | 1–2 % | Agent consults briefly (20–60 s) then conferences the second agent in for ~4 min | Initiator: `CALL_RESULT=Conferenced`, `TECHNICAL_RESULT=Conferenced`, `CONFERENCE_FLAG=1`, `CONSULT_FLAG=1`, `N_AGENT=2`, `TALK_TIME` includes the conference. → `conference_consult` ⟨own interaction⟩ → `conference_joined` ⟨same interaction⟩ |
| `conference_joined` | (child) | The second agent in the conference | `RESOURCE_ROLE=ConferenceJoined`, `CALL_RESULT=Answered`, `TECHNICAL_RESULT=Conferenced`, `CONFERENCE_FLAG=1`, `N_AGENT=2`, `N_CUSTOMER=1`, `QUEUE_TIME=RING_TIME=0` |
| `multi_hop` | 1–2 % | 2–3 consecutive blind transfers ("bounced customer") | Encoded as a chain of `blind_transfer` legs; `TRANSFER_COUNT` reaches 2–3. Depth is capped by `max_transfer_depth` (3) after which the leg completes normally |
| `external_transfer` | 1–4 % | Agent transfers to an outside number (partner, government line…) | Leg: `Transferred`, `TRANSFER_TYPE=External`, `TRANSFER_TO` = external number. → `external_transfer_leg` |
| `external_transfer_leg` | (child) | The external party | `RESOURCE_TYPE=External`, no agent / VQ, `N_AGENT=0`, `N_CUSTOMER=1`, `CALL_RESULT=Completed` (90 %) or `NoAnswer`, `TRANSFER_IN_FLAG=1`, `DNIS` = external number |

## 5. Callbacks

| Scenario | When | Encoding |
|---|---|---|
| `callback_outbound` | 1–2.2× the expected wait (+60 s) after a `callback_requested` leg; may land on the next day; dropped if after the generated period (count in manifest) | New interaction: `CALL_TYPE=Outbound`, `RESOURCE_ROLE=Initiated`, `CALLBACK_FLAG=1`, `RELATED_INTERACTION_ID` = the inbound interaction, VQ fields of the original queue, `ANI` = VQ DNIS, `DNIS` = customer ANI. Result `Answered` 75 % (talk like a normal call for the VQ), `NoAnswer` 20 %, `Busy` 5 % |

## 6. Outbound

| Scenario | When | Encoding |
|---|---|---|
| `outbound_manual` | Agent-initiated call to a customer | `CALL_TYPE=Outbound`, `RESOURCE_ROLE=Initiated`, no VQ, `ANI` = site outbound CLI, `DNIS` = customer number. Results: `Answered` 55 % (talk 0.7× normal, disposition), `NoAnswer` 30 % (ring 20–45 s), `Busy` 5 %, `AnsweringMachine` 10 % (10–45 s connected, `DISPOSITION=LeftMessage`). `N_CUSTOMER=1` when connected, else 0 |
| `dialer_answered` / `dialer_noanswer` / `dialer_busy` / `dialer_answeringmachine` | Campaign call placed by the dialer (`CAMPAIGN_NAME` set, LOB of the campaign) | As manual, `RESOURCE_ROLE=Received` (agent receives the connected call from the dialer); answered legs carry the dialer hand-off delay in `QUEUE_TIME` (1–8 s) |
| `dialer_drop` | 3 % of dialer attempts: customer answered but no agent was free | `RESOURCE_TYPE=Dialer`, `CALL_RESULT=DialerDrop`, `TECHNICAL_RESULT=Abandoned`, `ABANDON_FLAG=1`, `N_AGENT=0`, `N_CUSTOMER=1`, `DISCONNECT_REASON=System` |

## 7. Internal and direct calls

| Scenario | When | Encoding |
|---|---|---|
| `internal_call` | Agent calls another agent on shift | Two legs ⟨same interaction⟩: initiator (`RESOURCE_ROLE=Initiated`, `SEGMENT_SEQ=1`) and receiver (`Received`, `SEGMENT_SEQ=2`, `PREVIOUS_CALL_ID` = initiator). `CALL_TYPE=Internal`, `N_AGENT=2`, `N_CUSTOMER=0`, no customer / VQ fields, `ANI`/`DNIS` = agent DIDs. Answered 85 % (talk ~2 min) else `NoAnswer` on both legs |
| `direct_did` | Customer dials an agent's DID directly | `CALL_TYPE=Inbound`, `RESOURCE_ROLE=Received`, no VQ / route point, `DNIS` = agent DID. Answered 72 %; otherwise `CALL_RESULT=Voicemail`, `TECHNICAL_RESULT=Diverted`, `RING_TIME=25`, voicemail length in `IVR_TIME` |

## 8. Volume-level scenarios (arrival model)

These are not leg labels but shape the whole day; they are recorded in `_manifest.json → days[].events`.

| Event | Default frequency | Effect |
|---|---|---|
| Weekday pattern | always | Mon 1.18 … Fri 0.97, Sat 0.52, Sun 0.38 × `calls_per_day` |
| Intraday curve | always | peaks at 10:30 and 14:30, lunch dip, night floor 4 % (24x7 queues absorb the night traffic) |
| `burst` | Poisson(1.0)/day | 3–25 % of the day's volume dumped into a gaussian 3–25 min wide, between 07:00 and 22:00 |
| `mega_burst` | 5 % of days | Flash crowd: 0.5–1.5× the whole day's volume inside ~4 min (thousands of calls per minute); abandon rate and callback requests explode, service level collapses, then recovers as the backlog drains |
| `lull` | Poisson(1.0)/day | 10–60 min window with 0–10 % of normal intensity ("phones went dead") |
| `spike_day` | 5 % of days | Whole day × 1.5–3 (bill run, outage, marketing) |
| `quiet_day` | 4 % of days | Whole day × 0.15–0.5 (public holiday) |
| Daily noise | always | log-normal σ = 0.08 on the daily total, gamma jitter per minute |

Staffing follows the forecast curve only, so every burst overloads the queues realistically:
expected wait grows exponentially with load, more customers run out of patience, callbacks are
offered, overflow routing kicks in, and RONA/short-abandon rates stay at their base levels.

## 8b. Queue-level variation (unbalanced VQs)

Inbound arrivals are split across the VQs by `vqs[].weight`, which is intentionally very uneven:

| Tier | VQs | Share of inbound |
|---|---|---|
| Monster | `VQ_CustServ_General` (24x7), `VQ_Sales_New`, `VQ_Tech_Tier1` (24x7) | 27 % / 19 % / 15 % |
| Mid-size | `VQ_Billing_Payments`, `VQ_Retention_Cancel`, `VQ_Sales_Upgrade`, `VQ_CustServ_Account`, `VQ_Retention_Save_Offers` | 10 % / 9 % / 5 % / 4.5 % / 3 % |
| Small | `VQ_Tech_Tier2`, `VQ_Collections_Inbound` (Mon-Sat), `VQ_Billing_Disputes` (Mon-Fri), `VQ_Retention_Loyalty` (Mon-Sat) | 2 % / 1.5 % / 1.2 % / 0.8 % |
| Long tail | `VQ_Sales_Spanish`, `VQ_Tech_Enterprise` (Mon-Fri), `VQ_Retention_VIP`, `VQ_Overflow_Sales`, `VQ_CustServ_Accessibility` (Mon-Fri), `VQ_Overflow_Service` | 0.6 % … 0.2 % (30–180 calls/day) |

Consequences you will see in the data: the monster queues take the brunt of bursts (abandon 10–13 %
over a week with bursts, SL ~50–65 %), the mid-size queues sit in between, and the niche queues –
covered by dedicated coverage agents plus multi-skilled agents from their LOB – run at high service
levels with very few abandons. Closed queues still receive a trickle of after-hours calls
(`after_hours_leak`) so every VQ appears every day. Overflow queues get almost no direct traffic;
their volume comes from overflowed calls.

## 8c. Routing method: ACD vs Predictive Behavioural Routing (PBR)

Each VQ routes either by classic **ACD** (longest-idle, effectively a uniform pick among eligible
agents) or by **PBR** (`pbr_enabled: true`). By default the sales and retention queues use PBR:
`VQ_Sales_New`, `VQ_Sales_Upgrade`, `VQ_Sales_Spanish`, `VQ_Retention_Cancel`,
`VQ_Retention_Save_Offers`, `VQ_Retention_VIP`.

How PBR is modelled:

* every agent has a latent quality `pbr_z` (normal, mean shifted by tenure) and a `PBR_SCORE`
  percentile rank across the roster (`dim_agent.PBR_SCORE`);
* on a PBR queue an eligible agent is selected with probability ∝ `exp(pbr_skew × pbr_z)`
  (`pbr_skew` 0.5–1.0 by default), so the best-scoring agents receive several times the calls of
  the weakest ones and some agents barely get calls at all;
* for **VIP / Enterprise** customers the weights are raised to `pbr_premium_boost` (1.5–2.0),
  concentrating premium calls even more on the top agents;
* RONA, abandon-while-ringing and answered legs all use the same selection, so the imbalance is
  visible on every agent-facing leg of the queue;
* the fact rows record `ROUTING_METHOD` (`ACD`/`PBR`) and, on PBR agent legs, the chosen agent's
  `PBR_SCORE`.

Typical week (defaults): on ACD queues the top-decile agents handle ~10× the calls of the bottom
decile (mostly shift / secondary-skill effects) and 30 % of agents handle half the calls; on PBR
queues the ratio is ~30×, the busiest agent takes 1 200+ calls vs a few for the least-favoured, and
under 20 % of agents handle half the calls. `python -m gim_synth summarize` prints this comparison.

## 9. Agent-level variation

* **Speed factor** per agent (log-normal σ 0.15) × tenure (`<3m` 1.18, `3-12m` 1.06, `1-3y` 0.98, `3y+` 0.92) scales talk and ACW.
* **Shifts** – 8.5 h, start hours weighted by the VQ's expected hourly volume; night shifts mostly staffed by the offshore site; two days off per agent (weekend off 42 %, Mon-Fri VQs always off at weekends). Every open hour of every VQ is guaranteed `min_agents_per_open_hour` eligible agents; the coverage agents added for thin queues work every open day.
* **Skills** – primary VQ plus a secondary VQ in the same LOB (35 %), so multi-skilled agents appear across queues.
* **PBR score** – latent quality per agent (tenure-shifted normal) that PBR queues use to pick agents; see §8c.

## 10. Customer-level variation

* Skewed customer selection (`skew_power` 2.5) produces repeat callers; `FIRST_CALL_FLAG` and `REPEAT_CALL_7D_FLAG` are computed from the customer's contact history across the whole run, including outbound contacts.
* Patience is log-normal per VQ (e.g. Retention customers wait longer than Sales prospects).
* Segments (Consumer 72 %, SMB 15 %, Enterprise 5 %, VIP 8 %) are carried on every customer-facing leg.

## 11. Business outcomes (interaction_outcome_fact)

Every leg on which an agent actually spoke with a customer records what came out of it for the
business. The outcome is drawn **after** the leg's timing is known, so it can depend on the agent,
the customer and the wait, and it is written both as `DISPOSITION` on the leg and as one or two
rows in `interaction_outcome_fact` (see DATA_DICTIONARY.md for the columns).

### Which legs get an outcome

| Leg | Outcome catalogue used | Notes |
|---|---|---|
| answered inbound (`normal`, `long`, `multi_hold`, `consult_only`, `conference` initiator) | VQ's LOB | one primary outcome, optional `CrossSell` |
| `short` (4–30 s talk) | VQ's LOB, **Positive / monetary outcomes excluded** | wrong numbers and quick FAQs don't sell |
| `warm_transfer_received`, `blind_transfer_to_agent` (answered) | receiving VQ's / agent's LOB | the receiving agent owns the outcome |
| `direct_did`, `outbound_manual` (Answered) | agent's LOB | |
| `dialer_answered`, `callback_outbound` (Answered) | campaign's / original VQ's LOB | |
| `blind_transfer`, `warm_transfer`, `external_transfer` legs | – | `DISPOSITION=Transferred`, no outcome (it happens downstream) |
| `conference_joined` | – | `DISPOSITION=Assisted`, the initiator records the outcome |
| `system_drop`, abandons, RONA, IVR, consults, internal, answering machine, no answer | – | no outcome (`LeftMessage` disposition on answering machines) |

### The catalogue (defaults, `lobs[].business_outcomes`)

| LOB | Positive | Neutral | Negative |
|---|---|---|---|
| Sales | `Sale` (26 %, Revenue ≈ 65, product subtype) | `CallbackScheduled` (12 %, case), `Info` (20 %) | `NoSale` (30 %, reason subtype), `NotInterested` (12 %) |
| Retention | `Saved` (32 %, MRR_Retained ≈ 85, offer subtype) | `Downgraded` (12 %, MRR_Lost ≈ 25), `NoChange` (14 %), `PendingDecision` (10 %, case) | `Cancelled` (20 %, MRR_Lost ≈ 85, reason subtype), `OfferDeclined` (12 %) |
| CustomerService | `Resolved` (52 %) | `FollowUp` (14 %, case), `Info` (17 %) | `Escalated` (9 %, case), `Unresolved` (8 %) |
| TechSupport | `Resolved` (48 %) | `Dispatch` (12 %, case), `FollowUp` (14 %, case), `Info` (7 %) | `Escalated` (14 %, case), `Unresolved` (5 %) |
| Billing | `PaymentTaken` (33 %, Payment ≈ 120), `Adjusted` (18 %, Credit ≈ 30) | `Info` (27 %) | `Disputed` (12 %, case), `Escalated` (10 %, case) |
| Collections | `PromiseToPay` (33 %, Promise ≈ 180, promise date), `Paid` (24 %, Payment ≈ 160) | `Hardship` (8 %, case), `NoChange` (7 %) | `Refused` (15 %), `Dispute` (13 %, case) |
| any | `CrossSell` secondary (5 % baseline, Revenue ≈ 18) | | |

Percentages are the **baseline** weights; the realised mix shifts with the effects below (e.g. the
default week shows `Sale` ≈ 36 % because PBR sends Sales calls to the better agents).

### What bends the probabilities

| Effect | Knob | Default behaviour |
|---|---|---|
| Agent quality | `business_outcomes[].agent_lift` × `outcomes.agent_effect_scale` | log-odds shift per unit of the agent's latent PBR quality: `Sale` +0.6, `Saved` +0.7, `Resolved` +0.35…+0.4, `Cancelled` −0.5, `Escalated` −0.3. Positive-outcome rate rises from ~32 % (bottom score quartile) to ~56 % (top quartile). |
| Routing method | (consequence of PBR) | PBR queues pick high-quality agents, so PBR-routed Sales/Retention calls convert at ~41 % vs ~28 % on the ACD overflow / loyalty queues |
| Customer segment | `outcomes.segment_lift` | VIP +0.35, Enterprise +0.25, SMB +0.10 log-odds on Positive outcomes |
| Queue wait | `outcomes.wait_penalty_per_min` | −0.12 log-odds per minute waited beyond the first on Positive outcomes: <1 min ≈ 45 % positive, 5–15 min ≈ 34 %, >15 min ≈ 10 % |
| Call length | fixed rule | `short` handling never yields Positive / monetary outcomes |
| Cross-sell | `outcomes.cross_sell_prob`, `outcomes.cross_sell` | 5 % baseline, scaled by the same agent / segment lifts, only when the primary outcome is not Negative |

### Timestamps

* `OUTCOME_TIME` – monetary outcomes happen inside the conversation (50–97 % into the talk);
  other outcomes are coded at wrap-up, except `in_call_event_prob` (35 %) of them.
* `RECORDED_TIME` – inside the ACW window (`END_TIME`…`ACW_END_TIME`) or at `END_TIME` when the
  leg has no ACW; always ≥ `OUTCOME_TIME`.
* `FOLLOW_UP_DUE_TIME` – 1–5 days later at a business hour (3–14 days for promises to pay).

### Invariants checked per day

Every outcome joins to exactly one answered agent leg with a customer party; the primary
`BUSINESS_RESULT` equals the leg's `DISPOSITION`; one primary per leg, secondaries are `CrossSell`
only; `ANSWER_TIME ≤ OUTCOME_TIME ≤ RECORDED_TIME ≤ ACW_END_TIME`; `AMOUNT`/`CURRENCY` present iff
`AMOUNT_TYPE`; `CASE_ID`/`FOLLOW_UP_DUE_TIME` present iff `FOLLOW_UP_FLAG`, due after
`RECORDED_TIME`.
