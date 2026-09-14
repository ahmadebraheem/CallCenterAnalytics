"""Reference (dimension) data: sites, LOBs, queues/VQs, agent roster with shifts, customers."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pyarrow as pa

from .config import GeneratorConfig, LOBConfig, VQConfig

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

FIRST_NAMES = [
    "Aaliyah", "Aaron", "Adriana", "Amir", "Ana", "Andre", "Ava", "Ben", "Bianca", "Carlos", "Chloe", "Daniel",
    "Deepa", "Diego", "Elena", "Ethan", "Fatima", "Gabriel", "Grace", "Hannah", "Hiro", "Isabella", "Jacob", "Jasmine",
    "Javier", "Jin", "Jose", "Kavya", "Kenji", "Leah", "Liam", "Lucas", "Maria", "Marcus", "Maya", "Mia", "Miguel",
    "Naomi", "Nathan", "Nina", "Noah", "Olivia", "Omar", "Priya", "Rafael", "Rohan", "Sara", "Sofia", "Tariq", "Zoe",
]
LAST_NAMES = [
    "Adams", "Ali", "Alvarez", "Bautista", "Brown", "Castillo", "Chen", "Cruz", "Davis", "Dela Cruz", "Garcia",
    "Gomez", "Gupta", "Hernandez", "Ibrahim", "Jackson", "Johnson", "Khan", "Kim", "Lee", "Lopez", "Martin",
    "Mendoza", "Miller", "Moore", "Nguyen", "Okafor", "Patel", "Perez", "Ramos", "Reyes", "Rivera", "Robinson",
    "Rodriguez", "Santos", "Sharma", "Singh", "Smith", "Taylor", "Thomas", "Torres", "Tran", "Villanueva", "Walker",
    "White", "Williams", "Wilson", "Wong", "Yamamoto", "Young",
]
TENURE_BANDS = ["<3m", "3-12m", "1-3y", "3y+"]
TENURE_WEIGHTS = [0.12, 0.28, 0.38, 0.22]


def _open_days(spec: str) -> Set[int]:
    return {"Mon-Sun": set(range(7)), "Mon-Sat": set(range(6)), "Mon-Fri": set(range(5))}[spec]


@dataclass
class VQ:
    idx: int
    cfg: VQConfig
    name: str
    vq_id: int
    lob: str
    skill: str
    queue_id: str
    queue_name: str
    route_point: str
    dnis: str
    open_hour: int
    close_hour: int
    open_days: Set[int]
    home_site: str
    aht_s: float
    overflow_idx: Optional[int] = None

    def is_open(self, dow: int, hour: int) -> bool:
        return dow in self.open_days and self.open_hour <= hour < self.close_hour

    @property
    def is_24x7(self) -> bool:
        return self.open_hour == 0 and self.close_hour == 24 and len(self.open_days) == 7


@dataclass
class Agent:
    idx: int
    agent_id: str
    name: str
    site: str
    group: str
    lob: str
    tenure_band: str
    skills: List[int]              # VQ indexes
    shift_start_h: int
    shift_len_h: float
    days_off: Set[int]
    speed: float                   # multiplier on talk / ACW (1.0 = average)
    did: str
    pbr_z: float = 0.0             # latent PBR quality (standard normal); drives routing weight
    pbr_score: float = 0.5         # percentile rank of pbr_z across the roster (0..1), reported on legs

    def on_shift(self, dow: int, hour: int) -> bool:
        """True if the agent's shift (possibly wrapping past midnight) covers (dow, hour)."""
        end = self.shift_start_h + self.shift_len_h
        if dow not in self.days_off and self.shift_start_h <= hour < end:
            return True
        # wrapped part of a shift that started the previous day
        prev = (dow - 1) % 7
        if end > 24 and prev not in self.days_off and hour < end - 24:
            return True
        return False


@dataclass
class RefData:
    cfg: GeneratorConfig
    sites: List[str]
    lobs: Dict[str, LOBConfig]
    vqs: List[VQ]
    vq_by_name: Dict[str, VQ]
    agents: List[Agent]
    # (vq_idx, dow, hour) -> list of agent idx eligible to take a call
    eligible: Dict[Tuple[int, int, int], List[int]]
    # (dow, hour) -> list of agent idx on shift (any skill)
    on_shift: Dict[Tuple[int, int], List[int]]
    agents_by_vq: Dict[int, List[int]]
    customer_ids: List[str]
    customer_ani: List[str]
    customer_segment: List[str]
    # PBR queues only: cumulative selection weights aligned with `eligible` / `agents_by_vq` pools.
    # key -> (standard cum weights, premium-customer cum weights)
    pbr_cum: Dict[Tuple[int, int, int], Tuple[List[float], List[float]]] = field(default_factory=dict)
    pbr_cum_all: Dict[int, Tuple[List[float], List[float]]] = field(default_factory=dict)
    intraday_shape_hourly: np.ndarray = field(default_factory=lambda: np.ones(24) / 24)

    # ------------------------------------------------------------------ #
    def staffed(self, vq_idx: int, dow: int, hour: int) -> int:
        return len(self.eligible.get((vq_idx, dow, hour), ()))

    def dimension_tables(self) -> Dict[str, pa.Table]:
        dim_site = pa.table({
            "SITE": [s.name for s in self.cfg.sites],
            "COUNTRY": [s.country for s in self.cfg.sites],
            "OFFSHORE_FLAG": pa.array([int(s.offshore) for s in self.cfg.sites], pa.int8()),
            "OUTBOUND_CLI": [s.outbound_cli for s in self.cfg.sites],
        })
        dim_lob = pa.table({
            "LOB": [l.name for l in self.cfg.lobs],
            "LOB_SHORT": [l.short for l in self.cfg.lobs],
        })
        dim_vq = pa.table({
            "VQ_ID": pa.array([v.vq_id for v in self.vqs], pa.int32()),
            "VQ_NAME": [v.name for v in self.vqs],
            "QUEUE_ID": [v.queue_id for v in self.vqs],
            "QUEUE_NAME": [v.queue_name for v in self.vqs],
            "ROUTE_POINT": [v.route_point for v in self.vqs],
            "DNIS": [v.dnis for v in self.vqs],
            "LOB": [v.lob for v in self.vqs],
            "SKILL": [v.skill for v in self.vqs],
            "HOME_SITE": [v.home_site for v in self.vqs],
            "OPEN_HOUR": pa.array([v.open_hour for v in self.vqs], pa.int8()),
            "CLOSE_HOUR": pa.array([v.close_hour for v in self.vqs], pa.int8()),
            "OPEN_DAYS": [v.cfg.days for v in self.vqs],
            "SERVICE_LEVEL_S": pa.array([v.cfg.service_level_s for v in self.vqs], pa.int16()),
            "OVERFLOW_VQ_NAME": [v.cfg.overflow_vq for v in self.vqs],
            "ROUTING_METHOD": ["PBR" if v.cfg.pbr_enabled else "ACD" for v in self.vqs],
            "PBR_ENABLED_FLAG": pa.array([int(v.cfg.pbr_enabled) for v in self.vqs], pa.int8()),
            "PBR_SKEW": pa.array([v.cfg.pbr_skew if v.cfg.pbr_enabled else None for v in self.vqs], pa.float32()),
            "ROSTER_SIZE": pa.array([len(self.agents_by_vq.get(v.idx, [])) for v in self.vqs], pa.int32()),
            "EXPECTED_AHT_S": pa.array([round(v.aht_s, 1) for v in self.vqs], pa.float32()),
        })
        dim_agent = pa.table({
            "AGENT_ID": [a.agent_id for a in self.agents],
            "AGENT_NAME": [a.name for a in self.agents],
            "AGENT_GROUP": [a.group for a in self.agents],
            "SITE": [a.site for a in self.agents],
            "LOB": [a.lob for a in self.agents],
            "AGENT_TENURE_BAND": [a.tenure_band for a in self.agents],
            "PRIMARY_VQ_NAME": [self.vqs[a.skills[0]].name for a in self.agents],
            "SKILLS": ["|".join(self.vqs[s].skill for s in a.skills) for a in self.agents],
            "SHIFT_START_HOUR": pa.array([a.shift_start_h for a in self.agents], pa.int8()),
            "SHIFT_LEN_H": pa.array([a.shift_len_h for a in self.agents], pa.float32()),
            "DAYS_OFF": ["|".join(DOW_NAMES[d] for d in sorted(a.days_off)) for a in self.agents],
            "SPEED_FACTOR": pa.array([round(a.speed, 3) for a in self.agents], pa.float32()),
            "PBR_SCORE": pa.array([round(a.pbr_score, 4) for a in self.agents], pa.float32()),
            "AGENT_DID": [a.did for a in self.agents],
        })
        dim_customer = pa.table({
            "CUSTOMER_ID": self.customer_ids,
            "ANI": self.customer_ani,
            "CUSTOMER_SEGMENT": self.customer_segment,
        })
        return {"dim_site": dim_site, "dim_lob": dim_lob, "dim_vq": dim_vq,
                "dim_agent": dim_agent, "dim_customer": dim_customer}


# --------------------------------------------------------------------------- #
def _hourly_shape(cfg: GeneratorConfig) -> np.ndarray:
    hours = np.arange(24) + 0.5
    shape = np.full(24, cfg.intraday.night_floor)
    for h, sigma, w in cfg.intraday.peaks:
        shape += w * np.exp(-0.5 * ((hours - h) / sigma) ** 2)
    return shape / shape.sum()


def _expected_aht(v: VQConfig) -> float:
    talk_mean = v.talk_median_s * math.exp(v.talk_sigma ** 2 / 2)
    return talk_mean + v.hold_prob * v.hold_mean_s + v.acw_mean_s


def vq_probabilities_by_hour(cfg: GeneratorConfig, vqs: Sequence[VQ], dow: int) -> np.ndarray:
    """(24, n_vq) VQ selection probabilities per hour; closed VQs keep only the after-hours leakage weight."""
    out = np.zeros((24, len(vqs)))
    for h in range(24):
        for v in vqs:
            out[h, v.idx] = v.cfg.weight * (1.0 if v.is_open(dow, h) else cfg.inbound.after_hours_leak)
        out[h] /= out[h].sum()
    return out


def expected_hourly_calls(cfg: GeneratorConfig, vqs: Sequence[VQ], hourly_shape: np.ndarray) -> np.ndarray:
    """(24, n_vq) expected inbound arrivals per hour on a typical open day (used for roster sizing)."""
    p = vq_probabilities_by_hour(cfg, vqs, dow=1)
    inbound_per_day = cfg.calls_per_day * cfg.mix.inbound
    return inbound_per_day * hourly_shape[:, None] * p


def build_refdata(cfg: GeneratorConfig) -> RefData:
    rng = random.Random(cfg.seed * 7919 + 13)
    nprng = np.random.default_rng(cfg.seed + 101)
    sites = [s.name for s in cfg.sites]
    site_weights = [s.weight for s in cfg.sites]
    offshore_sites = [s.name for s in cfg.sites if s.offshore] or sites
    lobs = {l.name: l for l in cfg.lobs}
    hourly_shape = _hourly_shape(cfg)

    # ---------------- VQs ---------------- #
    vqs: List[VQ] = []
    lob_index = {name: i + 1 for i, name in enumerate(lobs)}
    for i, v in enumerate(cfg.vqs):
        qname = "Q_" + v.name.replace("VQ_", "").upper()
        vqs.append(VQ(
            idx=i, cfg=v, name=v.name, vq_id=3000 + i + 1, lob=v.lob, skill=v.skill,
            queue_id=str(9000 + i + 1), queue_name=qname, route_point=f"RP_{8000 + i + 1}",
            dnis=f"1800555{lob_index[v.lob]:02d}{i + 1:02d}",
            open_hour=v.open_hour, close_hour=v.close_hour, open_days=_open_days(v.days),
            home_site=v.home_site or sites[0], aht_s=_expected_aht(v),
        ))
    vq_by_name = {v.name: v for v in vqs}
    for v in vqs:
        if v.cfg.overflow_vq:
            v.overflow_idx = vq_by_name[v.cfg.overflow_vq].idx

    # ---------------- Agents ---------------- #
    agents: List[Agent] = []
    agents_by_vq: Dict[int, List[int]] = {v.idx: [] for v in vqs}
    group_counter: Dict[Tuple[str, str], int] = {}
    st = cfg.staffing
    used_names: Set[str] = set()

    hourly_calls = expected_hourly_calls(cfg, vqs, hourly_shape)   # (24, n_vq)

    def shift_start_weights(v: VQ) -> Tuple[List[int], List[float]]:
        starts, weights = [], []
        for s in range(24):
            hours = [(s + k) % 24 for k in range(int(math.ceil(st.shift_len_h)))]
            covered = [h for h in hours if v.open_hour <= h < v.close_hour]
            if not covered:
                continue
            w = sum(hourly_calls[h, v.idx] for h in covered)
            # a shift that mostly spills outside opening hours is unattractive
            w *= len(covered) / len(hours)
            starts.append(s)
            weights.append(w)
        return starts, weights

    def make_agent(v: VQ, start: int) -> Agent:
        idx = len(agents)
        night = start >= 21 or start < 5
        site = rng.choice(offshore_sites) if (night and rng.random() < 0.8) else rng.choices(sites, site_weights)[0]
        key = (v.lob, site)
        group_counter[key] = group_counter.get(key, 0) + 1
        team_no = (group_counter[key] - 1) // 15 + 1
        group = f"{lobs[v.lob].short}_{site.upper()}_TEAM{team_no:02d}"
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used_names or len(used_names) > len(FIRST_NAMES) * len(LAST_NAMES) * 0.8:
                used_names.add(name)
                break
        skills = [v.idx]
        if rng.random() < st.secondary_skill_prob:
            same_lob = [o.idx for o in vqs if o.lob == v.lob and o.idx != v.idx]
            if same_lob:
                skills.append(rng.choice(same_lob))
        # days off: weekends more likely; Mon-Fri VQs always off at weekends
        if len(v.open_days) == 5:
            days_off = {5, 6}
        elif rng.random() < 0.42:
            days_off = {5, 6}
        else:
            d1 = rng.randrange(7)
            d2 = (d1 + rng.choice([1, 2, 3])) % 7
            days_off = {d1, d2}
        tenure = rng.choices(TENURE_BANDS, TENURE_WEIGHTS)[0]
        speed = math.exp(rng.gauss(0, 0.15)) * {"<3m": 1.18, "3-12m": 1.06, "1-3y": 0.98, "3y+": 0.92}[tenure]
        # PBR quality correlates with tenure but keeps plenty of individual spread
        pbr_z = rng.gauss({"<3m": -0.6, "3-12m": -0.2, "1-3y": 0.2, "3y+": 0.5}[tenure], 0.9)
        a = Agent(
            idx=idx, agent_id=f"AG{100000 + idx:06d}", name=name, site=site, group=group, lob=v.lob,
            tenure_band=tenure, skills=skills, shift_start_h=start, shift_len_h=st.shift_len_h,
            days_off=days_off, speed=speed, did=f"1214555{idx:04d}", pbr_z=pbr_z,
        )
        agents.append(a)
        for s in skills:
            agents_by_vq[s].append(idx)
        return a

    for v in vqs:
        open_hours = [h for h in range(24) if v.open_hour <= h < v.close_hour]
        daily_calls = float(hourly_calls[open_hours, v.idx].sum()) * 1.12  # + transfer inflow
        agent_hours = daily_calls * v.aht_s / 3600.0 / st.occupancy
        roster = int(math.ceil(agent_hours / (st.shift_len_h - 1.0) * 7 / 5 * st.roster_factor))
        roster = max(roster, st.min_agents_per_open_hour * 3)
        starts, weights = shift_start_weights(v)
        for _ in range(roster):
            make_agent(v, rng.choices(starts, weights)[0])

    # ---------------- Eligibility index ---------------- #
    def build_indexes():
        eligible: Dict[Tuple[int, int, int], List[int]] = {}
        on_shift: Dict[Tuple[int, int], List[int]] = {}
        for a in agents:
            for dow in range(7):
                for hour in range(24):
                    if a.on_shift(dow, hour):
                        on_shift.setdefault((dow, hour), []).append(a.idx)
                        for s in a.skills:
                            eligible.setdefault((s, dow, hour), []).append(a.idx)
        return eligible, on_shift

    eligible, on_shift = build_indexes()
    # top up thin open hours so that every open (dow, hour) has at least N agents
    for v in vqs:
        for dow in range(7):
            for hour in range(24):
                if not v.is_open(dow, hour):
                    continue
                while len(eligible.get((v.idx, dow, hour), [])) < st.min_agents_per_open_hour:
                    # coverage agents work every open day so a thin queue needs only a handful of them
                    a = make_agent(v, hour if hour + st.shift_len_h <= v.close_hour or v.is_24x7
                                   else max(v.open_hour, int(v.close_hour - st.shift_len_h)))
                    a.days_off = set(range(7)) - v.open_days
                    eligible, on_shift = build_indexes()

    # ---------------- PBR scores and weighted pools ---------------- #
    order = sorted(range(len(agents)), key=lambda i: agents[i].pbr_z)
    for rank, i in enumerate(order):
        agents[i].pbr_score = (rank + 0.5) / len(agents)

    def cum_weights(pool: Sequence[int], v: VQ) -> Tuple[List[float], List[float]]:
        std, prem, acc_s, acc_p = [], [], 0.0, 0.0
        for i in pool:
            w = math.exp(v.cfg.pbr_skew * agents[i].pbr_z)
            acc_s += w
            acc_p += w ** v.cfg.pbr_premium_boost
            std.append(acc_s)
            prem.append(acc_p)
        return std, prem

    pbr_cum: Dict[Tuple[int, int, int], Tuple[List[float], List[float]]] = {}
    pbr_cum_all: Dict[int, Tuple[List[float], List[float]]] = {}
    for v in vqs:
        if not v.cfg.pbr_enabled:
            continue
        pbr_cum_all[v.idx] = cum_weights(agents_by_vq[v.idx], v)
        for dow in range(7):
            for hour in range(24):
                pool = eligible.get((v.idx, dow, hour))
                if pool:
                    pbr_cum[(v.idx, dow, hour)] = cum_weights(pool, v)

    # ---------------- Customers ---------------- #
    n = cfg.customers.pool_size
    seg_names = list(cfg.customers.segments)
    seg_p = np.array(list(cfg.customers.segments.values()), dtype=float)
    seg_p /= seg_p.sum()
    seg = nprng.choice(seg_names, size=n, p=seg_p)
    area_codes = nprng.choice([212, 214, 305, 312, 404, 415, 469, 512, 602, 617, 702, 713, 720, 818, 917], size=n)
    lines = nprng.integers(1000000, 9999999, size=n)
    customer_ids = [f"C{10000000 + i:08d}" for i in range(n)]
    customer_ani = [f"1{a}{l:07d}" for a, l in zip(area_codes.tolist(), lines.tolist())]

    return RefData(
        cfg=cfg, sites=sites, lobs=lobs, vqs=vqs, vq_by_name=vq_by_name, agents=agents,
        eligible=eligible, on_shift=on_shift, agents_by_vq=agents_by_vq,
        pbr_cum=pbr_cum, pbr_cum_all=pbr_cum_all,
        customer_ids=customer_ids, customer_ani=customer_ani, customer_segment=seg.tolist(),
        intraday_shape_hourly=hourly_shape,
    )
