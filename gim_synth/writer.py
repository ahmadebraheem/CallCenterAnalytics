"""Convert leg rows to Arrow tables and write Parquet."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from .schema import COLUMNS, SCHEMA

TIME_KEYS = {"ARRIVE_TIME": "_arrive", "ANSWER_TIME": "_answer", "END_TIME": "_end", "ACW_END_TIME": "_acw_end"}


def _epoch_ms_local_naive(day: date) -> int:
    """Milliseconds of local midnight expressed as a naive (wall clock) timestamp."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _epoch_ms_utc_of_local_midnight(day: date, tz: ZoneInfo) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp() * 1000)


def rows_to_table(rows: List[dict], day: date, tz: ZoneInfo) -> pa.Table:
    """Build an Arrow table for legs generated for `day` (seconds are relative to that day's local midnight)."""
    base_local = _epoch_ms_local_naive(day)
    base_utc = _epoch_ms_utc_of_local_midnight(day, tz)
    n = len(rows)
    cols: Dict[str, pa.Array] = {}
    for name, typ in COLUMNS.items():
        if name in TIME_KEYS:
            key = TIME_KEYS[name]
            vals = [None if r[key] is None else base_local + int(round(r[key] * 1000)) for r in rows]
            cols[name] = pa.array(vals, type=typ)
        elif name == "ARRIVE_TIME_UTC":
            cols[name] = pa.array([base_utc + int(round(r["_arrive"] * 1000)) for r in rows], type=typ)
        elif name == "INTERVAL_15MIN":
            cols[name] = pa.array([base_local + (int(r["_arrive"]) // 900) * 900_000 for r in rows], type=typ)
        elif name == "ARRIVE_HOUR":
            cols[name] = pa.array([int(r["_arrive"] // 3600) % 24 for r in rows], type=typ)
        else:
            cols[name] = pa.array([r[name] for r in rows], type=typ)
    return pa.table(cols, schema=SCHEMA) if n else SCHEMA.empty_table()


class ParquetWriter:
    def __init__(self, out_dir: str, compression: str = "zstd", partition_by_day: bool = True):
        self.out_dir = out_dir
        self.compression = compression
        self.partition_by_day = partition_by_day
        self.fact_dir = os.path.join(out_dir, "interaction_resource_fact")
        os.makedirs(self.fact_dir, exist_ok=True)
        self._single_writer: Optional[pq.ParquetWriter] = None
        self.files: List[str] = []

    def write_day(self, table: pa.Table, day: date) -> str:
        if self.partition_by_day:
            part_dir = os.path.join(self.fact_dir, f"call_date={day.isoformat()}")
            os.makedirs(part_dir, exist_ok=True)
            path = os.path.join(part_dir, "part-0.parquet")
            pq.write_table(table, path, compression=self.compression)
        else:
            path = os.path.join(self.fact_dir, "interaction_resource_fact.parquet")
            if self._single_writer is None:
                self._single_writer = pq.ParquetWriter(path, SCHEMA, compression=self.compression)
            self._single_writer.write_table(table)
        if path not in self.files:
            self.files.append(path)
        return path

    def write_dimension(self, name: str, table: pa.Table) -> str:
        path = os.path.join(self.out_dir, f"{name}.parquet")
        pq.write_table(table, path, compression=self.compression)
        return path

    def close(self) -> None:
        if self._single_writer is not None:
            self._single_writer.close()
            self._single_writer = None
