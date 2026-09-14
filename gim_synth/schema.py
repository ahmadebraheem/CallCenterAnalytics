"""Column definitions and Arrow schema of the INTERACTION_RESOURCE_FACT style output."""
from __future__ import annotations

from typing import Any, Dict, List

import pyarrow as pa

TS = pa.timestamp("ms")
TS_UTC = pa.timestamp("ms", tz="UTC")

# name -> arrow type.  Order here is the column order of the parquet files.
COLUMNS: Dict[str, pa.DataType] = {
    # ---- identity / lineage ----
    "IRF_ID": pa.int64(),
    "INTERACTION_ID": pa.int64(),
    "ROOT_INTERACTION_ID": pa.int64(),
    "CALL_ID": pa.string(),
    "CALL_DATE": pa.date32(),
    "SEGMENT_SEQ": pa.int32(),
    "PREVIOUS_CALL_ID": pa.string(),
    "PARENT_INTERACTION_ID": pa.int64(),
    "PARENT_CALL_ID": pa.string(),
    "RELATED_INTERACTION_ID": pa.int64(),
    # ---- classification ----
    "MEDIA_TYPE": pa.string(),
    "CALL_TYPE": pa.string(),
    "RESOURCE_TYPE": pa.string(),
    "RESOURCE_ROLE": pa.string(),
    "SCENARIO": pa.string(),
    "CALL_RESULT": pa.string(),
    "CALL_RESULT_CODE": pa.int16(),
    "TECHNICAL_RESULT": pa.string(),
    "TECHNICAL_RESULT_REASON": pa.string(),
    "DISCONNECT_REASON": pa.string(),
    "DISPOSITION": pa.string(),
    "CAMPAIGN_NAME": pa.string(),
    # ---- timing ----
    "ARRIVE_TIME": TS,
    "ARRIVE_TIME_UTC": TS_UTC,
    "ANSWER_TIME": TS,
    "END_TIME": TS,
    "ACW_END_TIME": TS,
    "IVR_TIME": pa.int32(),
    "QUEUE_TIME": pa.int32(),
    "RING_TIME": pa.int32(),
    "TALK_TIME": pa.int32(),
    "HOLD_TIME": pa.int32(),
    "HOLD_COUNT": pa.int16(),
    "ACW_TIME": pa.int32(),
    "DURATION": pa.int32(),
    "HANDLE_TIME": pa.int32(),
    "INTERVAL_15MIN": TS,
    "ARRIVE_HOUR": pa.int8(),
    # ---- routing ----
    "ROUTE_POINT": pa.string(),
    "DNIS": pa.string(),
    "ANI": pa.string(),
    "QUEUE_ID": pa.string(),
    "QUEUE_NAME": pa.string(),
    "VQ_ID": pa.int32(),
    "VQ_NAME": pa.string(),
    "ORIGINAL_VQ_NAME": pa.string(),
    "SKILL": pa.string(),
    "ROUTING_METHOD": pa.string(),
    "PBR_SCORE": pa.float32(),
    # ---- resource ----
    "AGENT_ID": pa.string(),
    "AGENT_NAME": pa.string(),
    "AGENT_GROUP": pa.string(),
    "AGENT_TENURE_BAND": pa.string(),
    "SITE": pa.string(),
    "LOB": pa.string(),
    # ---- customer ----
    "CUSTOMER_ID": pa.string(),
    "CUSTOMER_SEGMENT": pa.string(),
    # ---- flags & counts ----
    "ANSWERED_FLAG": pa.int8(),
    "ABANDON_FLAG": pa.int8(),
    "SHORT_ABANDON_FLAG": pa.int8(),
    "RONA_FLAG": pa.int8(),
    "OVERFLOW_FLAG": pa.int8(),
    "TRANSFER_FLAG": pa.int8(),
    "TRANSFER_IN_FLAG": pa.int8(),
    "TRANSFER_TYPE": pa.string(),
    "TRANSFER_TO": pa.string(),
    "TRANSFER_COUNT": pa.int16(),
    "CONFERENCE_FLAG": pa.int8(),
    "CONSULT_FLAG": pa.int8(),
    "CONSULT_RECEIVED_FLAG": pa.int8(),
    "CALLBACK_FLAG": pa.int8(),
    "CALLBACK_REQUESTED_FLAG": pa.int8(),
    "SERVICE_LEVEL_FLAG": pa.int8(),
    "FIRST_CALL_FLAG": pa.int8(),
    "REPEAT_CALL_7D_FLAG": pa.int8(),
    "N_AGENT": pa.int8(),
    "N_CUSTOMER": pa.int8(),
}

SCHEMA = pa.schema([pa.field(n, t, nullable=True) for n, t in COLUMNS.items()])

FLAG_COLUMNS: List[str] = [c for c in COLUMNS if c.endswith("_FLAG")]
ZERO_DEFAULT: List[str] = FLAG_COLUMNS + [
    "IVR_TIME", "QUEUE_TIME", "RING_TIME", "TALK_TIME", "HOLD_TIME", "HOLD_COUNT", "ACW_TIME",
    "DURATION", "HANDLE_TIME", "TRANSFER_COUNT", "N_AGENT", "N_CUSTOMER",
]

# CALL_RESULT -> CALL_RESULT_CODE
CALL_RESULT_CODES: Dict[str, int] = {
    "Answered": 1,
    "Abandoned": 2,
    "ShortAbandon": 3,
    "Transferred": 4,
    "Conferenced": 5,
    "RONA": 6,
    "SelfService": 7,
    "AfterHours": 8,
    "Voicemail": 9,
    "CallbackRequested": 10,
    "SystemError": 11,
    "NoAnswer": 12,
    "Busy": 13,
    "AnsweringMachine": 14,
    "Consulted": 15,
    "DialerDrop": 16,
    "Completed": 17,
}


def new_row() -> Dict[str, Any]:
    row: Dict[str, Any] = dict.fromkeys(COLUMNS, None)
    for c in ZERO_DEFAULT:
        row[c] = 0
    row["SERVICE_LEVEL_FLAG"] = None
    row["MEDIA_TYPE"] = "voice"
    return row
