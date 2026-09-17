# Data dictionary

All tables are Parquet. Timestamps are `timestamp[ms]`; the naive ones are local business time
(`timezone` in the config, default `America/New_York`), `ARRIVE_TIME_UTC` carries `tz=UTC`.
Flags are `int8` 0/1 (never null) unless stated otherwise. Durations are whole seconds (`int32`).

## interaction_resource_fact

One row per resource leg of a voice interaction (see README §3 for the grain). Partitioned by
`call_date=YYYY-MM-DD` (hive style); the column is also present in each file.

### Identity and lineage

| Column | Type | Description |
|---|---|---|
| `IRF_ID` | int64 | Surrogate key of the row, unique across the whole run. |
| `INTERACTION_ID` | int64 | Interaction the leg belongs to. Shared by all customer-facing legs of a call (initial agent, RONA, transfers, conference joins). Consult legs carry their own interaction id. |
| `ROOT_INTERACTION_ID` | int64 | Interaction id of the customer call at the root of the tree. Equal to `INTERACTION_ID` for every non-consult leg. Group by this to reassemble a full call including consults. |
| `CALL_ID` | string | 16-hex ConnID-style identifier of this leg. Unique per row. |
| `CALL_DATE` | date32 | Business date of the root interaction (partition key). Late legs of a call that crosses midnight keep the root's date. |
| `SEGMENT_SEQ` | int32 | 1-based order of the leg inside the root interaction tree in creation order: root leg = 1, then consult / transfer / conference legs as they occur. Internal calls: initiator = 1, receiver = 2. |
| `PREVIOUS_CALL_ID` | string, nullable | `CALL_ID` of the leg that led to this one: previous agent leg for transfers/RONA re-queues, the initiating agent leg for consult and conference legs, the initiator row for internal calls. Null when `SEGMENT_SEQ` = 1. |
| `PARENT_INTERACTION_ID` | int64, nullable | Consult legs only: the customer interaction from which the consult was made. |
| `PARENT_CALL_ID` | string, nullable | Consult legs only: the `CALL_ID` of the agent leg that initiated the consult. |
| `RELATED_INTERACTION_ID` | int64, nullable | Callback legs only (`CALLBACK_FLAG`=1): the inbound interaction in which the customer requested the callback. |

### Classification

| Column | Type | Description |
|---|---|---|
| `MEDIA_TYPE` | string | Always `voice`. |
| `CALL_TYPE` | string | `Inbound`, `Outbound`, `Internal`, `Consult`. |
| `RESOURCE_TYPE` | string | The resource this row describes: `Agent`, `Queue` (abandoned / callback-requested in a VQ, no agent), `IVR` (self-service, after-hours, voicemail), `External` (external party after an external transfer), `Dialer` (dialer drop, no agent). |
| `RESOURCE_ROLE` | string | Genesys-style role: `Received`, `ReceivedTransfer`, `ReceivedConsult`, `ConferenceJoined`, `Initiated` (outbound / internal initiator / callback). |
| `SCENARIO` | string | Generator label of the scenario that produced the leg (see SCENARIOS.md). Useful for testing; a real Info Mart has no such column. |
| `CALL_RESULT` | string | Business result: `Answered`, `Abandoned`, `ShortAbandon`, `Transferred`, `Conferenced`, `RONA`, `SelfService`, `AfterHours`, `Voicemail`, `CallbackRequested`, `SystemError`, `NoAnswer`, `Busy`, `AnsweringMachine`, `Consulted`, `DialerDrop`, `Completed` (external leg). |
| `CALL_RESULT_CODE` | int16 | Numeric code of `CALL_RESULT` (1 Answered, 2 Abandoned, 3 ShortAbandon, 4 Transferred, 5 Conferenced, 6 RONA, 7 SelfService, 8 AfterHours, 9 Voicemail, 10 CallbackRequested, 11 SystemError, 12 NoAnswer, 13 Busy, 14 AnsweringMachine, 15 Consulted, 16 DialerDrop, 17 Completed). |
| `TECHNICAL_RESULT` | string | Info Mart technical descriptor: `Completed`, `Abandoned`, `Transferred`, `Conferenced`, `Redirected` (RONA), `Diverted` (voicemail, callback), `Failed` (system error, no answer, busy). |
| `TECHNICAL_RESULT_REASON` | string | Reason detail, e.g. `AnsweredByAgent`, `AbandonedWhileQueued`, `AbandonedWhileRinging`, `RouteOnNoAnswer`, `BlindTransfer`, `WarmTransfer`, `ExternalTransfer`, `ConferenceInitiated`, `ConferenceJoined`, `ConsultCompleted`, `IVRContained`, `AfterHoursAnnouncement`, `AfterHoursVoicemail`, `NoAnswerVoicemail`, `CallbackAccepted`, `SystemError`, `OutboundConnected`, `AnsweringMachine`, `NoAnswer`, `Busy`, `NoAgentAvailable`, `InternalCall`, `ExternalParty`, `ExternalNoAnswer`. |
| `DISCONNECT_REASON` | string, nullable | Who released the leg: `Customer`, `Agent`, `System`, `Transfer` (leg ended because it was transferred). Null on RONA legs. |
| `DISPOSITION` | string, nullable | Agent wrap-up code = the primary `BUSINESS_RESULT` of the leg in `interaction_outcome_fact` (LOB specific, see that table: Sales Sale/NoSale/CallbackScheduled/Info/NotInterested; Retention Saved/Cancelled/Downgraded/OfferDeclined/NoChange/PendingDecision; CustomerService Resolved/FollowUp/Escalated/Info/Unresolved; TechSupport Resolved/Escalated/Dispatch/FollowUp/Info/Unresolved; Billing PaymentTaken/Adjusted/Disputed/Info/Escalated; Collections PromiseToPay/Paid/Refused/Dispute/Hardship/NoChange). Legs without an outcome row: `Transferred` on legs that were transferred out, `Assisted` on the joined agent of a conference, `LeftMessage` on answering-machine outbound legs. Null when no agent handled a customer (abandons, IVR, system drops, unanswered outbound). |
| `CAMPAIGN_NAME` | string, nullable | Dialer campaign for outbound dialer legs. |

### Timing

| Column | Type | Description |
|---|---|---|
| `ARRIVE_TIME` | timestamp | When the leg started: call arrival for the root leg, moment of transfer for transferred legs, consult start for consult legs, conference join for conference legs, dial time for outbound. |
| `ARRIVE_TIME_UTC` | timestamp (UTC) | Same instant in UTC. |
| `ANSWER_TIME` | timestamp, nullable | When the resource connected (`ARRIVE_TIME + IVR + QUEUE + RING`). Null when not answered. |
| `END_TIME` | timestamp | When the leg was released (`ANSWER_TIME + TALK + HOLD`, or arrival + IVR + queue + ring for unanswered legs). |
| `ACW_END_TIME` | timestamp, nullable | `END_TIME + ACW_TIME`; null when `ACW_TIME` = 0. |
| `IVR_TIME` | int32 | Seconds in IVR / announcement / voicemail before queuing (root inbound legs); voicemail length for unanswered direct DID calls. |
| `QUEUE_TIME` | int32 | Seconds waiting in the VQ (for abandons: the time waited before hanging up). Dialer-to-agent hand-off delay for dialer legs. |
| `RING_TIME` | int32 | Seconds the target resource was alerting. 25 s on RONA legs. |
| `TALK_TIME` | int32 | Seconds connected excluding hold. For conference initiators includes the conference portion. |
| `HOLD_TIME` | int32 | Seconds the customer was on hold; includes the consult time for legs that consulted. |
| `HOLD_COUNT` | int16 | Number of holds. |
| `ACW_TIME` | int32 | After-call work seconds. |
| `DURATION` | int32 | `END_TIME - ARRIVE_TIME` in seconds (excludes ACW). |
| `HANDLE_TIME` | int32 | `TALK_TIME + HOLD_TIME + ACW_TIME`. |
| `INTERVAL_15MIN` | timestamp | `ARRIVE_TIME` floored to the 15-minute interval (local). |
| `ARRIVE_HOUR` | int8 | Local hour of `ARRIVE_TIME` (0–23). |

### Routing

| Column | Type | Description |
|---|---|---|
| `ROUTE_POINT` | string, nullable | Route point DN the call entered on (`RP_8001`…). For overflowed calls this is the *original* VQ's route point. Null for direct DID / outbound manual / internal. |
| `DNIS` | string, nullable | Number dialled: the VQ's toll-free DNIS for inbound, the agent DID for direct calls / consults / internal, the customer number for outbound. |
| `ANI` | string, nullable | Calling number: the customer's ANI for inbound, the site outbound CLI (or VQ DNIS for callbacks) for outbound, the initiating agent id for consult legs, agent DID for internal. |
| `QUEUE_ID` | string, nullable | ACD queue DN behind the VQ (`9001`…). |
| `QUEUE_NAME` | string, nullable | ACD queue name (`Q_SALES_NEW`…). |
| `VQ_ID` | int32, nullable | Virtual queue id (`3001`…). |
| `VQ_NAME` | string, nullable | Virtual queue the leg was distributed from / worked in. On overflowed legs this is the queue that finally handled the call. Null for direct DID, outbound manual, internal and external legs. |
| `ORIGINAL_VQ_NAME` | string, nullable | Set when the call overflowed: the VQ the customer originally queued in. |
| `SKILL` | string, nullable | Skill expression of the VQ. |
| `ROUTING_METHOD` | string, nullable | How the resource was selected: `ACD` (longest-idle distribution from a non-PBR VQ), `PBR` (Predictive Behavioural Routing – agent chosen by predicted-outcome score; distribution across agents is deliberately lopsided), `Direct` (direct DID, transfer straight to an agent, warm-transfer receiver), `Consult`, `Conference`, `Manual` (agent-initiated outbound), `Dialer`, `Internal`, `Callback`. Queue legs (abandons, callback requests) carry the VQ's `ACD`/`PBR`; IVR and external legs are null. |
| `PBR_SCORE` | float32, nullable | On `PBR`-routed agent legs only: the selected agent's PBR score, expressed as a percentile rank across the roster (0–1; 0.9 = better predicted outcomes than 90 % of agents). High-scoring agents are picked far more often, so the mean of this column on PBR queues sits well above 0.5. |

### Resource

| Column | Type | Description |
|---|---|---|
| `AGENT_ID` | string, nullable | Agent employee id (`AG100001`…). Null on Queue / IVR / External / Dialer legs. |
| `AGENT_NAME` | string, nullable | Display name. |
| `AGENT_GROUP` | string, nullable | Team, `{LOB}_{SITE}_TEAMnn`. |
| `AGENT_TENURE_BAND` | string, nullable | `<3m`, `3-12m`, `1-3y`, `3y+`. New agents are slower (longer talk / ACW). |
| `SITE` | string, nullable | Agent's site; the VQ's home site when no agent handled the leg (queue abandons, after-hours); null for IVR self-service and external legs. |
| `LOB` | string | Line of business of the VQ (or of the agent for direct / outbound / internal). |

### Customer

| Column | Type | Description |
|---|---|---|
| `CUSTOMER_ID` | string, nullable | Customer key (`C10000000`…). Null on consult and internal legs. |
| `CUSTOMER_SEGMENT` | string, nullable | `Consumer`, `SMB`, `Enterprise`, `VIP`. |

### Flags and counts

| Column | Type | Description |
|---|---|---|
| `ANSWERED_FLAG` | int8 | Resource connected (has `ANSWER_TIME`). |
| `ABANDON_FLAG` | int8 | Customer hung up before an agent connected (in queue or while ringing) or the dialer dropped the customer. |
| `SHORT_ABANDON_FLAG` | int8 | Abandoned with `QUEUE_TIME` below `short_abandon_threshold_s` (5 s). Excluded from service-level (flag null). |
| `RONA_FLAG` | int8 | Agent was alerted and did not answer (Redirect On No Answer); call re-queued to a following leg. |
| `OVERFLOW_FLAG` | int8 | Call was rerouted from `ORIGINAL_VQ_NAME` to `VQ_NAME` after waiting `overflow_wait_s`. |
| `TRANSFER_FLAG` | int8 | This leg ended by transferring the customer onwards (blind, warm or external). |
| `TRANSFER_IN_FLAG` | int8 | This leg received the customer via a transfer. |
| `TRANSFER_TYPE` | string, nullable | `Blind`, `Warm`, `External` – set on both the transferring and the receiving leg. |
| `TRANSFER_TO` | string, nullable | On the transferring leg: target VQ name, target agent id or external number. |
| `TRANSFER_COUNT` | int16 | Transfers that occurred in the interaction before this leg began (root = 0). |
| `CONFERENCE_FLAG` | int8 | Leg took part in a conference (initiator and joined agent). |
| `CONSULT_FLAG` | int8 | Agent on this leg initiated a consult (warm transfer, consult-only, conference set-up). |
| `CONSULT_RECEIVED_FLAG` | int8 | This is the consult leg itself (`CALL_TYPE=Consult`). |
| `CALLBACK_FLAG` | int8 | Outbound leg fulfilling a requested callback. |
| `CALLBACK_REQUESTED_FLAG` | int8 | Inbound queue leg on which the customer accepted a callback offer. |
| `SERVICE_LEVEL_FLAG` | int8, nullable | For legs distributed from a VQ: 1 when `QUEUE_TIME + RING_TIME` ≤ the VQ's `service_level_s`, 0 otherwise (abandons after the threshold and RONA count as 0). Null when not applicable (short abandons, callbacks, direct / warm-received / consult / outbound legs). |
| `FIRST_CALL_FLAG` | int8 | First contact of this customer in the generated period. |
| `REPEAT_CALL_7D_FLAG` | int8 | Customer had another contact in the previous 7 days. |
| `N_AGENT` | int8 | Agent parties on the leg: 0 (queue / IVR / external / dialer drop), 1 (normal), 2 (consult, conference, internal). |
| `N_CUSTOMER` | int8 | Customer parties on the leg: 1 for customer-facing legs and connected outbound, 0 for consults, internal calls and unconnected outbound attempts. |

## interaction_outcome_fact

Business outcomes recorded by agents on handled customer calls – the "what happened for the
business" companion of the resource fact (in a real Info Mart this lives in
`INTERACTION_DESCRIPTOR.BUSINESS_RESULT` / attached user data and in the CRM). One row per outcome;
every **handled customer leg** (agent connected with a customer: answered inbound, warm-transfer
received, conference initiator, direct DID, connected outbound / dialer / callback) has exactly
one primary outcome (`OUTCOME_SEQ`=1) and optionally one `CrossSell` secondary (`OUTCOME_SEQ`=2).
Legs that were transferred out, consult legs, internal calls, abandons, IVR legs, system drops and
unconnected outbound attempts have no outcome. Partitioned like the fact (`call_date=`), same
timestamp conventions. Join on `IRF_ID` (one-to-many) or `ROOT_INTERACTION_ID` (whole call).

### Identity and join keys

| Column | Type | Description |
|---|---|---|
| `OUTCOME_ID` | int64 | Surrogate key, unique across the run. |
| `IRF_ID` | int64 | The agent leg the outcome was recorded on (`interaction_resource_fact.IRF_ID`). |
| `CALL_ID`, `INTERACTION_ID`, `ROOT_INTERACTION_ID`, `CALL_DATE` | | Copied from the leg for convenient joins / partition pruning. |
| `OUTCOME_SEQ` | int8 | 1 = primary outcome (equals the leg's `DISPOSITION`), 2 = secondary `CrossSell`. |

### Timing

| Column | Type | Description |
|---|---|---|
| `OUTCOME_TIME` | timestamp | When the outcome happened. Monetary outcomes (order placed, payment taken, save offer accepted, cancellation processed) occur **inside the conversation**, 50–97 % of the way between `ANSWER_TIME` and `END_TIME`; other outcomes are coded at wrap-up (= `RECORDED_TIME`) except a configurable share (`in_call_event_prob`, 35 %) that also falls inside the call. Always ≥ the leg's `ANSWER_TIME`. |
| `OUTCOME_TIME_UTC` | timestamp (UTC) | Same instant in UTC. |
| `RECORDED_TIME` | timestamp | When the agent coded the outcome: inside the after-call work window (`END_TIME` … `ACW_END_TIME`), or at `END_TIME` when the leg has no ACW. Always ≥ `OUTCOME_TIME`. |
| `IN_CALL_FLAG` | int8 | 1 when `OUTCOME_TIME` lies inside the talk (≤ `END_TIME`). |

### Context (denormalised from the leg)

| Column | Type | Description |
|---|---|---|
| `CALL_TYPE`, `SCENARIO`, `LOB`, `VQ_NAME`, `CAMPAIGN_NAME`, `ROUTING_METHOD` | | As on the leg. `LOB` is the LOB whose outcome catalogue was used (VQ's LOB, or the agent's LOB for direct / manual outbound). |
| `AGENT_ID`, `AGENT_NAME`, `AGENT_GROUP`, `AGENT_TENURE_BAND`, `SITE` | | The agent who recorded the outcome (= the leg's agent). |
| `AGENT_PBR_SCORE` | float32 | The agent's PBR score (percentile 0–1) on **every** row, regardless of routing method – the agent effect on outcomes is driven by the same latent quality, so this is the column to correlate conversion with. |
| `CUSTOMER_ID`, `CUSTOMER_SEGMENT` | | The customer. |
| `QUEUE_TIME`, `TALK_TIME`, `HANDLE_TIME` | int32 | Copied from the leg so wait / handle-time effects can be analysed without a join. |

### The outcome

| Column | Type | Description |
|---|---|---|
| `OUTCOME_CATEGORY` | string | Roll-up: `Sale`, `Retention`, `Churn`, `Service`, `Escalation`, `Billing`, `Collections`, `FollowUp`, `NoChange`. |
| `BUSINESS_RESULT` | string | The outcome code (LOB catalogue in `lobs[].business_outcomes`). Defaults – **Sales**: `Sale`, `NoSale`, `CallbackScheduled`, `Info`, `NotInterested`; **Retention**: `Saved`, `Cancelled`, `Downgraded`, `OfferDeclined` (kept the plan, refused the offer), `NoChange` (enquiry only), `PendingDecision`; **CustomerService**: `Resolved`, `FollowUp`, `Escalated`, `Info`, `Unresolved`; **TechSupport**: `Resolved`, `Escalated`, `Dispatch`, `FollowUp`, `Info`, `Unresolved`; **Billing**: `PaymentTaken`, `Adjusted`, `Disputed`, `Info`, `Escalated`; **Collections**: `PromiseToPay`, `Paid`, `Refused`, `Dispute`, `Hardship`, `NoChange`; plus `CrossSell` on secondary rows of any LOB. |
| `OUTCOME_SUBTYPE` | string, nullable | Detail of the outcome: product sold (`Mobile_Postpaid`, `Broadband_Fibre`, `TV_Bundle` …), save offer (`Discount`, `PlanChange`, `FreeMonths` …), cancel reason (`Price`, `Competitor`, `ServiceIssues` …), resolution (`Reset`, `ConfigFix` …), payment method (`Card`, `BankTransfer`), escalation target, cross-sell product (`DeviceInsurance`, `StreamingAddOn` …). Null for outcomes without subtypes. |
| `OUTCOME_POLARITY` | string | `Positive` (sale, save, resolved, payment, promise, credit), `Neutral` (info, no change, follow-up, downgrade, dispatch), `Negative` (no sale, cancelled, offer declined, escalated, unresolved, dispute, refused). |
| `OFFER_MADE_FLAG` | int8 | An offer was presented to the customer (probability per outcome, e.g. always on `Sale` / `NoSale` / `OfferDeclined`). |
| `AMOUNT_TYPE` | string, nullable | `Revenue` (sale / cross-sell value), `MRR_Retained` (monthly revenue saved), `MRR_Lost` (cancelled or downgraded monthly revenue), `Payment` (payment taken), `Credit` (adjustment granted), `Promise` (promise-to-pay amount). Null when the outcome carries no money. |
| `AMOUNT` | float64, nullable | Positive amount in `CURRENCY`, log-normal per outcome (e.g. Sale median 65, Payment median 120, MRR 85); the direction is given by `AMOUNT_TYPE`. Present iff `AMOUNT_TYPE`. |
| `CURRENCY` | string, nullable | `USD` by default (`outcomes.currency`). |
| `FOLLOW_UP_FLAG` | int8 | A case / task was created (scheduled callbacks, pending decisions, escalations, disputes, dispatches, promises to pay, hardship referrals). |
| `CASE_ID` | string, nullable | Case / ticket id (`CS10000001`…), present iff `FOLLOW_UP_FLAG`. |
| `FOLLOW_UP_DUE_TIME` | timestamp, nullable | When the follow-up is due: 1–5 days after `RECORDED_TIME` at a business hour (09:00–18:00 local); 3–14 days for promises to pay (the promise date). |

How outcomes are drawn: each LOB has a catalogue of outcomes with baseline weights; per call the
weights are bent in log-odds space by the **agent's** latent quality (`agent_lift`, e.g. +0.6 for
`Sale`, −0.5 for `Cancelled`), by the **customer segment** (`segment_lift`, VIP +0.35 on Positive
outcomes) and by the **wait** the customer endured (`wait_penalty_per_min`, −0.12 per minute
beyond the first on Positive outcomes). Calls with the `short` handling outcome (4–30 s talk) never
produce Positive or monetary outcomes. Because PBR queues route disproportionately to
high-quality agents, PBR-routed Sales/Retention calls convert visibly better than ACD-routed ones,
and conversion rises monotonically with `AGENT_PBR_SCORE` (see README §6).

## Dimension tables

### dim_site
`SITE`, `COUNTRY`, `OFFSHORE_FLAG`, `OUTBOUND_CLI`.

### dim_lob
`LOB`, `LOB_SHORT`.

### dim_vq
`VQ_ID`, `VQ_NAME`, `QUEUE_ID`, `QUEUE_NAME`, `ROUTE_POINT`, `DNIS`, `LOB`, `SKILL`, `HOME_SITE`,
`OPEN_HOUR`, `CLOSE_HOUR` (24 = midnight; 0/24 = 24x7), `OPEN_DAYS` (`Mon-Sun` / `Mon-Sat` / `Mon-Fri`),
`SERVICE_LEVEL_S`, `OVERFLOW_VQ_NAME`, `ROUTING_METHOD` (`ACD`/`PBR`), `PBR_ENABLED_FLAG`, `PBR_SKEW`
(log-normal sigma of agent weights, null for ACD queues), `ROSTER_SIZE` (agents skilled for the VQ),
`EXPECTED_AHT_S`.

### dim_agent
`AGENT_ID`, `AGENT_NAME`, `AGENT_GROUP`, `SITE`, `LOB`, `AGENT_TENURE_BAND`, `PRIMARY_VQ_NAME`,
`SKILLS` (pipe separated), `SHIFT_START_HOUR`, `SHIFT_LEN_H`, `DAYS_OFF` (pipe separated weekday
names), `SPEED_FACTOR` (multiplier on talk / ACW), `PBR_SCORE` (percentile rank 0–1 of the agent's
predicted-outcome quality; drives how often PBR queues pick the agent), `AGENT_DID`.

### dim_customer
`CUSTOMER_ID`, `ANI`, `CUSTOMER_SEGMENT`.

## _manifest.json

`generated_at`, `config` (effective configuration), `roster_size`, `vq_count`, `rows`,
`interactions`, `outcome_rows`, `elapsed_s`, `dropped_callbacks_after_period`, `call_result_counts`,
`call_type_counts`, `scenario_counts`, `business_result_counts` (`LOB:BUSINESS_RESULT` → count),
`outcome_polarity_counts`, `amount_totals_by_type`, `days[]` (`date`, `label`, `base_volume`,
`arrivals`, `legs`, `interactions`, `abandon_pct`, `service_level_pct`, `peak_calls_per_min`,
`min_calls_per_min_daytime`, `outcomes`, `positive_outcome_pct`, `events[]` with the bursts / mega
bursts / lulls / day events of that day), `files[]`.
