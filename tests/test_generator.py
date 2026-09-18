import json
import math
import os

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pytest

from gim_synth.arrivals import build_day_profile, expected_wait_curve
from gim_synth.config import config_to_dict, default_config, load_config
from gim_synth.generator import generate
from gim_synth.refdata import build_refdata
from gim_synth.schema import COLUMNS, OUTCOME_COLUMNS
from gim_synth.validate import validate_outcomes, validate_table

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


@pytest.fixture(scope="module")
def outcomes(small_run):
    cfg, stats, table, out = small_run
    return ds.dataset(os.path.join(out, "interaction_outcome_fact"), format="parquet", partitioning="hive").to_table()


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


def test_outputs_written(small_run, outcomes):
    cfg, stats, table, out = small_run
    assert stats["rows"] > 0
    assert os.path.exists(os.path.join(out, "_manifest.json"))
    for dim in ("dim_site", "dim_lob", "dim_vq", "dim_agent", "dim_customer"):
        assert os.path.exists(os.path.join(out, f"{dim}.parquet"))
    parts = sorted(os.listdir(os.path.join(out, "interaction_resource_fact")))
    assert parts == ["call_date=2026-08-03", "call_date=2026-08-04"]
    assert sorted(os.listdir(os.path.join(out, "interaction_outcome_fact"))) == parts
    manifest = json.load(open(os.path.join(out, "_manifest.json")))
    assert manifest["rows"] == table.num_rows
    assert manifest["outcome_rows"] == outcomes.num_rows > 0
    assert set(manifest["outcome_polarity_counts"]) == {"Positive", "Neutral", "Negative"}
    assert manifest["amount_totals_by_type"]["Revenue"] > 0


def test_data_dictionary_covers_every_column(small_run, outcomes):
    import csv
    import pyarrow.parquet as pq
    from gim_synth.dictionary import COLUMN_DESCRIPTIONS, dictionary_markdown

    cfg, stats, table, out = small_run
    with open(os.path.join(out, "_data_dictionary.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    tables = {"interaction_resource_fact": table, "interaction_outcome_fact": outcomes}
    for dim in ("dim_site", "dim_lob", "dim_vq", "dim_agent", "dim_customer"):
        tables[dim] = pq.read_table(os.path.join(out, f"{dim}.parquet"))
    assert set(r["TABLE_NAME"] for r in rows) == set(tables) == set(COLUMN_DESCRIPTIONS)
    for name, t in tables.items():
        if "call_date" in t.column_names:  # hive partition column added by the dataset reader
            t = t.drop_columns(["call_date"])
        described = [r for r in rows if r["TABLE_NAME"] == name]
        assert [r["COLUMN_NAME"] for r in described] == t.column_names, name
        assert [r["DATA_TYPE"] for r in described] == [str(f.type) for f in t.schema], name
        assert all(r["DESCRIPTION"].strip() for r in described), name
        for r in described:
            if r["NULLABLE"] == "no":
                assert t[r["COLUMN_NAME"]].null_count == 0, f"{name}.{r['COLUMN_NAME']} documented as non-null"
    md = dictionary_markdown([{**r, "ORDINAL": int(r["ORDINAL"])} for r in rows])
    assert md.count("\n## ") + md.startswith("## ") == len(tables)


def test_schema_and_invariants(small_run, outcomes):
    cfg, stats, table, out = small_run
    assert set(table.column_names) >= set(COLUMNS)
    assert set(outcomes.column_names) >= set(OUTCOME_COLUMNS)
    sl = {v.name: v.service_level_s for v in cfg.vqs}
    fact = table.select(list(COLUMNS))
    assert validate_table(fact, cfg, sl) == []
    assert validate_outcomes(outcomes.select(list(OUTCOME_COLUMNS)), fact) == []


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


def _agent_imbalance(table, method):
    sub = table.filter(pc.and_(pc.equal(table["ROUTING_METHOD"], method), pc.equal(table["ANSWERED_FLAG"], 1)))
    counts = sorted(sub.group_by("AGENT_ID").aggregate([("IRF_ID", "count")])["IRF_ID_count"].to_pylist())
    dec = max(1, len(counts) // 10)
    return sum(counts[-dec:]) / max(1, sum(counts[:dec])), sub.num_rows


def test_pbr_skews_agent_distribution(small_run):
    cfg, stats, table, out = small_run
    inbound = table.filter(pc.and_(pc.equal(table["CALL_TYPE"], "Inbound"), pc.is_valid(table["VQ_NAME"])))
    acd_ratio, n_acd = _agent_imbalance(inbound, "ACD")
    pbr_ratio, n_pbr = _agent_imbalance(inbound, "PBR")
    assert n_acd > 0 and n_pbr > 0
    assert pbr_ratio > 2 * acd_ratio, f"PBR should be markedly more lopsided (acd={acd_ratio:.1f}, pbr={pbr_ratio:.1f})"
    pbr_rows = table.filter(pc.equal(table["ROUTING_METHOD"], "PBR"))
    pbr_vqs = {v.name for v in cfg.vqs if v.pbr_enabled}
    assert set(pbr_rows["VQ_NAME"].to_pylist()) <= pbr_vqs
    scored = pbr_rows.filter(pc.is_valid(pbr_rows["PBR_SCORE"]))
    assert scored.num_rows > 0
    assert pc.mean(scored["PBR_SCORE"]).as_py() > 0.6, "PBR should favour high-scoring agents"
    assert set(table.filter(pc.equal(table["RESOURCE_TYPE"], "Agent"))["ROUTING_METHOD"].to_pylist()) <= {
        "ACD", "PBR", "Direct", "Consult", "Conference", "Manual", "Dialer", "Internal", "Callback"}


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


def test_vq_overrides():
    cfg = load_config(overrides={"vq_overrides": {"VQ_Tech_Tier1": {"pbr_enabled": True, "pbr_skew": 1.1}}})
    v = {v.name: v for v in cfg.vqs}["VQ_Tech_Tier1"]
    assert v.pbr_enabled is True and v.pbr_skew == 1.1
    with pytest.raises(ValueError):
        load_config(overrides={"vq_overrides": {"VQ_Tech_Tier1": {"nope": 1}}})
    with pytest.raises(ValueError):
        load_config(overrides={"vq_overrides": {"VQ_Nope": {"pbr_enabled": True}}})


def test_full_config_yaml_matches_defaults():
    import math
    from gim_synth.config import config_to_dict

    def flat(d, prefix=""):
        if isinstance(d, dict):
            for k, v in d.items():
                yield from flat(v, f"{prefix}/{k}")
        elif isinstance(d, list):
            for i, v in enumerate(d):
                yield from flat(v, f"{prefix}[{i}]")
        else:
            yield prefix, d

    a = dict(flat(config_to_dict(load_config("configs/full_config.yaml"))))
    b = dict(flat(config_to_dict(default_config())))
    assert a.keys() == b.keys()
    for k in a:
        if isinstance(a[k], float) or isinstance(b[k], float):
            assert math.isclose(a[k], b[k]), k
        else:
            assert a[k] == b[k], k


def test_catalogue_manual_fills_auto_fields():
    cfg = load_config(overrides={
        "catalogue": {"mode": "manual", "manual": {"auto_weight_share": 0.25, "volume_skew": 1.0}},
        "vqs": [
            {"name": "VQ_Sales_Main", "lob": "Sales", "weight": 0.6, "pbr_enabled": True},
            {"name": "VQ_Sales_Web", "lob": "Sales", "weight": "auto", "overflow_vq": "auto"},
            {"name": "VQ_Help", "lob": "HelpDesk", "weight": 0.15, "open_hour": 9, "close_hour": 17, "days": "Mon-Fri"},
            {"name": "VQ_Help_VIP", "lob": "HelpDesk", "weight": "auto"},
        ],
        "lobs": [{"name": "HelpDesk", "short": "HLP"}],
    })
    by = {v.name: v for v in cfg.vqs}
    assert set(by) == {"VQ_Sales_Main", "VQ_Sales_Web", "VQ_Help", "VQ_Help_VIP"}
    assert {l.name for l in cfg.lobs} == {"Sales", "HelpDesk"}          # Sales added from the VQs
    # auto weights: 25 % of total between the two, split 1 : 1/2 in listing order
    total = sum(v.weight for v in cfg.vqs)
    assert math.isclose((by["VQ_Sales_Web"].weight + by["VQ_Help_VIP"].weight) / total, 0.25, rel_tol=1e-6)
    assert math.isclose(by["VQ_Sales_Web"].weight / by["VQ_Help_VIP"].weight, 2.0, rel_tol=1e-4)
    # template values from the Sales library queue; explicit values kept
    assert (by["VQ_Sales_Web"].open_hour, by["VQ_Sales_Web"].close_hour) == (8, 21)
    assert by["VQ_Sales_Web"].talk_median_s == 330 and by["VQ_Sales_Web"].pbr_enabled is True
    assert by["VQ_Sales_Web"].overflow_vq == "VQ_Sales_Main" and by["VQ_Sales_Main"].overflow_vq is None
    assert (by["VQ_Help"].open_hour, by["VQ_Help"].close_hour, by["VQ_Help"].days) == (9, 17, "Mon-Fri")
    assert (by["VQ_Help_VIP"].open_hour, by["VQ_Help_VIP"].close_hour) == (8, 20)   # generic hours
    assert by["VQ_Help_VIP"].skill == "SK_HLP_VIP" and by["VQ_Help_VIP"].home_site in {s.name for s in cfg.sites}
    help_lob = next(l for l in cfg.lobs if l.name == "HelpDesk")
    assert help_lob.short == "HLP" and set(help_lob.business_outcomes) == {"Resolved", "FollowUp", "Escalated", "Info", "Unresolved"}
    assert set(help_lob.transfer_targets) == {"VQ_Help_VIP", "VQ_Sales_Main"}
    assert set(cfg.outbound.campaigns.values()) <= {"Sales", "HelpDesk"}     # built-in campaigns for absent LOBs dropped
    assert "Sales" in cfg.outbound.campaigns.values()
    with pytest.raises(ValueError, match="no `vqs:`"):
        load_config(overrides={"catalogue": {"mode": "manual"}})
    with pytest.raises(ValueError, match="unknown fields"):
        load_config(overrides={"catalogue": {"mode": "manual"}, "vqs": [{"name": "X", "lob": "Sales", "talk": 3}]})


def test_catalogue_auto_designs_unbalanced_catalogue():
    cfg = load_config(overrides={"catalogue": {"mode": "auto", "auto": {
        "n_vqs": 12, "lobs": ["Sales", "TechSupport", "Fraud"], "lob_shares": {"Fraud": 0.1},
        "pbr_share": 0.25, "always_open_share": 0.25, "weekday_only_share": 0.25, "overflow_share": 1.0}}})
    assert len(cfg.vqs) == 12 and {l.name for l in cfg.lobs} == {"Sales", "TechSupport", "Fraud"}
    assert len({v.name for v in cfg.vqs}) == 12
    per_lob = {l.name: sorted((v for v in cfg.vqs if v.lob == l.name), key=lambda v: -v.weight) for l in cfg.lobs}
    assert all(per_lob[l] for l in per_lob)
    assert per_lob["Sales"][0].name == "VQ_Sales_New" and per_lob["TechSupport"][0].name == "VQ_Tech_Tier1"
    # Zipf inside each LOB: strictly decreasing, leader clearly dominant
    for vqs in per_lob.values():
        assert all(a.weight > b.weight for a, b in zip(vqs, vqs[1:]))
    assert per_lob["Sales"][0].weight > 2 * per_lob["Sales"][-1].weight
    assert sum(1 for v in cfg.vqs if v.pbr_enabled) == 3
    assert all(v.lob == "Sales" for v in cfg.vqs if v.pbr_enabled)
    assert sum(1 for v in cfg.vqs if v.open_hour == 0 and v.close_hour == 24) == 3
    assert all(v.lob == "TechSupport" for v in cfg.vqs if v.open_hour == 0 and v.close_hour == 24)
    assert sum(1 for v in cfg.vqs if v.days == "Mon-Fri") == 3
    # every non-leader overflows to its LOB leader, leaders do not overflow
    for lob, vqs in per_lob.items():
        assert vqs[0].overflow_vq is None
        assert all(v.overflow_vq == vqs[0].name for v in vqs[1:])
    # niche queues talk longer than their LOB leader
    assert per_lob["Sales"][-1].talk_median_s > per_lob["Sales"][0].talk_median_s
    fraud = next(l for l in cfg.lobs if l.name == "Fraud")
    assert fraud.short == "FRA" and fraud.business_outcomes
    with pytest.raises(ValueError, match="n_vqs"):
        load_config(overrides={"catalogue": {"mode": "auto", "auto": {"n_vqs": 2, "lobs": ["Sales", "Billing", "Fraud"]}}})
    with pytest.raises(ValueError, match="lob_shares"):
        load_config(overrides={"catalogue": {"mode": "auto", "auto": {"lobs": ["Sales"], "lob_shares": {"Nope": 1}}}})
    with pytest.raises(ValueError, match="catalogue.mode"):
        load_config(overrides={"catalogue": {"mode": "magic"}})


@pytest.mark.parametrize("config_file", ["configs/catalogue_manual.yaml", "configs/catalogue_auto.yaml"])
def test_catalogue_example_configs_generate(tmp_path, config_file):
    cfg = load_config(config_file, overrides={"days": 1, "start_date": "2026-08-04", "calls_per_day": 3000,
                                              "customers": {"pool_size": 5000},
                                              "output": {"directory": str(tmp_path)}})
    stats = generate(cfg)
    assert stats["rows"] > 0 and stats["outcome_rows"] > 0
    t = ds.dataset(os.path.join(tmp_path, "interaction_resource_fact"), format="parquet", partitioning="hive").to_table()
    seen = set(t["VQ_NAME"].to_pylist()) - {None}
    assert seen <= {v.name for v in cfg.vqs} and len(seen) >= len(cfg.vqs) - 1
    assert set(t["LOB"].to_pylist()) - {None} <= {l.name for l in cfg.lobs}


def test_default_mode_unchanged_by_catalogue_resolution():
    a = config_to_dict(default_config())
    b = config_to_dict(load_config())
    assert a == b


def test_per_vq_behaviour_overrides(tmp_path):
    cfg = load_config(overrides={
        "start_date": "2026-08-04", "days": 1, "calls_per_day": 4000, "seed": 5,
        "customers": {"pool_size": 5000},
        "bursts": {"rate_per_day": 0, "mega_prob": 0}, "lulls": {"rate_per_day": 0},
        "vq_overrides": {
            "VQ_Tech_Tier1": {"rona_prob": 0.0, "short_abandon_prob": 0.25, "abandon_while_ringing_prob": 0.0},
            "VQ_Tech_Tier2": {"rona_prob": 0.3, "wait_scale": 4.0},
        },
        "output": {"directory": str(tmp_path)},
    })
    generate(cfg)
    t = ds.dataset(os.path.join(tmp_path, "interaction_resource_fact"), format="parquet",
                   partitioning="hive").to_table()
    t1 = t.filter(pc.equal(t["VQ_NAME"], "VQ_Tech_Tier1"))
    t2 = t.filter(pc.equal(t["VQ_NAME"], "VQ_Tech_Tier2"))
    assert pc.sum(t1["RONA_FLAG"]).as_py() == 0
    assert pc.sum(t1["SHORT_ABANDON_FLAG"]).as_py() / t1.num_rows > 0.15
    assert pc.sum(t2["RONA_FLAG"]).as_py() / t2.num_rows > 0.1
    other = t.filter(pc.and_(pc.is_valid(t["VQ_NAME"]), pc.not_equal(t["VQ_NAME"], "VQ_Tech_Tier2")))
    assert pc.mean(t2["QUEUE_TIME"]).as_py() > 2 * pc.mean(other["QUEUE_TIME"]).as_py()


# --------------------------------------------------------------------------- #
# business outcomes
# --------------------------------------------------------------------------- #
def _positive_rate(t) -> float:
    return pc.sum(pc.cast(pc.equal(t["OUTCOME_POLARITY"], "Positive"), "int64")).as_py() / max(1, t.num_rows)


def test_outcomes_cover_every_handled_leg_and_only_those(small_run, outcomes):
    cfg, stats, table, out = small_run
    handled = table.filter(pc.and_(pc.and_(pc.equal(table["ANSWERED_FLAG"], 1), pc.is_valid(table["AGENT_ID"])),
                                   pc.and_(pc.equal(table["N_CUSTOMER"], 1),
                                           pc.is_in(table["CALL_RESULT"], pa.array(["Answered", "Conferenced"])))))
    # legs that talked to a customer but do not own the outcome
    no_outcome = handled.filter(pc.is_in(handled["DISPOSITION"], pa.array(["Assisted", "LeftMessage"])))
    owners = handled.filter(pc.invert(pc.is_in(handled["DISPOSITION"], pa.array(["Assisted", "LeftMessage"]))))
    primary = outcomes.filter(pc.equal(outcomes["OUTCOME_SEQ"], 1))
    assert set(primary["IRF_ID"].to_pylist()) == set(owners["IRF_ID"].to_pylist())
    assert primary.num_rows == owners.num_rows
    assert not set(no_outcome["IRF_ID"].to_pylist()) & set(outcomes["IRF_ID"].to_pylist())
    # transferred-out legs never carry an outcome
    transferred = table.filter(pc.equal(table["TRANSFER_FLAG"], 1))
    assert not set(transferred["IRF_ID"].to_pylist()) & set(outcomes["IRF_ID"].to_pylist())
    # every LOB and every outbound / callback / direct path produces outcomes
    assert set(outcomes["LOB"].to_pylist()) == {l.name for l in cfg.lobs}
    assert {"Inbound", "Outbound"} <= set(outcomes["CALL_TYPE"].to_pylist())
    assert {"outbound_manual", "dialer_answered", "direct_did", "warm_transfer_received", "conference"} <= set(
        outcomes["SCENARIO"].to_pylist())


def test_outcome_catalogue_and_fields(small_run, outcomes):
    cfg, stats, table, out = small_run
    for l in cfg.lobs:
        sub = outcomes.filter(pc.and_(pc.equal(outcomes["LOB"], l.name), pc.equal(outcomes["OUTCOME_SEQ"], 1)))
        assert set(sub["BUSINESS_RESULT"].to_pylist()) == set(l.business_outcomes), l.name
        for name, o in l.business_outcomes.items():
            rows = sub.filter(pc.equal(sub["BUSINESS_RESULT"], name))
            if rows.num_rows == 0:
                continue
            assert set(rows["OUTCOME_POLARITY"].to_pylist()) == {o.polarity}
            assert set(rows["OUTCOME_CATEGORY"].to_pylist()) == {o.category}
            if o.amount_type:
                assert set(rows["AMOUNT_TYPE"].to_pylist()) == {o.amount_type}
                assert pc.min(rows["AMOUNT"]).as_py() > 0
                assert set(rows["IN_CALL_FLAG"].to_pylist()) == {1}, "monetary events happen inside the call"
            else:
                assert set(rows["AMOUNT_TYPE"].to_pylist()) == {None}
            if o.subtypes:
                assert set(rows["OUTCOME_SUBTYPE"].to_pylist()) <= set(o.subtypes)
            if o.follow_up_prob == 1.0:
                assert set(rows["FOLLOW_UP_FLAG"].to_pylist()) == {1}
                assert pc.all(pc.greater(rows["FOLLOW_UP_DUE_TIME"], rows["RECORDED_TIME"])).as_py()
                assert all(c.startswith("CS") for c in rows["CASE_ID"].to_pylist())
    secondary = outcomes.filter(pc.equal(outcomes["OUTCOME_SEQ"], 2))
    assert secondary.num_rows > 0
    assert set(secondary["BUSINESS_RESULT"].to_pylist()) == {"CrossSell"}
    assert set(secondary["AMOUNT_TYPE"].to_pylist()) == {"Revenue"}
    # Sales / Retention specifics the user cares about
    sales = outcomes.filter(pc.equal(outcomes["LOB"], "Sales"))
    assert {"Sale", "NoSale", "NotInterested", "CallbackScheduled", "Info"} <= set(sales["BUSINESS_RESULT"].to_pylist())
    ret = outcomes.filter(pc.equal(outcomes["LOB"], "Retention"))
    assert {"Saved", "Cancelled", "Downgraded", "OfferDeclined", "NoChange"} <= set(ret["BUSINESS_RESULT"].to_pylist())
    saved = ret.filter(pc.equal(ret["BUSINESS_RESULT"], "Saved"))
    assert set(saved["AMOUNT_TYPE"].to_pylist()) == {"MRR_Retained"}
    assert set(ret.filter(pc.equal(ret["BUSINESS_RESULT"], "Cancelled"))["AMOUNT_TYPE"].to_pylist()) == {"MRR_Lost"}


def test_outcome_timestamps_align_with_leg(small_run, outcomes):
    cfg, stats, table, out = small_run
    legs = table.select(["IRF_ID", "ANSWER_TIME", "END_TIME", "ACW_END_TIME", "DISPOSITION", "AGENT_ID"]).rename_columns(
        ["IRF_ID", "F_ANSWER", "F_END", "F_ACW_END", "F_DISP", "F_AGENT"])
    j = outcomes.join(legs, keys="IRF_ID", join_type="inner")
    assert j.num_rows == outcomes.num_rows
    assert pc.all(pc.greater_equal(j["OUTCOME_TIME"], j["F_ANSWER"])).as_py()
    assert pc.all(pc.less_equal(j["OUTCOME_TIME"], j["RECORDED_TIME"])).as_py()
    assert pc.all(pc.less_equal(j["RECORDED_TIME"], pc.coalesce(j["F_ACW_END"], j["F_END"]))).as_py()
    in_call = j.filter(pc.equal(j["IN_CALL_FLAG"], 1))
    assert pc.all(pc.less_equal(in_call["OUTCOME_TIME"], in_call["F_END"])).as_py()
    assert pc.all(pc.equal(j["AGENT_ID"], j["F_AGENT"])).as_py()
    prim = j.filter(pc.equal(j["OUTCOME_SEQ"], 1))
    assert pc.all(pc.equal(prim["BUSINESS_RESULT"], prim["F_DISP"])).as_py()


def test_short_calls_never_sell(small_run, outcomes):
    cfg, stats, table, out = small_run
    short = outcomes.filter(pc.equal(outcomes["SCENARIO"], "short"))
    assert short.num_rows > 0
    assert set(short["OUTCOME_POLARITY"].to_pylist()) <= {"Neutral", "Negative"}
    assert set(short["AMOUNT_TYPE"].to_pylist()) == {None}


def test_outcomes_depend_on_agent_quality_and_routing(small_run, outcomes):
    cfg, stats, table, out = small_run
    prim = outcomes.filter(pc.equal(outcomes["OUTCOME_SEQ"], 1))
    low = prim.filter(pc.less(prim["AGENT_PBR_SCORE"], 0.3))
    high = prim.filter(pc.greater(prim["AGENT_PBR_SCORE"], 0.7))
    assert low.num_rows > 200 and high.num_rows > 200
    assert _positive_rate(high) > _positive_rate(low) + 0.1, "good agents must convert markedly better"
    sr = prim.filter(pc.is_in(prim["LOB"], pa.array(["Sales", "Retention"])))
    pbr = sr.filter(pc.equal(sr["ROUTING_METHOD"], "PBR"))
    acd = sr.filter(pc.equal(sr["ROUTING_METHOD"], "ACD"))
    assert pbr.num_rows > 200 and acd.num_rows > 50
    assert _positive_rate(pbr) > _positive_rate(acd), "PBR routes to better agents => better outcomes"
    # Cancelled is less likely with strong agents
    ret = prim.filter(pc.equal(prim["LOB"], "Retention"))
    ret_low = ret.filter(pc.less(ret["AGENT_PBR_SCORE"], 0.4))
    ret_high = ret.filter(pc.greater(ret["AGENT_PBR_SCORE"], 0.6))

    def cancel_rate(t):
        return pc.sum(pc.cast(pc.equal(t["BUSINESS_RESULT"], "Cancelled"), "int64")).as_py() / max(1, t.num_rows)
    assert cancel_rate(ret_low) > cancel_rate(ret_high)


def test_outcomes_disabled_and_agent_effect_off(tmp_path):
    cfg = load_config(overrides={
        "start_date": "2026-08-04", "days": 1, "calls_per_day": 3000, "seed": 9,
        "customers": {"pool_size": 5000}, "outcomes": {"enabled": False},
        "output": {"directory": str(tmp_path / "a")},
    })
    generate(cfg)
    assert not os.path.exists(tmp_path / "a" / "interaction_outcome_fact")
    t = ds.dataset(str(tmp_path / "a" / "interaction_resource_fact"), format="parquet", partitioning="hive").to_table()
    assert "Sale" in set(t["DISPOSITION"].to_pylist()), "DISPOSITION is still drawn from the outcome model"

    cfg = load_config(overrides={
        "start_date": "2026-08-04", "days": 1, "calls_per_day": 3000, "seed": 9,
        "customers": {"pool_size": 5000},
        "outcomes": {"agent_effect_scale": 0.0, "wait_penalty_per_min": 0.0, "cross_sell_prob": 0.0,
                     "segment_lift": {"Consumer": 0, "SMB": 0, "Enterprise": 0, "VIP": 0}},
        "output": {"directory": str(tmp_path / "b")},
    })
    generate(cfg)
    o = ds.dataset(str(tmp_path / "b" / "interaction_outcome_fact"), format="parquet", partitioning="hive").to_table()
    assert o.filter(pc.equal(o["OUTCOME_SEQ"], 2)).num_rows == 0
    # within one LOB (so the PBR routing mix cannot bias the comparison) the agent must not matter
    cs = o.filter(pc.equal(o["LOB"], "CustomerService"))
    low = cs.filter(pc.less(cs["AGENT_PBR_SCORE"], 0.3))
    high = cs.filter(pc.greater(cs["AGENT_PBR_SCORE"], 0.7))
    assert low.num_rows > 150 and high.num_rows > 150
    assert abs(_positive_rate(high) - _positive_rate(low)) < 0.08, "no agent effect when the scale is 0"


def test_outcome_config_validation():
    with pytest.raises(ValueError):
        load_config(overrides={"outcomes": {"cross_sell": {"polarity": "Great"}}})
    with pytest.raises(ValueError):
        load_config(overrides={"outcomes": {"cross_sell": {"amount_type": "Bitcoin"}}})
    with pytest.raises(ValueError):
        load_config(overrides={"outcomes": {"cross_sell_prob": 1.5}})
    with pytest.raises(ValueError):
        load_config(overrides={"outcomes": {"follow_up_days": [5, 1]}})
    cfg = load_config(overrides={"outcomes": {"cross_sell": {"amount_median": 40}}})
    assert cfg.outcomes.cross_sell.amount_median == 40 and cfg.outcomes.cross_sell.category == "Sale"
    # LOB catalogue can be replaced through YAML with the compact per-outcome dict form
    lobs = [dict(name=l.name, short=l.short, outcome_weights=l.outcome_weights, transfer_targets=l.transfer_targets,
                 business_outcomes={"Yes": {"weight": 1, "polarity": "Positive", "category": "Sale"},
                                    "No": {"weight": 1, "polarity": "Negative"}})
            for l in default_config().lobs]
    cfg = load_config(overrides={"lobs": lobs})
    assert set(cfg.lobs[0].business_outcomes) == {"Yes", "No"}
    assert cfg.lobs[0].business_outcomes["No"].category == "NoChange"
