"""Machine-readable data dictionary of every table the generator writes.

Descriptions live here (one entry per column of every table); the Arrow types come from the actual
schemas, so the dictionary can never drift from the data. `build_dictionary` returns one row per
column and is written to `_data_dictionary.csv` alongside the Parquet output.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, List

import pyarrow as pa

from .schema import OUTCOME_SCHEMA, SCHEMA

TABLE_DESCRIPTIONS: Dict[str, str] = {
    "interaction_resource_fact": (
        "One row per resource leg (agent, queue, IVR, external party, dialer) of a voice interaction - the "
        "Genesys Info Mart INTERACTION_RESOURCE_FACT grain. Partitioned by call_date."),
    "interaction_outcome_fact": (
        "Business outcomes recorded on handled customer legs (sale, saved, cancelled, resolved, payment, no "
        "change ...): one primary outcome per leg plus optional CrossSell secondary. Join to "
        "interaction_resource_fact on IRF_ID. Partitioned by call_date."),
    "dim_site": "Contact-centre sites (locations) agents work from.",
    "dim_lob": "Lines of business.",
    "dim_vq": "Virtual queues with their ACD queue, route point, DNIS, opening hours, routing method and expected AHT.",
    "dim_agent": "Agent roster: team, site, LOB, skills, shift, days off, personal speed factor and PBR score.",
    "dim_customer": "Customer pool: id, phone number (ANI) and segment.",
}

COLUMN_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    # ------------------------------------------------------------------ #
    "interaction_resource_fact": {
        "IRF_ID": "Surrogate key of the row, unique across the run.",
        "INTERACTION_ID": "Interaction the leg belongs to; shared by all customer-facing legs of a call, consult legs get their own.",
        "ROOT_INTERACTION_ID": "Interaction id of the customer call at the root of the tree; group by this to reassemble a full call including consults.",
        "CALL_ID": "16-hex ConnID-style identifier of this leg, unique per row.",
        "CALL_DATE": "Business date of the root interaction (partition key); late legs of a call crossing midnight keep the root's date.",
        "SEGMENT_SEQ": "1-based order of the leg inside the root interaction tree in creation order.",
        "PREVIOUS_CALL_ID": "CALL_ID of the leg that led to this one (previous agent for transfers / RONA re-queues, initiating leg for consults and conferences, initiator for internal calls); null when SEGMENT_SEQ = 1.",
        "PARENT_INTERACTION_ID": "Consult legs only: the customer interaction from which the consult was made.",
        "PARENT_CALL_ID": "Consult legs only: the CALL_ID of the agent leg that initiated the consult.",
        "RELATED_INTERACTION_ID": "Callback legs only (CALLBACK_FLAG = 1): the inbound interaction in which the callback was requested.",
        "MEDIA_TYPE": "Always voice.",
        "CALL_TYPE": "Inbound | Outbound | Internal | Consult.",
        "RESOURCE_TYPE": "Resource the row describes: Agent | Queue (abandoned / callback requested, no agent) | IVR | External | Dialer.",
        "RESOURCE_ROLE": "Received | ReceivedTransfer | ReceivedConsult | ConferenceJoined | Initiated.",
        "SCENARIO": "Generator label of the scenario that produced the leg (see docs/SCENARIOS.md); not present in a real Info Mart.",
        "CALL_RESULT": "Business result of the leg: Answered, Abandoned, ShortAbandon, Transferred, Conferenced, RONA, SelfService, AfterHours, Voicemail, CallbackRequested, SystemError, NoAnswer, Busy, AnsweringMachine, Consulted, DialerDrop, Completed.",
        "CALL_RESULT_CODE": "Numeric code of CALL_RESULT (1 Answered ... 17 Completed).",
        "TECHNICAL_RESULT": "Info Mart technical descriptor: Completed | Abandoned | Transferred | Conferenced | Redirected | Diverted | Failed.",
        "TECHNICAL_RESULT_REASON": "Reason detail, e.g. AnsweredByAgent, AbandonedWhileQueued, RouteOnNoAnswer, BlindTransfer, IVRContained, NoAgentAvailable.",
        "DISCONNECT_REASON": "Who released the leg: Customer | Agent | System | Transfer; null on RONA legs.",
        "DISPOSITION": "Agent wrap-up code = primary BUSINESS_RESULT in interaction_outcome_fact; Transferred / Assisted / LeftMessage on legs without an outcome; null when no agent handled a customer.",
        "CAMPAIGN_NAME": "Dialer campaign for outbound dialer legs.",
        "ARRIVE_TIME": "Local start of the leg: arrival for root legs, moment of transfer / consult start / conference join / dial time otherwise.",
        "ARRIVE_TIME_UTC": "ARRIVE_TIME as a UTC instant.",
        "ANSWER_TIME": "When the resource connected (ARRIVE_TIME + IVR + QUEUE + RING); null when not answered.",
        "END_TIME": "When the leg was released (ANSWER_TIME + TALK + HOLD, or arrival + IVR + queue + ring for unanswered legs).",
        "ACW_END_TIME": "END_TIME + ACW_TIME; null when ACW_TIME = 0.",
        "IVR_TIME": "Seconds in IVR / announcement / voicemail before queuing.",
        "QUEUE_TIME": "Seconds waiting in the VQ (time waited before hanging up for abandons; dialer hand-off delay for dialer legs).",
        "RING_TIME": "Seconds the target resource was alerting (25 on RONA legs).",
        "TALK_TIME": "Seconds connected excluding hold (conference initiators include the conference portion).",
        "HOLD_TIME": "Seconds the customer was on hold, including consult time for legs that consulted.",
        "HOLD_COUNT": "Number of holds.",
        "ACW_TIME": "After-call work seconds.",
        "DURATION": "END_TIME - ARRIVE_TIME in seconds (excludes ACW).",
        "HANDLE_TIME": "TALK_TIME + HOLD_TIME + ACW_TIME.",
        "INTERVAL_15MIN": "ARRIVE_TIME floored to the 15-minute interval (local).",
        "ARRIVE_HOUR": "Local hour of ARRIVE_TIME (0-23).",
        "ROUTE_POINT": "Route point DN the call entered on (the original VQ's for overflowed calls); null for direct DID / manual outbound / internal.",
        "DNIS": "Number dialled: VQ DNIS for inbound, agent DID for direct / consult / internal, customer number for outbound.",
        "ANI": "Calling number: customer ANI for inbound, site CLI (or VQ DNIS for callbacks) for outbound, initiating agent id for consults, agent DID for internal.",
        "QUEUE_ID": "ACD queue DN behind the VQ.",
        "QUEUE_NAME": "ACD queue name.",
        "VQ_ID": "Virtual queue id.",
        "VQ_NAME": "Virtual queue the leg was distributed from / worked in (the queue that finally handled an overflowed call); null for direct DID, manual outbound, internal, external legs.",
        "ORIGINAL_VQ_NAME": "Set when the call overflowed: the VQ the customer originally queued in.",
        "SKILL": "Skill expression of the VQ.",
        "ROUTING_METHOD": "How the resource was selected: ACD | PBR | Direct | Consult | Conference | Manual | Dialer | Internal | Callback; queue legs carry the VQ's ACD/PBR; null on IVR / external legs.",
        "PBR_SCORE": "PBR-routed agent legs only: the selected agent's PBR percentile score (0-1).",
        "AGENT_ID": "Agent employee id; null on Queue / IVR / External / Dialer legs.",
        "AGENT_NAME": "Agent display name.",
        "AGENT_GROUP": "Team, {LOB}_{SITE}_TEAMnn.",
        "AGENT_TENURE_BAND": "<3m | 3-12m | 1-3y | 3y+.",
        "SITE": "Agent's site, or the VQ's home site when no agent handled the leg; null for IVR self-service and external legs.",
        "LOB": "Line of business of the VQ (or of the agent for direct / outbound / internal).",
        "CUSTOMER_ID": "Customer key; null on consult and internal legs.",
        "CUSTOMER_SEGMENT": "Consumer | SMB | Enterprise | VIP.",
        "ANSWERED_FLAG": "Resource connected (has ANSWER_TIME).",
        "ABANDON_FLAG": "Customer hung up before an agent connected (in queue or while ringing) or the dialer dropped the customer.",
        "SHORT_ABANDON_FLAG": "Abandoned with QUEUE_TIME below short_abandon_threshold_s; excluded from service level.",
        "RONA_FLAG": "Agent was alerted and did not answer (Redirect On No Answer).",
        "OVERFLOW_FLAG": "Call was rerouted from ORIGINAL_VQ_NAME to VQ_NAME after waiting overflow_wait_s.",
        "TRANSFER_FLAG": "This leg ended by transferring the customer onwards.",
        "TRANSFER_IN_FLAG": "This leg received the customer via a transfer.",
        "TRANSFER_TYPE": "Blind | Warm | External, on both the transferring and the receiving leg.",
        "TRANSFER_TO": "On the transferring leg: target VQ name, target agent id or external number.",
        "TRANSFER_COUNT": "Transfers that occurred in the interaction before this leg began.",
        "CONFERENCE_FLAG": "Leg took part in a conference (initiator and joined agent).",
        "CONSULT_FLAG": "Agent on this leg initiated a consult (warm transfer, consult-only, conference set-up).",
        "CONSULT_RECEIVED_FLAG": "This is the consult leg itself (CALL_TYPE = Consult).",
        "CALLBACK_FLAG": "Outbound leg fulfilling a requested callback.",
        "CALLBACK_REQUESTED_FLAG": "Inbound queue leg on which the customer accepted a callback offer.",
        "SERVICE_LEVEL_FLAG": "1 when QUEUE_TIME + RING_TIME <= the VQ's service_level_s, 0 otherwise; null when not applicable (short abandons, callbacks, direct / warm-received / consult / outbound legs).",
        "FIRST_CALL_FLAG": "First contact of this customer in the generated period.",
        "REPEAT_CALL_7D_FLAG": "Customer had another contact in the previous 7 days.",
        "N_AGENT": "Agent parties on the leg: 0, 1 or 2 (consult, conference, internal).",
        "N_CUSTOMER": "Customer parties on the leg: 1 for customer-facing legs and connected outbound, else 0.",
    },
    # ------------------------------------------------------------------ #
    "interaction_outcome_fact": {
        "OUTCOME_ID": "Surrogate key, unique across the run.",
        "IRF_ID": "The agent leg the outcome was recorded on (interaction_resource_fact.IRF_ID).",
        "CALL_ID": "CALL_ID of that leg.",
        "INTERACTION_ID": "INTERACTION_ID of that leg.",
        "ROOT_INTERACTION_ID": "ROOT_INTERACTION_ID of that leg (whole-call grouping key).",
        "CALL_DATE": "CALL_DATE of that leg (partition key).",
        "OUTCOME_SEQ": "1 = primary outcome (equals the leg's DISPOSITION), 2 = secondary CrossSell.",
        "OUTCOME_TIME": "When the outcome happened: inside the conversation for monetary outcomes (and in_call_event_prob of the others), otherwise at wrap-up; always >= the leg's ANSWER_TIME.",
        "OUTCOME_TIME_UTC": "OUTCOME_TIME as a UTC instant.",
        "RECORDED_TIME": "When the agent coded the outcome: inside the ACW window (END_TIME..ACW_END_TIME) or at END_TIME when there is no ACW; always >= OUTCOME_TIME.",
        "IN_CALL_FLAG": "1 when OUTCOME_TIME lies inside the talk (<= END_TIME).",
        "CALL_TYPE": "CALL_TYPE of the leg.",
        "SCENARIO": "SCENARIO of the leg.",
        "LOB": "LOB whose outcome catalogue was used (VQ's LOB, or the agent's LOB for direct / manual outbound).",
        "VQ_NAME": "VQ_NAME of the leg.",
        "CAMPAIGN_NAME": "Dialer campaign of the leg, if any.",
        "ROUTING_METHOD": "ROUTING_METHOD of the leg (ACD, PBR, Direct, Manual, Dialer, Callback).",
        "AGENT_ID": "Agent who recorded the outcome (= the leg's agent).",
        "AGENT_NAME": "Agent display name.",
        "AGENT_GROUP": "Agent team.",
        "AGENT_TENURE_BAND": "Agent tenure band.",
        "AGENT_PBR_SCORE": "The agent's PBR percentile score (0-1) on every row regardless of routing method; the agent effect on outcomes is driven by the same latent quality.",
        "SITE": "Agent site.",
        "CUSTOMER_ID": "Customer key.",
        "CUSTOMER_SEGMENT": "Consumer | SMB | Enterprise | VIP.",
        "QUEUE_TIME": "QUEUE_TIME of the leg (seconds), for wait-effect analysis without a join.",
        "TALK_TIME": "TALK_TIME of the leg (seconds).",
        "HANDLE_TIME": "HANDLE_TIME of the leg (seconds).",
        "OUTCOME_CATEGORY": "Roll-up: Sale | Retention | Churn | Service | Escalation | Billing | Collections | FollowUp | NoChange.",
        "BUSINESS_RESULT": "The outcome code from the LOB catalogue (Sale, NoSale, Saved, Cancelled, Downgraded, OfferDeclined, NoChange, Resolved, Escalated, FollowUp, PaymentTaken, PromiseToPay ...) or CrossSell on secondary rows.",
        "OUTCOME_SUBTYPE": "Detail: product sold, save offer, cancel reason, resolution type, payment method, escalation target, cross-sell product; null when the outcome has no subtypes.",
        "OUTCOME_POLARITY": "Positive | Neutral | Negative.",
        "OFFER_MADE_FLAG": "An offer was presented to the customer.",
        "AMOUNT_TYPE": "Revenue | MRR_Retained | MRR_Lost | Payment | Credit | Promise; null when the outcome carries no money.",
        "AMOUNT": "Positive amount in CURRENCY (direction given by AMOUNT_TYPE); present iff AMOUNT_TYPE.",
        "CURRENCY": "ISO currency code of AMOUNT (outcomes.currency).",
        "FOLLOW_UP_FLAG": "A case / task was created (scheduled callback, pending decision, escalation, dispute, dispatch, promise to pay, hardship referral).",
        "CASE_ID": "Case / ticket id, present iff FOLLOW_UP_FLAG.",
        "FOLLOW_UP_DUE_TIME": "When the follow-up is due (1-5 days after RECORDED_TIME at a business hour; 3-14 days for promises to pay).",
    },
    # ------------------------------------------------------------------ #
    "dim_site": {
        "SITE": "Site name (primary key; joins interaction_resource_fact.SITE, dim_agent.SITE, dim_vq.HOME_SITE).",
        "COUNTRY": "ISO country code of the site.",
        "OFFSHORE_FLAG": "1 for offshore sites, which staff most night shifts.",
        "OUTBOUND_CLI": "Caller id presented on outbound calls from this site (ANI of outbound legs).",
    },
    "dim_lob": {
        "LOB": "Line of business name (primary key; joins LOB on both facts, dim_vq.LOB, dim_agent.LOB).",
        "LOB_SHORT": "Short code used in AGENT_GROUP names (SAL, RET, CS, TEC, BIL, COL).",
    },
    "dim_vq": {
        "VQ_ID": "Virtual queue id (primary key; joins interaction_resource_fact.VQ_ID).",
        "VQ_NAME": "Virtual queue name (joins VQ_NAME / ORIGINAL_VQ_NAME / TRANSFER_TO on the fact).",
        "QUEUE_ID": "ACD queue DN behind the VQ.",
        "QUEUE_NAME": "ACD queue name.",
        "ROUTE_POINT": "Route point DN calls to this VQ enter on.",
        "DNIS": "Toll-free number dialled to reach this VQ.",
        "LOB": "Line of business of the VQ.",
        "SKILL": "Skill expression required to take calls from the VQ.",
        "HOME_SITE": "Site reported on queue legs that never reached an agent.",
        "OPEN_HOUR": "Local opening hour (0-23).",
        "CLOSE_HOUR": "Local closing hour (1-24; 24 = midnight; OPEN 0 / CLOSE 24 = 24x7).",
        "OPEN_DAYS": "Mon-Sun | Mon-Sat | Mon-Fri.",
        "SERVICE_LEVEL_S": "Service-level threshold in seconds used for SERVICE_LEVEL_FLAG.",
        "OVERFLOW_VQ_NAME": "Backup VQ calls overflow to after overflow_wait_s; null when none.",
        "ROUTING_METHOD": "ACD (longest idle, uniform) or PBR (Predictive Behavioural Routing, skewed to high-scoring agents).",
        "PBR_ENABLED_FLAG": "1 when the VQ uses PBR.",
        "PBR_SKEW": "Strength of the PBR skew: agent selection weight = exp(PBR_SKEW * agent quality z-score), i.e. the log-normal sigma of agent weights (0 = uniform, 1.2 = extreme); null for ACD queues.",
        "ROSTER_SIZE": "Number of agents skilled for the VQ (primary or secondary skill).",
        "EXPECTED_AHT_S": "Expected average handle time in seconds used for staffing (talk + expected hold + ACW).",
    },
    "dim_agent": {
        "AGENT_ID": "Agent employee id (primary key; joins AGENT_ID on both facts and TRANSFER_TO).",
        "AGENT_NAME": "Display name.",
        "AGENT_GROUP": "Team, {LOB}_{SITE}_TEAMnn.",
        "SITE": "Site the agent works from.",
        "LOB": "Line of business of the agent's primary skill.",
        "AGENT_TENURE_BAND": "<3m | 3-12m | 1-3y | 3y+; newer agents are slower and have lower PBR quality on average.",
        "PRIMARY_VQ_NAME": "VQ of the agent's primary skill.",
        "SKILLS": "Pipe-separated skill expressions the agent carries (primary plus optional secondary in the same LOB).",
        "SHIFT_START_HOUR": "Local hour the agent's shift starts (0-23).",
        "SHIFT_LEN_H": "Shift length in hours.",
        "DAYS_OFF": "Pipe-separated weekday names the agent does not work.",
        "SPEED_FACTOR": "Multiplier on the agent's talk and ACW times (1.0 = average).",
        "PBR_SCORE": "Percentile rank (0-1) of the agent's latent quality; drives PBR routing and the agent effect on business outcomes.",
        "AGENT_DID": "Agent's direct-dial number (DNIS of direct / consult / internal legs).",
    },
    "dim_customer": {
        "CUSTOMER_ID": "Customer key (primary key; joins CUSTOMER_ID on both facts).",
        "ANI": "Customer phone number (ANI of inbound legs, DNIS of outbound legs).",
        "CUSTOMER_SEGMENT": "Consumer | SMB | Enterprise | VIP.",
    },
}

DICTIONARY_COLUMNS = ["TABLE_NAME", "TABLE_DESCRIPTION", "COLUMN_NAME", "ORDINAL", "DATA_TYPE", "NULLABLE", "DESCRIPTION"]

FLAG_LIKE = ("_FLAG",)


def _nullable(table: str, col: str) -> str:
    if col.endswith(FLAG_LIKE) and col != "SERVICE_LEVEL_FLAG":
        return "no"
    if table.startswith("dim_"):
        return "yes" if col in ("OVERFLOW_VQ_NAME", "PBR_SKEW") else "no"
    if table == "interaction_resource_fact" and col in (
            "IRF_ID", "INTERACTION_ID", "ROOT_INTERACTION_ID", "CALL_ID", "CALL_DATE", "SEGMENT_SEQ", "MEDIA_TYPE",
            "CALL_TYPE", "RESOURCE_TYPE", "RESOURCE_ROLE", "SCENARIO", "CALL_RESULT", "CALL_RESULT_CODE",
            "TECHNICAL_RESULT", "TECHNICAL_RESULT_REASON", "ARRIVE_TIME", "ARRIVE_TIME_UTC", "END_TIME", "IVR_TIME",
            "QUEUE_TIME", "RING_TIME", "TALK_TIME", "HOLD_TIME", "HOLD_COUNT", "ACW_TIME", "DURATION", "HANDLE_TIME",
            "INTERVAL_15MIN", "ARRIVE_HOUR", "DNIS", "ANI", "LOB", "TRANSFER_COUNT", "N_AGENT", "N_CUSTOMER"):
        return "no"
    if table == "interaction_outcome_fact" and col in (
            "OUTCOME_ID", "IRF_ID", "CALL_ID", "INTERACTION_ID", "ROOT_INTERACTION_ID", "CALL_DATE", "OUTCOME_SEQ",
            "OUTCOME_TIME", "OUTCOME_TIME_UTC", "RECORDED_TIME", "CALL_TYPE", "SCENARIO", "LOB", "ROUTING_METHOD",
            "AGENT_ID", "AGENT_NAME", "AGENT_GROUP", "AGENT_TENURE_BAND", "AGENT_PBR_SCORE", "SITE", "CUSTOMER_ID",
            "CUSTOMER_SEGMENT", "QUEUE_TIME", "TALK_TIME", "HANDLE_TIME", "OUTCOME_CATEGORY", "BUSINESS_RESULT",
            "OUTCOME_POLARITY"):
        return "no"
    return "yes"


def build_dictionary(dimension_schemas: Dict[str, pa.Schema]) -> List[Dict[str, object]]:
    """One row per column of every table. Raises if a column lacks a description or a description
    refers to a column that does not exist, so the dictionary cannot drift from the schemas."""
    schemas: Dict[str, pa.Schema] = {"interaction_resource_fact": SCHEMA, "interaction_outcome_fact": OUTCOME_SCHEMA}
    schemas.update(dimension_schemas)
    rows: List[Dict[str, object]] = []
    for table, schema in schemas.items():
        described = COLUMN_DESCRIPTIONS.get(table)
        if described is None:
            raise KeyError(f"no data dictionary for table {table}")
        missing = [f.name for f in schema if f.name not in described]
        extra = [c for c in described if c not in schema.names]
        if missing or extra:
            raise KeyError(f"data dictionary out of sync for {table}: undocumented={missing} unknown={extra}")
        for i, f in enumerate(schema, start=1):
            rows.append({
                "TABLE_NAME": table,
                "TABLE_DESCRIPTION": TABLE_DESCRIPTIONS[table],
                "COLUMN_NAME": f.name,
                "ORDINAL": i,
                "DATA_TYPE": str(f.type),
                "NULLABLE": _nullable(table, f.name),
                "DESCRIPTION": described[f.name],
            })
    return rows


def dictionary_csv(rows: List[Dict[str, object]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=DICTIONARY_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def dictionary_markdown(rows: List[Dict[str, object]]) -> str:
    out: List[str] = []
    for table in dict.fromkeys(r["TABLE_NAME"] for r in rows):
        out.append(f"## {table}\n\n{TABLE_DESCRIPTIONS[table]}\n\n| # | Column | Type | Nullable | Description |\n|---|---|---|---|---|")
        for r in rows:
            if r["TABLE_NAME"] == table:
                out.append(f"| {r['ORDINAL']} | `{r['COLUMN_NAME']}` | {r['DATA_TYPE']} | {r['NULLABLE']} | {r['DESCRIPTION']} |")
        out.append("")
    return "\n".join(out)
