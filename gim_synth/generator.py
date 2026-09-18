"""Day-by-day orchestration: arrivals -> VQ assignment -> queue model -> scenario legs -> parquet."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from .arrivals import MINUTES, arrival_seconds, build_day_profile, expected_wait_curve
from .config import GeneratorConfig, config_to_dict
from .dictionary import build_dictionary, dictionary_csv
from .refdata import RefData, build_refdata, vq_probabilities_by_hour
from .scenarios import LegBuilder
from .validate import ValidationError, validate_outcomes, validate_table
from .writer import OUTCOME_TABLE, ParquetWriter, outcome_rows_to_table, rows_to_table

TYPE_LABELS = ["inbound", "outbound_manual", "outbound_dialer", "internal", "direct_did"]


def _staffed_per_minute(ref: RefData, vq_idx: int, dow: int) -> np.ndarray:
    hourly = np.array([ref.staffed(vq_idx, dow, h) for h in range(24)], dtype=float)
    return np.repeat(hourly, 60)


class Generator:
    def __init__(self, cfg: GeneratorConfig, log: Optional[Callable[[str], None]] = None):
        cfg.validate()
        self.cfg = cfg
        self.log = log or (lambda msg: None)
        self.tz = ZoneInfo(cfg.timezone)
        self.ref = build_refdata(cfg)
        self.builder = LegBuilder(cfg, self.ref)
        self.nprng = np.random.default_rng(cfg.seed)
        self.start = date.fromisoformat(cfg.start_date)
        self.service_levels = {v.name: v.cfg.service_level_s for v in self.ref.vqs}
        self.stats: Dict[str, object] = {
            "rows": 0, "interactions": 0, "outcome_rows": 0, "days": [], "call_result": Counter(),
            "call_type": Counter(), "scenario": Counter(), "business_result": Counter(),
            "outcome_polarity": Counter(), "amount_by_type": Counter(), "validation_failures": [],
        }

    # ------------------------------------------------------------------ #
    def run(self) -> Dict[str, object]:
        cfg = self.cfg
        out = cfg.output
        os.makedirs(out.directory, exist_ok=True)
        writer = ParquetWriter(out.directory, out.compression, out.partition_by_day)
        t0 = time.time()
        self.log(f"roster: {len(self.ref.agents)} agents across {len(self.ref.vqs)} VQs, "
                 f"{len(self.ref.customer_ids)} customers")
        dims = self.ref.dimension_tables()
        if out.write_dimensions:
            for name, table in dims.items():
                writer.write_dimension(name, table)
        with open(os.path.join(out.directory, "_data_dictionary.csv"), "w", encoding="utf-8", newline="") as fh:
            fh.write(dictionary_csv(build_dictionary({n: t.schema for n, t in dims.items()})))
        try:
            for d in range(cfg.days):
                day = self.start + timedelta(days=d)
                rows, outcome_rows = self._generate_day(d, day)
                table = rows_to_table(rows, day, self.tz)
                outcomes = outcome_rows_to_table(outcome_rows, day, self.tz) if cfg.outcomes.enabled else None
                if out.validate:
                    failures = validate_table(table, cfg, self.service_levels)
                    if outcomes is not None:
                        failures += validate_outcomes(outcomes, table)
                    if failures:
                        self.stats["validation_failures"].append({"day": day.isoformat(), "failures": failures})
                        raise ValidationError(f"{day}: " + "; ".join(failures))
                writer.write_day(table, day)
                if outcomes is not None:
                    writer.write_day(outcomes, day, OUTCOME_TABLE)
                self._collect_stats(day, rows, outcome_rows)
                self.log(f"{day} {self.stats['days'][-1]['label']:<10} legs={len(rows):>7,} "
                         f"interactions={self.stats['days'][-1]['interactions']:>7,} "
                         f"abandon%={self.stats['days'][-1]['abandon_pct']:5.1f} "
                         f"SL%={self.stats['days'][-1]['service_level_pct']:5.1f} "
                         f"peak/min={self.stats['days'][-1]['peak_calls_per_min']:>5} "
                         f"outcomes={len(outcome_rows):>6,} "
                         f"({time.time() - t0:6.1f}s)")
        finally:
            writer.close()
        self.stats["rows"] = int(sum(d["legs"] for d in self.stats["days"]))
        self.stats["interactions"] = int(sum(d["interactions"] for d in self.stats["days"]))
        self.stats["outcome_rows"] = int(sum(d["outcomes"] for d in self.stats["days"]))
        self.stats["dropped_callbacks_after_period"] = len(self.builder.pending_callbacks)
        self.stats["elapsed_s"] = round(time.time() - t0, 1)
        self._write_manifest(writer)
        return self.stats

    # ------------------------------------------------------------------ #
    def _generate_day(self, day_index: int, day: date) -> Tuple[List[dict], List[dict]]:
        cfg, ref, b = self.cfg, self.ref, self.builder
        dow = day.weekday()
        profile = build_day_profile(cfg, day, self.nprng)
        arrivals = arrival_seconds(profile, self.nprng)
        n = arrivals.size

        # interaction type per arrival
        mix = np.array([cfg.mix.inbound, cfg.mix.outbound_manual, cfg.mix.outbound_dialer, cfg.mix.internal,
                        cfg.mix.direct_did], dtype=float)
        mix /= mix.sum()
        types = self.nprng.choice(len(TYPE_LABELS), size=n, p=mix)

        # VQ per inbound arrival, hour-dependent (closed VQs only get after-hours leakage)
        vq_p = vq_probabilities_by_hour(cfg, ref.vqs, dow)
        hours = np.minimum((arrivals // 3600).astype(int), 23)
        vq_idx = np.full(n, -1, dtype=int)
        inbound = types == 0
        for h in range(24):
            sel = np.where(inbound & (hours == h))[0]
            if sel.size:
                vq_idx[sel] = self.nprng.choice(len(ref.vqs), size=sel.size, p=vq_p[h])

        # queue model per VQ
        minutes = np.minimum((arrivals // 60).astype(int), MINUTES - 1)
        ew_by_vq: Dict[int, np.ndarray] = {}
        for v in ref.vqs:
            per_min = np.bincount(minutes[inbound & (vq_idx == v.idx)], minlength=MINUTES)
            ew_by_vq[v.idx] = expected_wait_curve(per_min, _staffed_per_minute(ref, v.idx, dow), v.aht_s,
                                                  v.cfg.base_asa_s, cfg)
        b.set_day(day_index, day, ew_by_vq)

        # customers (skewed so that some customers call repeatedly)
        pool = len(ref.customer_ids)
        custs = (pool * self.nprng.random(n) ** cfg.customers.skew_power).astype(int)
        custs = np.minimum(custs, pool - 1)

        rows: List[dict] = []
        for i in range(n):
            t = float(arrivals[i])
            k = types[i]
            if k == 0:
                rows.extend(b.inbound(t, int(vq_idx[i]), int(custs[i])))
            elif k == 1:
                rows.extend(b.outbound_manual(t, int(custs[i])))
            elif k == 2:
                rows.extend(b.outbound_dialer(t, int(custs[i])))
            elif k == 3:
                rows.extend(b.internal(t))
            else:
                rows.extend(b.direct_did(t, int(custs[i])))

        for t, pc in b.pop_due_callbacks(day_index):
            rows.extend(b.callback(t, pc))

        rows.sort(key=lambda r: (r["_arrive"], r["ROOT_INTERACTION_ID"], r["SEGMENT_SEQ"]))
        outcome_rows = b.outcome_rows
        outcome_rows.sort(key=lambda r: (r["_recorded"], r["IRF_ID"], r["OUTCOME_SEQ"]))
        self._last_profile = profile
        return rows, outcome_rows

    # ------------------------------------------------------------------ #
    def _collect_stats(self, day: date, rows: List[dict], outcome_rows: List[dict]) -> None:
        p = self._last_profile
        results = Counter(r["CALL_RESULT"] for r in rows)
        types = Counter(r["CALL_TYPE"] for r in rows)
        scen = Counter(r["SCENARIO"] for r in rows)
        self.stats["call_result"].update(results)
        self.stats["call_type"].update(types)
        self.stats["scenario"].update(scen)
        self.stats["business_result"].update(f"{o['LOB']}:{o['BUSINESS_RESULT']}" for o in outcome_rows)
        self.stats["outcome_polarity"].update(o["OUTCOME_POLARITY"] for o in outcome_rows)
        for o in outcome_rows:
            if o["AMOUNT"] is not None:
                self.stats["amount_by_type"][o["AMOUNT_TYPE"]] += o["AMOUNT"]
        positive = sum(1 for o in outcome_rows if o["OUTCOME_POLARITY"] == "Positive")
        queued = [r for r in rows if r["RESOURCE_TYPE"] in ("Queue", "Agent") and r["CALL_TYPE"] == "Inbound"
                  and r["VQ_NAME"] is not None and r["RESOURCE_ROLE"] in ("Received", "ReceivedTransfer")]
        offered = len(queued)
        abandoned = sum(1 for r in queued if r["ABANDON_FLAG"] == 1 and r["SHORT_ABANDON_FLAG"] == 0)
        sl_rows = [r for r in queued if r["SERVICE_LEVEL_FLAG"] is not None]
        sl_met = sum(1 for r in sl_rows if r["SERVICE_LEVEL_FLAG"] == 1)
        roots = len({r["ROOT_INTERACTION_ID"] for r in rows})
        self.stats["days"].append({
            "date": day.isoformat(),
            "label": p.day_event,
            "base_volume": p.base_count,
            "arrivals": p.total,
            "legs": len(rows),
            "interactions": roots,
            "abandon_pct": round(100.0 * abandoned / offered, 2) if offered else 0.0,
            "service_level_pct": round(100.0 * sl_met / len(sl_rows), 2) if sl_rows else 0.0,
            "peak_calls_per_min": int(p.minute_counts.max()),
            "min_calls_per_min_daytime": int(p.minute_counts[8 * 60:20 * 60].min()),
            "outcomes": len(outcome_rows),
            "positive_outcome_pct": round(100.0 * positive / len(outcome_rows), 2) if outcome_rows else 0.0,
            "events": p.events,
        })

    def _write_manifest(self, writer: ParquetWriter) -> None:
        manifest = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": config_to_dict(self.cfg),
            "roster_size": len(self.ref.agents),
            "vq_count": len(self.ref.vqs),
            "rows": self.stats["rows"],
            "interactions": self.stats["interactions"],
            "outcome_rows": self.stats["outcome_rows"],
            "elapsed_s": self.stats["elapsed_s"],
            "dropped_callbacks_after_period": self.stats["dropped_callbacks_after_period"],
            "call_result_counts": dict(self.stats["call_result"]),
            "call_type_counts": dict(self.stats["call_type"]),
            "scenario_counts": dict(self.stats["scenario"]),
            "business_result_counts": dict(self.stats["business_result"]),
            "outcome_polarity_counts": dict(self.stats["outcome_polarity"]),
            "amount_totals_by_type": {k: round(v, 2) for k, v in self.stats["amount_by_type"].items()},
            "days": self.stats["days"],
            "files": writer.files,
        }
        with open(os.path.join(self.cfg.output.directory, "_manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, default=str)


def generate(cfg: GeneratorConfig, log: Optional[Callable[[str], None]] = None) -> Dict[str, object]:
    return Generator(cfg, log).run()
