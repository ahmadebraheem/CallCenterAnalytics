"""Arrival process: daily volume, intraday curve, bursts ("a billion calls at once") and lulls (dead phones).

Produces a per-minute intensity for a day and the resulting arrival timestamps (seconds after
local midnight).  All randomness comes from a numpy Generator so the day profile is reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List

import numpy as np

from .config import GeneratorConfig
from .refdata import DOW_NAMES

MINUTES = 1440


@dataclass
class DayProfile:
    day: date
    dow: int
    base_count: int                      # forecast-like baseline volume before bursts
    day_multiplier: float
    day_event: str                       # normal | spike_day | quiet_day
    intensity: np.ndarray                # expected arrivals per minute (1440)
    minute_counts: np.ndarray            # realised arrivals per minute (1440)
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return int(self.minute_counts.sum())


def minute_shape(cfg: GeneratorConfig) -> np.ndarray:
    """Baseline intraday shape (sums to 1) at minute resolution."""
    m = (np.arange(MINUTES) + 0.5) / 60.0
    shape = np.full(MINUTES, cfg.intraday.night_floor)
    for h, sigma, w in cfg.intraday.peaks:
        shape += w * np.exp(-0.5 * ((m - h) / sigma) ** 2)
    return shape / shape.sum()


def build_day_profile(cfg: GeneratorConfig, day: date, rng: np.random.Generator) -> DayProfile:
    dow = day.weekday()
    events: List[Dict[str, Any]] = []

    # ---- daily volume ---- #
    mult = cfg.weekday_factors.get(DOW_NAMES[dow], 1.0) * float(np.exp(rng.normal(0, cfg.daily_noise_sigma)))
    day_event = "normal"
    u = rng.random()
    if u < cfg.day_events.spike_day_prob:
        f = float(rng.uniform(cfg.day_events.spike_min, cfg.day_events.spike_max))
        mult *= f
        day_event = "spike_day"
        events.append({"type": "spike_day", "multiplier": round(f, 2)})
    elif u < cfg.day_events.spike_day_prob + cfg.day_events.quiet_day_prob:
        f = float(rng.uniform(cfg.day_events.quiet_min, cfg.day_events.quiet_max))
        mult *= f
        day_event = "quiet_day"
        events.append({"type": "quiet_day", "multiplier": round(f, 2)})
    base_count = int(round(cfg.calls_per_day * mult))

    # ---- baseline intensity with per-minute jitter ---- #
    shape = minute_shape(cfg)
    jitter = rng.gamma(cfg.intraday.minute_jitter_shape, 1.0 / cfg.intraday.minute_jitter_shape, MINUTES)
    intensity = base_count * shape * jitter

    minutes = np.arange(MINUTES)

    # ---- bursts ---- #
    b = cfg.bursts
    n_bursts = rng.poisson(b.rate_per_day)
    for _ in range(n_bursts):
        center = rng.uniform(b.window[0] * 60, b.window[1] * 60)
        sigma = rng.uniform(b.min_sigma_min, b.max_sigma_min)
        extra = base_count * rng.uniform(b.min_frac, b.max_frac)
        bump = np.exp(-0.5 * ((minutes - center) / sigma) ** 2)
        bump *= extra / bump.sum()
        intensity += bump
        events.append({"type": "burst", "center_min": int(center), "sigma_min": round(float(sigma), 1),
                       "extra_calls": int(extra)})
    if rng.random() < b.mega_prob:
        center = rng.uniform(b.window[0] * 60, b.window[1] * 60)
        extra = base_count * rng.uniform(b.mega_min_frac, b.mega_max_frac)
        bump = np.exp(-0.5 * ((minutes - center) / b.mega_sigma_min) ** 2)
        bump *= extra / bump.sum()
        intensity += bump
        events.append({"type": "mega_burst", "center_min": int(center), "sigma_min": b.mega_sigma_min,
                       "extra_calls": int(extra)})

    # ---- lulls ---- #
    l = cfg.lulls
    n_lulls = rng.poisson(l.rate_per_day)
    for _ in range(n_lulls):
        start = int(rng.uniform(l.window[0] * 60, l.window[1] * 60))
        length = int(rng.integers(l.min_len_min, l.max_len_min + 1))
        factor = float(rng.uniform(l.min_intensity, l.max_intensity))
        end = min(MINUTES, start + length)
        intensity[start:end] *= factor
        events.append({"type": "lull", "start_min": start, "length_min": end - start, "intensity": round(factor, 3)})

    minute_counts = rng.poisson(intensity)
    return DayProfile(day=day, dow=dow, base_count=base_count, day_multiplier=round(mult, 3),
                      day_event=day_event, intensity=intensity, minute_counts=minute_counts, events=events)


def arrival_seconds(profile: DayProfile, rng: np.random.Generator) -> np.ndarray:
    """Sorted arrival offsets (seconds since local midnight) for the day."""
    counts = profile.minute_counts
    minutes = np.repeat(np.arange(MINUTES), counts)
    secs = minutes * 60.0 + rng.random(minutes.size) * 60.0
    secs.sort()
    return secs


# --------------------------------------------------------------------------- #
# Queue load model
# --------------------------------------------------------------------------- #
def expected_wait_curve(arrivals_per_min: np.ndarray, staffed_per_min: np.ndarray, aht_s: float,
                        base_asa_s: float, cfg: GeneratorConfig) -> np.ndarray:
    """Expected wait (seconds) per minute for one VQ given offered load vs staffed capacity.

    * offered rate = trailing-window mean of arrivals per minute
    * capacity     = staffed agents * 60 / AHT * target occupancy   (calls/min)
    * rho          = offered / capacity
    * E[W]         = base ASA below the knee, growing exponentially above it, capped
    * a backlog term makes the wait decay gradually after a burst instead of vanishing instantly
    """
    q = cfg.queue_model
    w = max(1, q.window_min)
    kernel = np.ones(w) / w
    offered = np.convolve(arrivals_per_min.astype(float), kernel, mode="full")[:MINUTES]
    capacity = staffed_per_min.astype(float) * 60.0 / aht_s * q.occupancy_target
    rho = np.minimum(offered / np.maximum(capacity, 1e-3), 50.0)
    growth = np.where(rho < q.load_knee, 1.0, np.exp(q.load_k * (rho - q.load_knee)))
    ew = np.minimum(base_asa_s * growth, q.max_wait_s)
    ew = np.where(offered <= 0, base_asa_s, ew)
    out = np.empty_like(ew)
    prev = base_asa_s
    for i in range(MINUTES):
        val = max(ew[i], prev * q.backlog_decay)
        out[i] = val
        prev = val
    return out
