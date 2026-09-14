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
        "elapsed_s": stats["elapsed_s"],
        "call_result_counts": dict(stats["call_result"]),
    }, indent=2))
    return 0


def _cmd_print_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config) if args.config else default_config()
    print(dump_config_yaml(cfg))
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
    answered = table.filter(pc.equal(table["ANSWERED_FLAG"], 1))
    print(f"answered legs: {answered.num_rows:,}  mean talk={pc.mean(answered['TALK_TIME']).as_py():.0f}s  "
          f"mean hold={pc.mean(answered['HOLD_TIME']).as_py():.0f}s  mean acw={pc.mean(answered['ACW_TIME']).as_py():.0f}s  "
          f"max talk={pc.max(answered['TALK_TIME']).as_py()}s")
    per_day = table.group_by("CALL_DATE").aggregate([("IRF_ID", "count")]).sort_by("CALL_DATE")
    print("\nlegs per day:")
    for d, c in zip(per_day["CALL_DATE"].to_pylist(), per_day["IRF_ID_count"].to_pylist()):
        print(f"  {d}  {c:>8,}")
    return 0


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
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
