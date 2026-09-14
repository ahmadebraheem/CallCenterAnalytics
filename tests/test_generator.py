import json
import os

import pyarrow.compute as pc
import pyarrow.dataset as ds
import pytest

from gim_synth.arrivals import build_day_profile, expected_wait_curve
from gim_synth.config import default_config, load_config
from gim_synth.generator import generate
from gim_synth.refdata import build_refdata
from gim_synth.schema import COLUMNS
from gim_synth.validate import validate_table

import numpy as np
from datetime import date


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    cfg = load_config(overrides={
        "start_date": "2026-08-03", "days": 2, "calls_per_day": 4000, "seed": 11,
        "customers": {"pool_size": 20000},
        "output": {"directory": str(out)},
    })
    stats = generate(cfg)
    table = ds.dataset(os.path.join(out, "interaction_resource_fact"), format="parquet",
                       partitioning="hive").to_table()
    return cfg, stats, table, out


def test_config_roundtrip_and_validation():
    cfg = default_config()
    cfg.validate()
    with pytest.raises(ValueError):
        load_config(overrides={"days": 0})
    with pytest.raises(ValueError):
        load_config(overrides={"vqs": [{"name": "VQ_X", "lob": "Nope", "skill": "s", "weight": 1}]})
    with pytest.raises(ValueError):
        load_config(overrides={"queue_model": {"not_a_key": 1}})
    with pytest.raises(ValueError):
        load_config(overrides={"vq_weights": {"VQ_Does_Not_Exist": 0.5}})
    cfg = load_config(overrides={"vq_weights": {"VQ_Sales_New": 0.5}})
    assert {v.name: v.weight for v in cfg.vqs}["VQ_Sales_New"] == 0.5


def test_default_vq_split_is_skewed():
    cfg = default_config()
    weights = sorted((v.weight for v in cfg.vqs), reverse=True)
    total = sum(weights)
    assert weights[0] / total >= 0.25
    assert sum(weights[:3]) / total >= 0.55
    assert weights[-1] / total <= 0.005


def test_outputs_written(small_run):
    cfg, stats, table, out = small_run
    assert stats["rows"] > 0
    assert os.path.exists(os.path.join(out, "_manifest.json"))
    for dim in ("dim_site", "dim_lob", "dim_vq", "dim_agent", "dim_customer"):
        assert os.path.exists(os.path.join(out, f"{dim}.parquet"))
    parts = sorted(os.listdir(os.path.join(out, "interaction_resource_fact")))
    assert parts == ["call_date=2026-08-03", "call_date=2026-08-04"]
    manifest = json.load(open(os.path.join(out, "_manifest.json")))
    assert manifest["rows"] == table.num_rows


def test_schema_and_invariants(small_run):
    cfg, stats, table, out = small_run
    assert set(table.column_names) >= set(COLUMNS)
    sl = {v.name: v.service_level_s for v in cfg.vqs}
    assert validate_table(table.select(list(COLUMNS)), cfg, sl) == []


def test_scenario_coverage(small_run):
    cfg, stats, table, out = small_run
    scen = set(table["SCENARIO"].to_pylist())
    expected = {
        "normal", "short", "long", "multi_hold", "system_drop", "abandon_queue", "abandon_short", "rona",
        "blind_transfer", "warm_transfer", "warm_transfer_consult", "warm_transfer_received", "consult_only",
        "conference", "conference_consult", "conference_joined", "external_transfer", "external_transfer_leg",
        "ivr_self_service", "after_hours", "outbound_manual", "internal_call", "direct_did",
        "dialer_answered", "dialer_noanswer",
    }
    missing = expected - scen
    assert not missing, f"missing scenarios: {missing}"
    assert set(table["CALL_TYPE"].to_pylist()) == {"Inbound", "Outbound", "Internal", "Consult"}
    assert set(table["MEDIA_TYPE"].to_pylist()) == {"voice"}


def test_lineage(small_run):
    cfg, stats, table, out = small_run
    consult = table.filter(pc.equal(table["CALL_TYPE"], "Consult"))
    assert consult.num_rows > 0
    roots = set(table["ROOT_INTERACTION_ID"].to_pylist())
    parents = set(consult["PARENT_INTERACTION_ID"].to_pylist())
    assert parents <= roots
    call_ids = set(table["CALL_ID"].to_pylist())
    prev = [p for p in table["PREVIOUS_CALL_ID"].to_pylist() if p is not None]
    assert set(prev) <= call_ids
    transferred_in = table.filter(pc.equal(table["TRANSFER_IN_FLAG"], 1))
    assert transferred_in.num_rows > 0
    assert min(transferred_in["SEGMENT_SEQ"].to_pylist()) >= 2


def test_volume_matches_config(small_run):
    cfg, stats, table, out = small_run
    per_day = [d["interactions"] for d in stats["days"]]
    for n in per_day:
        assert n >= cfg.calls_per_day * 0.6


def test_vq_volumes_are_unbalanced(small_run):
    cfg, stats, table, out = small_run
    inbound = table.filter(pc.and_(pc.equal(table["CALL_TYPE"], "Inbound"), pc.is_valid(table["VQ_NAME"])))
    g = inbound.group_by("VQ_NAME").aggregate([("IRF_ID", "count")])
    counts = dict(zip(g["VQ_NAME"].to_pylist(), g["IRF_ID_count"].to_pylist()))
    assert len(counts) == len(cfg.vqs), "every VQ should receive some traffic"
    ordered = sorted(counts.values(), reverse=True)
    total = sum(ordered)
    assert ordered[0] / total > 0.2, "biggest VQ should dominate"
    assert sum(ordered[:3]) / total > 0.5, "top-3 VQs should carry most of the volume"
    assert ordered[0] > 25 * ordered[-1], "long tail VQs should be tiny compared with the biggest"


def test_burst_and_lull_profile():
    cfg = load_config(overrides={"bursts": {"rate_per_day": 3.0, "mega_prob": 1.0}, "lulls": {"rate_per_day": 3.0}})
    rng = np.random.default_rng(5)
    p = build_day_profile(cfg, date(2026, 8, 4), rng)
    kinds = {e["type"] for e in p.events}
    assert {"burst", "mega_burst", "lull"} <= kinds
    assert p.minute_counts.max() > 20 * np.median(p.minute_counts[8 * 60:20 * 60])


def test_expected_wait_grows_with_load():
    cfg = default_config()
    calm = expected_wait_curve(np.full(1440, 2.0), np.full(1440, 100.0), 400.0, 10.0, cfg)
    flooded = expected_wait_curve(np.full(1440, 200.0), np.full(1440, 100.0), 400.0, 10.0, cfg)
    assert calm.max() <= 10.0 + 1e-6
    assert flooded.min() > 100.0


def test_roster_covers_open_hours():
    cfg = default_config()
    ref = build_refdata(cfg)
    for v in ref.vqs:
        for dow in range(7):
            for hour in range(24):
                if v.is_open(dow, hour):
                    assert ref.staffed(v.idx, dow, hour) >= cfg.staffing.min_agents_per_open_hour
