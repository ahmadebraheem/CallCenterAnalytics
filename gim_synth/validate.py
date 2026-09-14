"""Invariant checks on a generated day table (pure pyarrow, no pandas needed)."""
from __future__ import annotations

from typing import List

import pyarrow as pa
import pyarrow.compute as pc

from .config import GeneratorConfig


class ValidationError(Exception):
    pass


def _count_true(mask) -> int:
    return int(pc.sum(pc.fill_null(mask, False)).as_py() or 0)


def validate_table(t: pa.Table, cfg: GeneratorConfig, service_levels: dict) -> List[str]:
    """Return a list of failed check descriptions (empty when everything is consistent)."""
    failures: List[str] = []

    def check(name: str, bad_mask) -> None:
        bad = _count_true(bad_mask)
        if bad:
            failures.append(f"{name}: {bad} rows")

    c = {name: t[name] for name in t.column_names}
    answered = pc.equal(c["ANSWERED_FLAG"], 1)
    not_answered = pc.equal(c["ANSWERED_FLAG"], 0)
    abandoned = pc.equal(c["ABANDON_FLAG"], 1)

    # ---- uniqueness ---- #
    if len(pc.unique(c["IRF_ID"])) != t.num_rows:
        failures.append("IRF_ID not unique")
    if len(pc.unique(c["CALL_ID"])) != t.num_rows:
        failures.append("CALL_ID not unique")

    # ---- timing ---- #
    check("ANSWER_TIME present iff answered",
          pc.xor(answered, pc.is_valid(c["ANSWER_TIME"])))
    check("END_TIME >= ARRIVE_TIME", pc.less(c["END_TIME"], c["ARRIVE_TIME"]))
    check("ANSWER_TIME >= ARRIVE_TIME", pc.less(c["ANSWER_TIME"], c["ARRIVE_TIME"]))
    check("END_TIME >= ANSWER_TIME", pc.less(c["END_TIME"], c["ANSWER_TIME"]))
    dur_ms = pc.cast(pc.subtract(pc.cast(c["END_TIME"], pa.int64()), pc.cast(c["ARRIVE_TIME"], pa.int64())), pa.int64())
    check("DURATION == END - ARRIVE", pc.not_equal(pc.divide(dur_ms, 1000), pc.cast(c["DURATION"], pa.int64())))
    check("HANDLE_TIME == TALK + HOLD + ACW",
          pc.not_equal(c["HANDLE_TIME"], pc.add(pc.add(c["TALK_TIME"], c["HOLD_TIME"]), c["ACW_TIME"])))
    check("unanswered legs have zero talk/hold/acw",
          pc.and_(not_answered, pc.greater(pc.add(pc.add(c["TALK_TIME"], c["HOLD_TIME"]), c["ACW_TIME"]), 0)))
    check("ACW_END_TIME present iff ACW_TIME > 0",
          pc.xor(pc.greater(c["ACW_TIME"], 0), pc.is_valid(c["ACW_END_TIME"])))
    for col in ("IVR_TIME", "QUEUE_TIME", "RING_TIME", "TALK_TIME", "HOLD_TIME", "ACW_TIME", "DURATION"):
        check(f"{col} >= 0", pc.less(c[col], 0))

    # ---- abandon semantics ---- #
    check("abandoned legs are not answered", pc.and_(abandoned, answered))
    check("SHORT_ABANDON implies ABANDON", pc.and_(pc.equal(c["SHORT_ABANDON_FLAG"], 1), pc.equal(c["ABANDON_FLAG"], 0)))
    check("short abandon under threshold",
          pc.and_(pc.equal(c["SHORT_ABANDON_FLAG"], 1), pc.greater_equal(c["QUEUE_TIME"], cfg.short_abandon_threshold_s)))
    check("queue abandons carry no agent",
          pc.and_(pc.and_(abandoned, pc.equal(c["RESOURCE_TYPE"], "Queue")), pc.is_valid(c["AGENT_ID"])))

    # ---- lineage ---- #
    check("SEGMENT_SEQ 1 has no PREVIOUS_CALL_ID",
          pc.and_(pc.equal(c["SEGMENT_SEQ"], 1), pc.is_valid(c["PREVIOUS_CALL_ID"])))
    check("SEGMENT_SEQ > 1 has PREVIOUS_CALL_ID",
          pc.and_(pc.greater(c["SEGMENT_SEQ"], 1), pc.is_null(c["PREVIOUS_CALL_ID"])))
    check("PARENT_INTERACTION_ID only on Consult legs",
          pc.xor(pc.is_valid(c["PARENT_INTERACTION_ID"]), pc.equal(c["CALL_TYPE"], "Consult")))
    check("PARENT_CALL_ID present iff PARENT_INTERACTION_ID present",
          pc.xor(pc.is_valid(c["PARENT_CALL_ID"]), pc.is_valid(c["PARENT_INTERACTION_ID"])))
    check("Consult legs have INTERACTION_ID != ROOT_INTERACTION_ID",
          pc.and_(pc.equal(c["CALL_TYPE"], "Consult"), pc.equal(c["INTERACTION_ID"], c["ROOT_INTERACTION_ID"])))
    check("non-consult legs share ROOT_INTERACTION_ID",
          pc.and_(pc.not_equal(c["CALL_TYPE"], "Consult"), pc.not_equal(c["INTERACTION_ID"], c["ROOT_INTERACTION_ID"])))
    check("RELATED_INTERACTION_ID only on callbacks",
          pc.xor(pc.is_valid(c["RELATED_INTERACTION_ID"]), pc.equal(c["CALLBACK_FLAG"], 1)))
    check("TRANSFER_IN implies TRANSFER_COUNT >= 1",
          pc.and_(pc.equal(c["TRANSFER_IN_FLAG"], 1), pc.less(c["TRANSFER_COUNT"], 1)))
    check("TRANSFER_FLAG implies TRANSFER_TYPE",
          pc.and_(pc.equal(c["TRANSFER_FLAG"], 1), pc.is_null(c["TRANSFER_TYPE"])))
    check("TRANSFER_FLAG implies TRANSFER_TO",
          pc.and_(pc.equal(c["TRANSFER_FLAG"], 1), pc.is_null(c["TRANSFER_TO"])))

    # ---- resource semantics ---- #
    check("Agent resource legs have AGENT_ID",
          pc.and_(pc.equal(c["RESOURCE_TYPE"], "Agent"), pc.is_null(c["AGENT_ID"])))
    check("non-Agent resource legs have no AGENT_ID",
          pc.and_(pc.not_equal(c["RESOURCE_TYPE"], "Agent"), pc.is_valid(c["AGENT_ID"])))
    check("N_AGENT >= 1 when AGENT_ID present",
          pc.and_(pc.is_valid(c["AGENT_ID"]), pc.less(c["N_AGENT"], 1)))
    check("Consult legs have N_CUSTOMER == 0",
          pc.and_(pc.equal(c["CALL_TYPE"], "Consult"), pc.not_equal(c["N_CUSTOMER"], 0)))
    check("Internal legs have N_CUSTOMER == 0",
          pc.and_(pc.equal(c["CALL_TYPE"], "Internal"), pc.not_equal(c["N_CUSTOMER"], 0)))
    check("Agent legs have ROUTING_METHOD",
          pc.and_(pc.equal(c["RESOURCE_TYPE"], "Agent"), pc.is_null(c["ROUTING_METHOD"])))
    check("PBR_SCORE present iff PBR-routed agent leg",
          pc.xor(pc.is_valid(c["PBR_SCORE"]),
                 pc.and_(pc.equal(c["ROUTING_METHOD"], "PBR"), pc.is_valid(c["AGENT_ID"]))))
    check("voice only", pc.not_equal(c["MEDIA_TYPE"], "voice"))
    check("CALL_RESULT_CODE present", pc.is_null(c["CALL_RESULT_CODE"]))

    # ---- service level ---- #
    sl_set = pc.equal(c["SERVICE_LEVEL_FLAG"], 1)
    check("SERVICE_LEVEL_FLAG=1 only on answered legs", pc.and_(sl_set, not_answered))
    if service_levels:
        thresholds = pa.array([service_levels.get(v) for v in c["VQ_NAME"].to_pylist()], pa.int32())
        speed = pc.add(c["QUEUE_TIME"], c["RING_TIME"])
        check("SERVICE_LEVEL_FLAG=1 within VQ threshold", pc.and_(sl_set, pc.greater(speed, thresholds)))
        check("SERVICE_LEVEL_FLAG=0 answered legs are outside threshold",
              pc.and_(pc.and_(pc.equal(c["SERVICE_LEVEL_FLAG"], 0), answered), pc.less_equal(speed, thresholds)))
    return failures
