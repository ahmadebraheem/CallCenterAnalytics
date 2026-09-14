"""Configuration model for the synthetic Genesys Info Mart generator.

Everything that shapes the data (volumes, arrival curve, bursts, VQs, LOB
behaviour, agent roster sizing, output) lives here as dataclasses with sane
defaults.  A YAML file can override any subset of fields; lists (sites, lobs,
vqs) are replaced wholesale when supplied.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, get_args, get_origin, get_type_hints

import yaml


# --------------------------------------------------------------------------- #
# Reference entities
# --------------------------------------------------------------------------- #
@dataclass
class SiteConfig:
    name: str
    country: str
    weight: float
    offshore: bool = False
    outbound_cli: str = "18005550000"


@dataclass
class LOBConfig:
    name: str
    short: str
    dispositions: Dict[str, float]
    # Weights of what happens once an agent answers an inbound call.
    outcome_weights: Dict[str, float]
    # Where blind / warm transfers from this LOB go (VQ name -> weight).
    transfer_targets: Dict[str, float]


@dataclass
class VQConfig:
    name: str
    lob: str
    skill: str
    weight: float                     # share of inbound arrivals
    open_hour: int = 8                # local business time
    close_hour: int = 20              # 24 = midnight; 0/24 => 24x7
    days: str = "Mon-Sun"             # Mon-Sun | Mon-Sat | Mon-Fri
    talk_median_s: float = 300.0
    talk_sigma: float = 0.6
    hold_prob: float = 0.30
    hold_mean_s: float = 60.0
    acw_mean_s: float = 45.0
    base_asa_s: float = 15.0          # ASA when the queue is lightly loaded
    patience_median_s: float = 120.0  # customer patience (log-normal median)
    patience_sigma: float = 1.0
    service_level_s: int = 20
    overflow_vq: Optional[str] = None
    home_site: str = ""
    # Predictive Behavioural Routing: when enabled, agents are chosen by their PBR score instead of
    # longest-idle ACD, so call distribution across agents becomes lopsided.
    pbr_enabled: bool = False
    pbr_skew: float = 0.8             # log-normal sigma of agent weights; 0 = uniform, 1.2 = extreme
    pbr_premium_boost: float = 1.6    # exponent applied to weights for VIP / Enterprise customers


# --------------------------------------------------------------------------- #
# Behavioural knobs
# --------------------------------------------------------------------------- #
@dataclass
class IntradayConfig:
    # Mixture of gaussian peaks over the 24h clock: (hour, sigma_hours, weight)
    peaks: List[List[float]] = field(default_factory=lambda: [[10.5, 1.8, 0.55], [14.5, 2.2, 0.45]])
    night_floor: float = 0.04         # relative intensity floor for 24x7 traffic
    minute_jitter_shape: float = 25.0 # gamma shape of per-minute noise (higher = smoother)


@dataclass
class BurstConfig:
    """Sudden floods of calls ("a billion calls at once")."""
    rate_per_day: float = 1.0         # Poisson mean of bursts per day
    min_frac: float = 0.03            # extra calls as a fraction of the day's base volume
    max_frac: float = 0.25
    min_sigma_min: float = 3.0        # burst width (gaussian sigma, minutes)
    max_sigma_min: float = 25.0
    window: List[int] = field(default_factory=lambda: [7, 22])  # hours in which bursts can start
    mega_prob: float = 0.05           # chance per day of a flash-crowd event
    mega_min_frac: float = 0.5        # flash crowd size as a fraction of daily base
    mega_max_frac: float = 1.5
    mega_sigma_min: float = 4.0


@dataclass
class LullConfig:
    """Periods where the phones go dead (carrier outage, IVR down, ...)."""
    rate_per_day: float = 1.0
    min_len_min: int = 10
    max_len_min: int = 60
    min_intensity: float = 0.0        # multiplier applied to baseline during the lull
    max_intensity: float = 0.10
    window: List[int] = field(default_factory=lambda: [6, 23])


@dataclass
class DayEventConfig:
    spike_day_prob: float = 0.05      # e.g. outage / bill run / marketing day
    spike_min: float = 1.5
    spike_max: float = 3.0
    quiet_day_prob: float = 0.04      # e.g. public holiday
    quiet_min: float = 0.15
    quiet_max: float = 0.5


@dataclass
class InteractionMixConfig:
    inbound: float = 0.86
    outbound_manual: float = 0.04
    outbound_dialer: float = 0.04
    internal: float = 0.03
    direct_did: float = 0.03


@dataclass
class IvrConfig:
    mean_s: float = 22.0
    sd_s: float = 9.0
    min_s: float = 3.0
    contained_prob: float = 0.08      # resolved in self-service, never queued
    after_hours_voicemail_prob: float = 0.35


@dataclass
class InboundBehaviourConfig:
    short_abandon_prob: float = 0.015  # misdials / instant hang-ups regardless of queue state
    abandon_while_ringing_prob: float = 0.006
    rona_prob: float = 0.02
    rona_ring_s: float = 25.0
    overflow_wait_s: float = 120.0    # waited this long before overflow is considered
    overflow_prob: float = 0.6
    callback_offer_wait_s: float = 240.0  # expected wait at which callback is offered
    callback_accept_prob: float = 0.35
    callback_connect_prob: float = 0.75
    callback_no_answer_prob: float = 0.20
    max_transfer_depth: int = 3
    after_hours_leak: float = 0.04    # weight given to closed VQs (calls that hit the after-hours announcement)


@dataclass
class QueueModelConfig:
    occupancy_target: float = 0.85
    window_min: int = 12              # trailing minutes of arrivals used to compute load
    load_knee: float = 0.6            # rho below which wait == base ASA
    load_k: float = 4.0               # exponential growth above the knee
    max_wait_s: float = 1800.0
    backlog_decay: float = 0.88       # per-minute decay of the expected wait after a burst
    wait_gamma_shape: float = 1.5


@dataclass
class StaffingConfig:
    occupancy: float = 0.78
    roster_factor: float = 1.05       # covers days-off, shift spreading, shrinkage
    shift_len_h: float = 8.5
    secondary_skill_prob: float = 0.35
    min_agents_per_open_hour: int = 2


@dataclass
class OutboundConfig:
    manual_results: Dict[str, float] = field(default_factory=lambda: {
        "Answered": 0.55, "NoAnswer": 0.30, "Busy": 0.05, "AnsweringMachine": 0.10})
    dialer_results: Dict[str, float] = field(default_factory=lambda: {
        "Answered": 0.30, "NoAnswer": 0.35, "Busy": 0.07, "AnsweringMachine": 0.25, "DialerDrop": 0.03})
    campaigns: Dict[str, str] = field(default_factory=lambda: {
        "CAMP_Collections_PastDue": "Collections",
        "CAMP_Sales_Winback": "Sales",
        "CAMP_Retention_RenewalReminder": "Retention",
        "CAMP_Billing_PaymentReminder": "Billing"})
    internal_answer_prob: float = 0.85
    did_answer_prob: float = 0.72


@dataclass
class CustomerConfig:
    pool_size: int = 400_000
    skew_power: float = 2.5           # higher => more repeat callers
    segments: Dict[str, float] = field(default_factory=lambda: {
        "Consumer": 0.72, "SMB": 0.15, "Enterprise": 0.05, "VIP": 0.08})


@dataclass
class OutputConfig:
    directory: str = "./out"
    compression: str = "zstd"
    partition_by_day: bool = True
    write_dimensions: bool = True
    validate: bool = True


# --------------------------------------------------------------------------- #
# Root config
# --------------------------------------------------------------------------- #
def _default_sites() -> List[SiteConfig]:
    return [
        SiteConfig("Dallas", "US", 0.38, False, "18005550100"),
        SiteConfig("Phoenix", "US", 0.22, False, "18005550200"),
        SiteConfig("Toronto", "CA", 0.15, False, "18005550300"),
        SiteConfig("Manila", "PH", 0.25, True, "18005550400"),
    ]


def _default_lobs() -> List[LOBConfig]:
    return [
        LOBConfig(
            "Sales", "SAL",
            dispositions={"Sale": 0.28, "NoSale": 0.30, "CallbackScheduled": 0.12, "Info": 0.20, "NotInterested": 0.10},
            outcome_weights={"normal": 0.60, "short": 0.10, "long": 0.03, "multi_hold": 0.08, "blind_transfer": 0.06,
                             "warm_transfer": 0.04, "consult_only": 0.04, "conference": 0.01, "multi_hop": 0.01,
                             "external_transfer": 0.01, "system_drop": 0.02},
            transfer_targets={"VQ_Retention_Cancel": 0.20, "VQ_CustServ_General": 0.33, "VQ_Billing_Payments": 0.25,
                              "VQ_Tech_Tier1": 0.10, "VQ_Sales_Upgrade": 0.09, "VQ_Sales_Spanish": 0.03},
        ),
        LOBConfig(
            "Retention", "RET",
            dispositions={"Saved": 0.42, "Cancelled": 0.23, "Downgraded": 0.12, "OfferDeclined": 0.13, "Info": 0.10},
            outcome_weights={"normal": 0.48, "short": 0.04, "long": 0.08, "multi_hold": 0.14, "blind_transfer": 0.05,
                             "warm_transfer": 0.07, "consult_only": 0.08, "conference": 0.02, "multi_hop": 0.01,
                             "external_transfer": 0.01, "system_drop": 0.02},
            transfer_targets={"VQ_Sales_Upgrade": 0.25, "VQ_Billing_Disputes": 0.28, "VQ_CustServ_Account": 0.28,
                              "VQ_Tech_Tier1": 0.15, "VQ_Retention_VIP": 0.04},
        ),
        LOBConfig(
            "CustomerService", "CS",
            dispositions={"Resolved": 0.55, "Escalated": 0.10, "FollowUp": 0.15, "Info": 0.20},
            outcome_weights={"normal": 0.55, "short": 0.08, "long": 0.03, "multi_hold": 0.08, "blind_transfer": 0.09,
                             "warm_transfer": 0.05, "consult_only": 0.05, "conference": 0.01, "multi_hop": 0.02,
                             "external_transfer": 0.02, "system_drop": 0.02},
            transfer_targets={"VQ_Billing_Payments": 0.30, "VQ_Tech_Tier1": 0.25, "VQ_Sales_New": 0.15,
                              "VQ_Retention_Cancel": 0.15, "VQ_CustServ_Account": 0.15},
        ),
        LOBConfig(
            "TechSupport", "TEC",
            dispositions={"Resolved": 0.50, "Escalated": 0.15, "Dispatch": 0.12, "FollowUp": 0.15, "Info": 0.08},
            outcome_weights={"normal": 0.50, "short": 0.05, "long": 0.09, "multi_hold": 0.12, "blind_transfer": 0.06,
                             "warm_transfer": 0.06, "consult_only": 0.05, "conference": 0.02, "multi_hop": 0.02,
                             "external_transfer": 0.01, "system_drop": 0.02},
            transfer_targets={"VQ_Tech_Tier2": 0.50, "VQ_CustServ_General": 0.20, "VQ_Billing_Payments": 0.15,
                              "VQ_Sales_Upgrade": 0.10, "VQ_Tech_Enterprise": 0.05},
        ),
        LOBConfig(
            "Billing", "BIL",
            dispositions={"PaymentTaken": 0.35, "Adjusted": 0.20, "Disputed": 0.12, "Info": 0.25, "Escalated": 0.08},
            outcome_weights={"normal": 0.58, "short": 0.07, "long": 0.03, "multi_hold": 0.09, "blind_transfer": 0.07,
                             "warm_transfer": 0.04, "consult_only": 0.05, "conference": 0.01, "multi_hop": 0.02,
                             "external_transfer": 0.02, "system_drop": 0.02},
            transfer_targets={"VQ_Billing_Disputes": 0.35, "VQ_CustServ_General": 0.25, "VQ_Retention_Cancel": 0.20,
                              "VQ_Collections_Inbound": 0.20},
        ),
        LOBConfig(
            "Collections", "COL",
            dispositions={"PromiseToPay": 0.35, "Paid": 0.25, "Refused": 0.15, "NoContact": 0.10, "Dispute": 0.15},
            outcome_weights={"normal": 0.60, "short": 0.10, "long": 0.02, "multi_hold": 0.06, "blind_transfer": 0.05,
                             "warm_transfer": 0.04, "consult_only": 0.05, "conference": 0.01, "multi_hop": 0.01,
                             "external_transfer": 0.04, "system_drop": 0.02},
            transfer_targets={"VQ_Billing_Payments": 0.50, "VQ_Billing_Disputes": 0.30, "VQ_CustServ_General": 0.20},
        ),
    ]


def _default_vqs() -> List[VQConfig]:
    """Deliberately unbalanced: two or three monster queues carry most of the traffic, a handful of
    mid-size queues follow, and a long tail of niche queues sees only a few dozen calls a day."""
    return [
        # ---- monster queues (~60 % of inbound volume) ----
        VQConfig("VQ_CustServ_General", "CustomerService", "SK_CS_General", 0.270, 0, 24, "Mon-Sun", 270, 0.6, 0.30, 55, 35, 7, 120, 1.0, 20, "VQ_Overflow_Service", "Manila"),
        VQConfig("VQ_Sales_New", "Sales", "SK_Sales_New", 0.190, 8, 21, "Mon-Sun", 330, 0.55, 0.25, 45, 40, 6, 110, 1.0, 20, "VQ_Overflow_Sales", "Dallas", pbr_enabled=True, pbr_skew=0.8, pbr_premium_boost=1.6),
        VQConfig("VQ_Tech_Tier1", "TechSupport", "SK_Tech_T1", 0.150, 0, 24, "Mon-Sun", 420, 0.65, 0.45, 95, 50, 10, 150, 1.0, 30, "VQ_Overflow_Service", "Manila"),
        # ---- mid-size queues ----
        VQConfig("VQ_Billing_Payments", "Billing", "SK_Billing", 0.100, 7, 22, "Mon-Sun", 240, 0.55, 0.25, 45, 30, 6, 120, 1.0, 20, "VQ_CustServ_General", "Phoenix"),
        VQConfig("VQ_Retention_Cancel", "Retention", "SK_Retention", 0.090, 8, 22, "Mon-Sun", 520, 0.55, 0.45, 90, 70, 12, 180, 0.9, 30, "VQ_Retention_Save_Offers", "Dallas", pbr_enabled=True, pbr_skew=1.0, pbr_premium_boost=1.8),
        VQConfig("VQ_Sales_Upgrade", "Sales", "SK_Sales_Upgrade", 0.050, 8, 21, "Mon-Sun", 280, 0.55, 0.30, 50, 40, 7, 120, 1.0, 20, "VQ_Overflow_Sales", "Phoenix", pbr_enabled=True, pbr_skew=0.7, pbr_premium_boost=1.6),
        VQConfig("VQ_CustServ_Account", "CustomerService", "SK_CS_Account", 0.045, 7, 23, "Mon-Sun", 310, 0.6, 0.35, 60, 40, 7, 130, 1.0, 20, "VQ_CustServ_General", "Phoenix"),
        VQConfig("VQ_Retention_Save_Offers", "Retention", "SK_Retention_Offers", 0.030, 8, 22, "Mon-Sun", 460, 0.5, 0.40, 80, 60, 10, 160, 0.9, 30, None, "Toronto", pbr_enabled=True, pbr_skew=0.9, pbr_premium_boost=1.8),
        # ---- small queues ----
        VQConfig("VQ_Tech_Tier2", "TechSupport", "SK_Tech_T2", 0.020, 7, 23, "Mon-Sun", 780, 0.6, 0.55, 140, 90, 22, 240, 0.8, 60, None, "Dallas"),
        VQConfig("VQ_Collections_Inbound", "Collections", "SK_Collections", 0.015, 8, 21, "Mon-Sat", 300, 0.6, 0.30, 60, 45, 10, 140, 1.0, 30, "VQ_Billing_Payments", "Dallas"),
        VQConfig("VQ_Billing_Disputes", "Billing", "SK_Billing_Disputes", 0.012, 8, 20, "Mon-Fri", 480, 0.55, 0.45, 100, 75, 14, 200, 0.9, 30, None, "Dallas"),
        VQConfig("VQ_Retention_Loyalty", "Retention", "SK_Loyalty", 0.008, 9, 20, "Mon-Sat", 400, 0.5, 0.35, 70, 55, 9, 150, 0.9, 30, "VQ_Retention_Save_Offers", "Toronto"),
        # ---- long tail: niche queues with a few dozen calls a day ----
        VQConfig("VQ_Sales_Spanish", "Sales", "SK_Sales_Spanish", 0.006, 9, 20, "Mon-Sat", 360, 0.55, 0.25, 45, 40, 12, 110, 1.0, 20, "VQ_Sales_New", "Phoenix", pbr_enabled=True, pbr_skew=0.5, pbr_premium_boost=1.5),
        VQConfig("VQ_Tech_Enterprise", "TechSupport", "SK_Tech_Enterprise", 0.004, 7, 22, "Mon-Fri", 900, 0.6, 0.55, 150, 120, 20, 300, 0.8, 60, None, "Dallas"),
        VQConfig("VQ_Retention_VIP", "Retention", "SK_VIP", 0.003, 8, 22, "Mon-Sun", 600, 0.5, 0.40, 80, 90, 5, 240, 0.9, 15, "VQ_Retention_Cancel", "Toronto", pbr_enabled=True, pbr_skew=0.6, pbr_premium_boost=2.0),
        VQConfig("VQ_Overflow_Sales", "Sales", "SK_Sales_New", 0.003, 8, 21, "Mon-Sun", 330, 0.55, 0.25, 45, 40, 15, 110, 1.0, 20, None, "Manila"),
        VQConfig("VQ_CustServ_Accessibility", "CustomerService", "SK_CS_Accessibility", 0.002, 8, 20, "Mon-Fri", 520, 0.5, 0.30, 60, 60, 8, 200, 0.9, 30, "VQ_CustServ_General", "Toronto"),
        VQConfig("VQ_Overflow_Service", "CustomerService", "SK_CS_General", 0.002, 0, 24, "Mon-Sun", 290, 0.6, 0.30, 55, 35, 15, 120, 1.0, 20, None, "Manila"),
    ]


@dataclass
class GeneratorConfig:
    start_date: str = "2026-08-01"
    days: int = 31
    timezone: str = "America/New_York"
    seed: int = 42
    calls_per_day: int = 30_000
    weekday_factors: Dict[str, float] = field(default_factory=lambda: {
        "Mon": 1.18, "Tue": 1.08, "Wed": 1.02, "Thu": 1.00, "Fri": 0.97, "Sat": 0.52, "Sun": 0.38})
    daily_noise_sigma: float = 0.08
    short_abandon_threshold_s: int = 5

    intraday: IntradayConfig = field(default_factory=IntradayConfig)
    bursts: BurstConfig = field(default_factory=BurstConfig)
    lulls: LullConfig = field(default_factory=LullConfig)
    day_events: DayEventConfig = field(default_factory=DayEventConfig)
    mix: InteractionMixConfig = field(default_factory=InteractionMixConfig)
    ivr: IvrConfig = field(default_factory=IvrConfig)
    inbound: InboundBehaviourConfig = field(default_factory=InboundBehaviourConfig)
    queue_model: QueueModelConfig = field(default_factory=QueueModelConfig)
    staffing: StaffingConfig = field(default_factory=StaffingConfig)
    outbound: OutboundConfig = field(default_factory=OutboundConfig)
    customers: CustomerConfig = field(default_factory=CustomerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    sites: List[SiteConfig] = field(default_factory=_default_sites)
    lobs: List[LOBConfig] = field(default_factory=_default_lobs)
    vqs: List[VQConfig] = field(default_factory=_default_vqs)
    # Optional shortcut: override just the volume share of some VQs (name -> weight) without
    # re-declaring the whole `vqs` list.  Weights are relative; they need not sum to 1.
    vq_weights: Dict[str, float] = field(default_factory=dict)
    # Optional shortcut: override any VQConfig fields of some VQs, e.g.
    #   vq_overrides: {VQ_Tech_Tier1: {pbr_enabled: true, pbr_skew: 1.0}, VQ_Sales_New: {close_hour: 23}}
    vq_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ----------------------------------------------------------------- #
    def apply_vq_weights(self) -> None:
        by_name = {v.name: v for v in self.vqs}
        vq_fields = {f.name for f in fields(VQConfig)}
        for name, w in self.vq_weights.items():
            if name not in by_name:
                raise ValueError(f"vq_weights references unknown VQ {name}")
            if w < 0:
                raise ValueError(f"vq_weights[{name}] must be >= 0")
            by_name[name].weight = float(w)
        for name, overrides in self.vq_overrides.items():
            if name not in by_name:
                raise ValueError(f"vq_overrides references unknown VQ {name}")
            for key, val in (overrides or {}).items():
                if key not in vq_fields or key == "name":
                    raise ValueError(f"vq_overrides[{name}]: unknown VQ field {key}")
                setattr(by_name[name], key, val)

    def validate(self) -> None:
        self.apply_vq_weights()
        if sum(v.weight for v in self.vqs) <= 0:
            raise ValueError("VQ weights must sum to a positive number")
        lob_names = {l.name for l in self.lobs}
        vq_names = {v.name for v in self.vqs}
        site_names = {s.name for s in self.sites}
        for v in self.vqs:
            if v.lob not in lob_names:
                raise ValueError(f"VQ {v.name} references unknown LOB {v.lob}")
            if v.overflow_vq and v.overflow_vq not in vq_names:
                raise ValueError(f"VQ {v.name} overflow target {v.overflow_vq} does not exist")
            if v.home_site and v.home_site not in site_names:
                raise ValueError(f"VQ {v.name} home_site {v.home_site} does not exist")
            if not (0 <= v.open_hour < 24 and 0 < v.close_hour <= 24 and v.open_hour < v.close_hour):
                raise ValueError(f"VQ {v.name} has invalid hours {v.open_hour}-{v.close_hour}")
        for l in self.lobs:
            for t in l.transfer_targets:
                if t not in vq_names:
                    raise ValueError(f"LOB {l.name} transfer target {t} does not exist")
        for camp, lob in self.outbound.campaigns.items():
            if lob not in lob_names:
                raise ValueError(f"Campaign {camp} references unknown LOB {lob}")
        if self.days < 1:
            raise ValueError("days must be >= 1")
        if self.calls_per_day < 1:
            raise ValueError("calls_per_day must be >= 1")


# --------------------------------------------------------------------------- #
# Loading / dumping
# --------------------------------------------------------------------------- #
def _build(cls, data):
    """Recursively build a dataclass from a plain dict (lists of dataclasses supported)."""
    if data is None:
        return None
    if not is_dataclass(cls):
        return data
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        t = hints[f.name]
        origin = get_origin(t)
        if origin is list and val is not None:
            (inner,) = get_args(t)
            kwargs[f.name] = [_build(inner, x) if is_dataclass(inner) else x for x in val]
        elif origin is not None and type(None) in get_args(t):
            inner = [a for a in get_args(t) if a is not type(None)][0]
            kwargs[f.name] = _build(inner, val)
        else:
            kwargs[f.name] = _build(t, val) if is_dataclass(t) else val
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**kwargs)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_config() -> GeneratorConfig:
    return GeneratorConfig()


def config_to_dict(cfg: GeneratorConfig) -> Dict[str, Any]:
    return dataclasses.asdict(cfg)


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> GeneratorConfig:
    """Load defaults, apply a YAML file (optional) and then CLI overrides (optional)."""
    data = config_to_dict(default_config())
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            file_data = yaml.safe_load(fh) or {}
        data = _deep_merge(data, file_data)
    if overrides:
        data = _deep_merge(data, {k: v for k, v in overrides.items() if v is not None})
    cfg = _build(GeneratorConfig, data)
    cfg.validate()
    return cfg


def dump_config_yaml(cfg: GeneratorConfig) -> str:
    return yaml.safe_dump(config_to_dict(cfg), sort_keys=False, default_flow_style=False)
