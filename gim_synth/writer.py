"""Convert leg / outcome rows to Arrow tables and write Parquet."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Dict, List

import pyarrow as pa
import pyarrow.parquet as pq
from zoneinfo import ZoneInfo

from .schema import COLUMNS, OUTCOME_COLUMNS, OUTCOME_SCHEMA, SCHEMA

TIME_KEYS = {"ARRIVE_TIME": "_arrive", "ANSWER_TIME": "_answer", "END_TIME": "_end", "ACW_END_TIME": "_acw_end"}
OUTCOME_TIME_KEYS = {"OUTCOME_TIME": "_outcome", "RECORDED_TIME": "_recorded", "FOLLOW_UP_DUE_TIME": "_follow_up"}

FACT_TABLE = "interaction_resource_fact"
OUTCOME_TABLE = "interaction_outcome_fact"


def _epoch_ms_local_naive(day: date) -> int:
    """Milliseconds of local midnight expressed as a naive (wall clock) timestamp."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _epoch_ms_utc_of_local_midnight(day: date, tz: ZoneInfo) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp() * 1000)


def _ts_col(rows: List[dict], key: str, base: int, typ: pa.DataType) -> pa.Array:
    return pa.array([None if r[key] is None else base + int(round(r[key] * 1000)) for r in rows], type=typ)


def rows_to_table(rows: List[dict], day: date, tz: ZoneInfo) -> pa.Table:
    """Build an Arrow table for legs generated for `day` (seconds are relative to that day's local midnight)."""
    base_local = _epoch_ms_local_naive(day)
    base_utc = _epoch_ms_utc_of_local_midnight(day, tz)
    cols: Dict[str, pa.Array] = {}
    for name, typ in COLUMNS.items():
        if name in TIME_KEYS:
            cols[name] = _ts_col(rows, TIME_KEYS[name], base_local, typ)
        elif name == "ARRIVE_TIME_UTC":
            cols[name] = _ts_col(rows, "_arrive", base_utc, typ)
        elif name == "INTERVAL_15MIN":
            cols[name] = pa.array([base_local + (int(r["_arrive"]) // 900) * 900_000 for r in rows], type=typ)
        elif name == "ARRIVE_HOUR":
            cols[name] = pa.array([int(r["_arrive"] // 3600) % 24 for r in rows], type=typ)
        else:
            cols[name] = pa.array([r[name] for r in rows], type=typ)
    return pa.table(cols, schema=SCHEMA) if rows else SCHEMA.empty_table()


def outcome_rows_to_table(rows: List[dict], day: date, tz: ZoneInfo) -> pa.Table:
    """Build an Arrow table for the business outcomes of `day` (same relative-seconds convention)."""
    base_local = _epoch_ms_local_naive(day)
    base_utc = _epoch_ms_utc_of_local_midnight(day, tz)
    cols: Dict[str, pa.Array] = {}
    for name, typ in OUTCOME_COLUMNS.items():
        if name in OUTCOME_TIME_KEYS:
            cols[name] = _ts_col(rows, OUTCOME_TIME_KEYS[name], base_local, typ)
        elif name == "OUTCOME_TIME_UTC":
            cols[name] = _ts_col(rows, "_outcome", base_utc, typ)
        else:
            cols[name] = pa.array([r[name] for r in rows], type=typ)
    return pa.table(cols, schema=OUTCOME_SCHEMA) if rows else OUTCOME_SCHEMA.empty_table()


class ParquetWriter:
    def __init__(self, out_dir: str, compression: str = "zstd", partition_by_day: bool = True):
        self.out_dir = out_dir
        self.compression = compression
        self.partition_by_day = partition_by_day
        self.fact_dir = os.path.join(out_dir, FACT_TABLE)
        os.makedirs(self.fact_dir, exist_ok=True)
        self._single_writers: Dict[str, pq.ParquetWriter] = {}
        self.files: List[str] = []

    def write_day(self, table: pa.Table, day: date, name: str = FACT_TABLE) -> str:
        table_dir = os.path.join(self.out_dir, name)
        os.makedirs(table_dir, exist_ok=True)
        if self.partition_by_day:
            part_dir = os.path.join(table_dir, f"call_date={day.isoformat()}")
            os.makedirs(part_dir, exist_ok=True)
            path = os.path.join(part_dir, "part-0.parquet")
            pq.write_table(table, path, compression=self.compression)
        else:
            path = os.path.join(table_dir, f"{name}.parquet")
            if name not in self._single_writers:
                self._single_writers[name] = pq.ParquetWriter(path, table.schema, compression=self.compression)
            self._single_writers[name].write_table(table)
        if path not in self.files:
            self.files.append(path)
        return path

    def write_dimension(self, name: str, table: pa.Table) -> str:
        path = os.path.join(self.out_dir, f"{name}.parquet")
        pq.write_table(table, path, compression=self.compression)
        return path

    def close(self) -> None:
        for w in self._single_writers.values():
            w.close()
        self._single_writers = {}
