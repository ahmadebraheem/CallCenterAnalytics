"""Command line interface: `python -m gim_synth <command> [options]`."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, Optional

from .config import default_config, dump_config_yaml, load_config
from .generator import generate


def _cmd_generate(args: argparse.Namespace) -> int:
    overrides: Dict[str, Any] = {
        "start_date": args.start,
        "days": args.days,
        "calls_per_day": args.calls_per_day,
        "seed": args.seed,
        "timezone": args.timezone,
        "output": {k: v for k, v in {
            "directory": args.out,
            "partition_by_day": None if args.single_file is None else not args.single_file,
            "validate": None if args.no_validate is None else not args.no_validate,
            "compression": args.compression,
        }.items() if v is not None},
    }
    cfg = load_config(args.config, overrides)
    log = (lambda m: print(m, file=sys.stderr, flush=True)) if not args.quiet else None
    stats = generate(cfg, log)
    print(json.dumps({
        "output": cfg.output.directory,
        "days": cfg.days,
        "rows": stats["rows"],
        "interactions": stats["interactions"],
        "outcome_rows": stats["outcome_rows"],
        "elapsed_s": stats["elapsed_s"],
        "call_result_counts": dict(stats["call_result"]),
        "outcome_polarity_counts": dict(stats["outcome_polarity"]),
        "amount_totals_by_type": {k: round(v, 2) for k, v in stats["amount_by_type"].items()},
    }, indent=2))
    return 0


def _cmd_print_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config) if args.config else default_config()
    print(dump_config_yaml(cfg))
    return 0


def _cmd_dictionary(args: argparse.Namespace) -> int:
    from .dictionary import build_dictionary, dictionary_csv, dictionary_markdown
    from .refdata import build_refdata

    cfg = load_config(args.config) if args.config else default_config()
    dims = build_refdata(cfg).dimension_tables()
    rows = build_dictionary({n: t.schema for n, t in dims.items()})
    if args.table:
        rows = [r for r in rows if r["TABLE_NAME"] == args.table]
        if not rows:
            print(f"unknown table {args.table}", file=sys.stderr)
            return 1
    print(dictionary_csv(rows) if args.format == "csv" else dictionary_markdown(rows), end="")
    return 0


def _cmd_summarize(args: argparse.Namespace) -> int:
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    fact_dir = os.path.join(args.out, "interaction_resource_fact")
    files = sorted(glob.glob(os.path.join(fact_dir, "**", "*.parquet"), recursive=True))
    if not files:
        print(f"no parquet files under {fact_dir}", file=sys.stderr)
        return 1
    table = ds.dataset(files, format="parquet").to_table()
    n = table.num_rows

    def group_count(col: str) -> Dict[str, int]:
        g = table.group_by(col).aggregate([(col, "count")]).sort_by([(col + "_count", "descending")])
        return {str(k): int(v) for k, v in zip(g[col].to_pylist(), g[col + "_count"].to_pylist())}

    print(f"files: {len(files)}   rows: {n:,}   interactions: {len(pc.unique(table['ROOT_INTERACTION_ID'])):,}")
    print(f"date range: {pc.min(table['CALL_DATE']).as_py()} .. {pc.max(table['CALL_DATE']).as_py()}")
    for col in ("CALL_TYPE", "CALL_RESULT", "RESOURCE_TYPE", "RESOURCE_ROLE", "TRANSFER_TYPE", "LOB", "SITE"):
        print(f"\n{col}:")
        for k, v in group_count(col).items():
            print(f"  {k:<28} {v:>9,}  {100.0 * v / n:5.1f}%")
    inbound_q = table.filter(pc.and_(pc.equal(table["CALL_TYPE"], "Inbound"), pc.is_valid(table["VQ_NAME"])))
    print("\nVQ_NAME (inbound legs, share, abandon%, SL%):")
    g = inbound_q.group_by("VQ_NAME").aggregate([("IRF_ID", "count"), ("ABANDON_FLAG", "sum"),
                                                 ("SERVICE_LEVEL_FLAG", "mean")]).sort_by([("IRF_ID_count", "descending")])
    for name, cnt, ab_sum, sl_mean in zip(g["VQ_NAME"].to_pylist(), g["IRF_ID_count"].to_pylist(),
                                           g["ABANDON_FLAG_sum"].to_pylist(), g["SERVICE_LEVEL_FLAG_mean"].to_pylist()):
        print(f"  {name:<28} {cnt:>9,}  {100.0 * cnt / max(1, inbound_q.num_rows):5.1f}%  "
              f"ab={100.0 * (ab_sum or 0) / cnt:5.1f}%  sl={100.0 * (sl_mean or 0):5.1f}%")
    ab = pc.sum(inbound_q["ABANDON_FLAG"]).as_py() or 0
    sab = pc.sum(inbound_q["SHORT_ABANDON_FLAG"]).as_py() or 0
    sl = inbound_q.filter(pc.is_valid(inbound_q["SERVICE_LEVEL_FLAG"]))
    print(f"\ninbound queued legs: {inbound_q.num_rows:,}  abandon%={100.0 * ab / max(1, inbound_q.num_rows):.2f}  "
          f"short-abandon%={100.0 * sab / max(1, inbound_q.num_rows):.2f}  "
          f"SL%={100.0 * (pc.sum(sl['SERVICE_LEVEL_FLAG']).as_py() or 0) / max(1, sl.num_rows):.2f}")
    print("\nagent load balance by ROUTING_METHOD (answered inbound queue legs):")
    for method in ("ACD", "PBR"):
        sub = inbound_q.filter(pc.and_(pc.equal(inbound_q["ROUTING_METHOD"], method),
                                       pc.equal(inbound_q["ANSWERED_FLAG"], 1)))
        if sub.num_rows == 0:
            continue
        per_agent = sub.group_by("AGENT_ID").aggregate([("IRF_ID", "count")])["IRF_ID_count"].to_pylist()
        per_agent.sort()
        n_a = len(per_agent)
        dec = max(1, n_a // 10)
        top, bottom = sum(per_agent[-dec:]) / dec, sum(per_agent[:dec]) / dec
        mean = sum(per_agent) / n_a
        cum = 0
        half = 0
        for i, v in enumerate(per_agent):
            cum += v
            if half == 0 and cum >= sum(per_agent) / 2:
                half = n_a - i
        print(f"  {method:<4} legs={sub.num_rows:>8,} agents={n_a:>5}  calls/agent mean={mean:6.1f} "
              f"min={per_agent[0]:>4} max={per_agent[-1]:>4}  top10%/bottom10%={top / max(bottom, 0.5):5.1f}x  "
              f"agents handling half the calls={100.0 * half / n_a:4.1f}%")
    answered = table.filter(pc.equal(table["ANSWERED_FLAG"], 1))
    print(f"answered legs: {answered.num_rows:,}  mean talk={pc.mean(answered['TALK_TIME']).as_py():.0f}s  "
          f"mean hold={pc.mean(answered['HOLD_TIME']).as_py():.0f}s  mean acw={pc.mean(answered['ACW_TIME']).as_py():.0f}s  "
          f"max talk={pc.max(answered['TALK_TIME']).as_py()}s")
    per_day = table.group_by("CALL_DATE").aggregate([("IRF_ID", "count")]).sort_by("CALL_DATE")
    print("\nlegs per day:")
    for d, c in zip(per_day["CALL_DATE"].to_pylist(), per_day["IRF_ID_count"].to_pylist()):
        print(f"  {d}  {c:>8,}")
    _summarize_outcomes(args.out)
    return 0


def _summarize_outcomes(out_dir: str) -> None:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    files = sorted(glob.glob(os.path.join(out_dir, "interaction_outcome_fact", "**", "*.parquet"), recursive=True))
    if not files:
        return
    o = ds.dataset(files, format="parquet").to_table()
    n = o.num_rows
    pos = pc.sum(pc.cast(pc.equal(o["OUTCOME_POLARITY"], "Positive"), "int64")).as_py() or 0
    print(f"\n=== business outcomes: {n:,} rows on {len(pc.unique(o['IRF_ID'])):,} handled legs, "
          f"positive={100.0 * pos / max(1, n):.1f}%  cross-sells={o.filter(pc.equal(o['OUTCOME_SEQ'], 2)).num_rows:,}")

    print("\nBUSINESS_RESULT by LOB (share within LOB):")
    g = o.group_by(["LOB", "BUSINESS_RESULT"]).aggregate([("OUTCOME_ID", "count")]).sort_by(
        [("LOB", "ascending"), ("OUTCOME_ID_count", "descending")])
    totals = dict(zip(*(t.to_pylist() for t in o.group_by("LOB").aggregate([("OUTCOME_ID", "count")]).columns)))
    for lob, res, cnt in zip(g["LOB"].to_pylist(), g["BUSINESS_RESULT"].to_pylist(), g["OUTCOME_ID_count"].to_pylist()):
        print(f"  {lob:<16} {res:<18} {cnt:>8,}  {100.0 * cnt / totals[lob]:5.1f}%")

    print("\namounts by AMOUNT_TYPE:")
    a = o.filter(pc.is_valid(o["AMOUNT"])).group_by("AMOUNT_TYPE").aggregate(
        [("AMOUNT", "sum"), ("AMOUNT", "count"), ("AMOUNT", "mean")]).sort_by([("AMOUNT_sum", "descending")])
    for t, s, c, m in zip(a["AMOUNT_TYPE"].to_pylist(), a["AMOUNT_sum"].to_pylist(), a["AMOUNT_count"].to_pylist(),
                          a["AMOUNT_mean"].to_pylist()):
        print(f"  {t:<14} n={c:>7,}  total={s:>14,.2f}  mean={m:>9,.2f}")

    def positive_rate(sub) -> str:
        if sub.num_rows == 0:
            return "   n/a"
        p = pc.sum(pc.cast(pc.equal(sub["OUTCOME_POLARITY"], "Positive"), "int64")).as_py() or 0
        return f"{100.0 * p / sub.num_rows:5.1f}%"

    prim = o.filter(pc.equal(o["OUTCOME_SEQ"], 1))
    print("\npositive-outcome rate by agent PBR score quartile (primary outcomes; shows the agent effect):")
    for lo, hi in ((0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)):
        sub = prim.filter(pc.and_(pc.greater_equal(prim["AGENT_PBR_SCORE"], lo), pc.less(prim["AGENT_PBR_SCORE"], hi)))
        print(f"  score {lo:4.2f}-{min(hi, 1.0):4.2f}  n={sub.num_rows:>8,}  positive={positive_rate(sub)}")
    print("\npositive-outcome rate by ROUTING_METHOD for Sales / Retention (PBR queues route to better agents):")
    sr = prim.filter(pc.is_in(prim["LOB"], pa.array(["Sales", "Retention"])))
    for method in ("ACD", "PBR", "Direct", "Callback", "Dialer", "Manual"):
        sub = sr.filter(pc.equal(sr["ROUTING_METHOD"], method))
        if sub.num_rows:
            print(f"  {method:<9} n={sub.num_rows:>8,}  positive={positive_rate(sub)}")
    print("\npositive-outcome rate by queue wait (primary outcomes, inbound):")
    inbound = prim.filter(pc.equal(prim["CALL_TYPE"], "Inbound"))
    for lo, hi, label in ((0, 60, "<1 min"), (60, 300, "1-5 min"), (300, 900, "5-15 min"), (900, 10 ** 9, ">15 min")):
        sub = inbound.filter(pc.and_(pc.greater_equal(inbound["QUEUE_TIME"], lo), pc.less(inbound["QUEUE_TIME"], hi)))
        print(f"  wait {label:<8} n={sub.num_rows:>8,}  positive={positive_rate(sub)}")
    fu = o.filter(pc.equal(o["FOLLOW_UP_FLAG"], 1))
    print(f"\nfollow-ups / cases: {fu.num_rows:,}   in-call events: {pc.sum(o['IN_CALL_FLAG']).as_py() or 0:,}   "
          f"offers made: {pc.sum(o['OFFER_MADE_FLAG']).as_py() or 0:,}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gim_synth", description="Synthetic Genesys Info Mart voice interaction generator")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate parquet data")
    g.add_argument("--config", help="YAML file overriding the default configuration")
    g.add_argument("--start", help="first day, YYYY-MM-DD")
    g.add_argument("--days", type=int, help="number of days to generate")
    g.add_argument("--calls-per-day", type=int, dest="calls_per_day", help="baseline arrivals per weekday")
    g.add_argument("--seed", type=int)
    g.add_argument("--timezone", help="IANA zone of the contact centre business clock")
    g.add_argument("--out", help="output directory")
    g.add_argument("--compression", help="parquet compression codec (zstd, snappy, gzip, none)")
    g.add_argument("--single-file", dest="single_file", action="store_const", const=True, default=None,
                   help="write one parquet file instead of one partition per day")
    g.add_argument("--no-validate", dest="no_validate", action="store_const", const=True, default=None,
                   help="skip invariant validation")
    g.add_argument("--quiet", action="store_true")
    g.set_defaults(func=_cmd_generate)

    c = sub.add_parser("print-config", help="dump the effective configuration as YAML")
    c.add_argument("--config")
    c.set_defaults(func=_cmd_print_config)

    s = sub.add_parser("summarize", help="print quick statistics of a generated dataset")
    s.add_argument("--out", default="./out")
    s.set_defaults(func=_cmd_summarize)

    d = sub.add_parser("dictionary", help="print the data dictionary of all output tables")
    d.add_argument("--config", help="YAML file (the dictionary is schema-driven; the config only affects dimension contents)")
    d.add_argument("--table", help="restrict to one table, e.g. dim_vq")
    d.add_argument("--format", choices=["markdown", "csv"], default="markdown")
    d.set_defaults(func=_cmd_dictionary)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
