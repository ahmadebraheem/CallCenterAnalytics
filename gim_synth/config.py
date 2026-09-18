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
class OutcomeDef:
    """One business outcome an agent can record on a handled customer call.

    The name of the outcome (the dict key in `business_outcomes`) becomes BUSINESS_RESULT in the
    outcome fact and DISPOSITION on the agent's fact row.
    """
    weight: float                       # baseline relative frequency
    polarity: str = "Neutral"           # Positive | Neutral | Negative
    category: str = "NoChange"          # Sale | Retention | Churn | Service | Escalation | Billing | Collections | FollowUp | NoChange
    # log-odds shift per unit of agent quality (the PBR z-score): 0.6 => a +1 sigma agent gets
    # e^0.6 = 1.8x the odds of this outcome, a -1 sigma agent 0.55x. Negative values for outcomes
    # that good agents avoid (Cancelled, Escalated ...).
    agent_lift: float = 0.0
    offer_prob: float = 0.0             # probability an offer was presented (OFFER_MADE_FLAG)
    follow_up_prob: float = 0.0         # probability a case / follow-up is created (CASE_ID, FOLLOW_UP_DUE_TIME)
    amount_type: Optional[str] = None   # Revenue | MRR_Retained | MRR_Lost | Payment | Credit | Promise (None = no amount)
    amount_median: float = 0.0          # log-normal median of AMOUNT
    amount_sigma: float = 0.5
    subtypes: Dict[str, float] = field(default_factory=dict)  # OUTCOME_SUBTYPE mix (product, save offer, reason ...)


@dataclass
class LOBConfig:
    name: str
    short: str
    # Business outcomes an agent can record after handling a customer call (BUSINESS_RESULT ->
    # definition). The primary outcome also becomes DISPOSITION on the fact row.
    business_outcomes: Dict[str, OutcomeDef]
    # Weights of what happens once an agent answers an inbound call (call handling path).
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
    # Per-queue overrides of the global `inbound` behaviour (None = use the global value).
    short_abandon_prob: Optional[float] = None
    abandon_while_ringing_prob: Optional[float] = None
    rona_prob: Optional[float] = None
    ivr_contained_prob: Optional[float] = None
    # Multiplier on the drawn queue wait (1.0 = as modelled); >1 makes the queue slower => more abandons.
    wait_scale: float = 1.0


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


OUTCOME_POLARITIES = ("Positive", "Neutral", "Negative")
OUTCOME_CATEGORIES = ("Sale", "Retention", "Churn", "Service", "Escalation", "Billing", "Collections", "FollowUp", "NoChange")
AMOUNT_TYPES = ("Revenue", "MRR_Retained", "MRR_Lost", "Payment", "Credit", "Promise")


def _default_cross_sell() -> OutcomeDef:
    return OutcomeDef(weight=1.0, polarity="Positive", category="Sale", agent_lift=0.5, offer_prob=1.0,
                      amount_type="Revenue", amount_median=18.0, amount_sigma=0.5,
                      subtypes={"DeviceInsurance": 0.30, "StreamingAddOn": 0.25, "DataBoost": 0.25, "Accessory": 0.20})


@dataclass
class OutcomesConfig:
    """Business outcomes recorded on handled customer calls (interaction_outcome_fact)."""
    enabled: bool = True
    currency: str = "USD"
    # Global multiplier on every OutcomeDef.agent_lift (0 = outcomes independent of the agent).
    agent_effect_scale: float = 1.0
    # Log-odds penalty on Positive outcomes per minute the customer waited beyond the first minute
    # (an angry customer after a 10-minute wait is harder to sell to / save).
    wait_penalty_per_min: float = 0.12
    # Log-odds shift on Positive outcomes by customer segment.
    segment_lift: Dict[str, float] = field(default_factory=lambda: {
        "Consumer": 0.0, "SMB": 0.10, "Enterprise": 0.25, "VIP": 0.35})
    # Calls with the `short` handling outcome (4-30 s talk) never produce Positive / amount-bearing
    # outcomes; they fall back to the remaining (Neutral / Negative) outcomes of the LOB.
    # Probability of a secondary cross-sell outcome (OUTCOME_SEQ = 2) on calls whose primary
    # outcome is not Negative; scaled by the cross_sell agent_lift like any other outcome.
    cross_sell_prob: float = 0.05
    cross_sell: OutcomeDef = field(default_factory=_default_cross_sell)
    # Share of outcomes whose OUTCOME_TIME falls inside the conversation (order submitted, payment
    # taken ...) rather than at wrap-up; always applies to amount-bearing outcomes.
    in_call_event_prob: float = 0.35
    follow_up_days: List[float] = field(default_factory=lambda: [1.0, 5.0])    # FOLLOW_UP_DUE_TIME window
    promise_days: List[float] = field(default_factory=lambda: [3.0, 14.0])    # for Promise amounts


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


def _o(weight: float, polarity: str = "Neutral", category: str = "NoChange", **kw) -> OutcomeDef:
    return OutcomeDef(weight=weight, polarity=polarity, category=category, **kw)


def _default_business_outcomes() -> Dict[str, Dict[str, OutcomeDef]]:
    return {
        "Sales": {
            "Sale": _o(0.26, "Positive", "Sale", agent_lift=0.6, offer_prob=1.0,
                       amount_type="Revenue", amount_median=65.0, amount_sigma=0.5,
                       subtypes={"Mobile_Postpaid": 0.32, "Broadband_Fibre": 0.25, "TV_Bundle": 0.14,
                                 "Mobile_Prepaid": 0.13, "Device_Financing": 0.10, "Accessory": 0.06}),
            "NoSale": _o(0.30, "Negative", "NoChange", agent_lift=-0.3, offer_prob=1.0,
                         subtypes={"Price": 0.40, "StillDeciding": 0.25, "Competitor": 0.20, "NotEligible": 0.15}),
            "CallbackScheduled": _o(0.12, "Neutral", "FollowUp", offer_prob=0.7, follow_up_prob=1.0),
            "Info": _o(0.20, "Neutral", "NoChange", offer_prob=0.3),
            "NotInterested": _o(0.12, "Negative", "NoChange", agent_lift=-0.15, offer_prob=0.5),
        },
        "Retention": {
            "Saved": _o(0.32, "Positive", "Retention", agent_lift=0.7, offer_prob=0.95,
                        amount_type="MRR_Retained", amount_median=85.0, amount_sigma=0.45,
                        subtypes={"Discount": 0.42, "PlanChange": 0.20, "FreeMonths": 0.15,
                                  "DeviceUpgrade": 0.10, "ServiceFix": 0.08, "NoOfferNeeded": 0.05}),
            "Cancelled": _o(0.20, "Negative", "Churn", agent_lift=-0.5, offer_prob=0.85,
                            amount_type="MRR_Lost", amount_median=85.0, amount_sigma=0.45,
                            subtypes={"Price": 0.38, "Competitor": 0.30, "ServiceIssues": 0.15, "Moving": 0.10, "Other": 0.07}),
            "Downgraded": _o(0.12, "Neutral", "Retention", agent_lift=0.1, offer_prob=1.0,
                             amount_type="MRR_Lost", amount_median=25.0, amount_sigma=0.5,
                             subtypes={"PlanDowngrade": 0.6, "RemovedAddOn": 0.4}),
            "OfferDeclined": _o(0.12, "Negative", "NoChange", agent_lift=-0.2, offer_prob=1.0),
            "NoChange": _o(0.14, "Neutral", "NoChange", offer_prob=0.2),
            "PendingDecision": _o(0.10, "Neutral", "FollowUp", offer_prob=0.9, follow_up_prob=1.0),
        },
        "CustomerService": {
            "Resolved": _o(0.52, "Positive", "Service", agent_lift=0.35,
                           subtypes={"AccountUpdate": 0.30, "Explained": 0.30, "ServiceChange": 0.15,
                                     "ComplaintResolved": 0.10, "Other": 0.15}),
            "FollowUp": _o(0.14, "Neutral", "FollowUp", agent_lift=-0.2, follow_up_prob=1.0),
            "Escalated": _o(0.09, "Negative", "Escalation", agent_lift=-0.3, follow_up_prob=1.0,
                            subtypes={"Supervisor": 0.5, "Complaint": 0.3, "BackOffice": 0.2}),
            "Info": _o(0.17, "Neutral", "NoChange"),
            "Unresolved": _o(0.08, "Negative", "Service", agent_lift=-0.3),
        },
        "TechSupport": {
            "Resolved": _o(0.48, "Positive", "Service", agent_lift=0.4,
                           subtypes={"Reset": 0.30, "ConfigFix": 0.30, "Educated": 0.20, "RemoteFix": 0.20}),
            "Escalated": _o(0.14, "Negative", "Escalation", agent_lift=-0.3, follow_up_prob=1.0,
                            subtypes={"Tier2Ticket": 0.6, "Engineering": 0.2, "NetworkOps": 0.2}),
            "Dispatch": _o(0.12, "Neutral", "Service", follow_up_prob=1.0,
                           subtypes={"Technician": 0.8, "Replacement": 0.2}),
            "FollowUp": _o(0.14, "Neutral", "FollowUp", follow_up_prob=1.0),
            "Info": _o(0.07, "Neutral", "NoChange"),
            "Unresolved": _o(0.05, "Negative", "Service", agent_lift=-0.3),
        },
        "Billing": {
            "PaymentTaken": _o(0.33, "Positive", "Billing", agent_lift=0.2,
                               amount_type="Payment", amount_median=120.0, amount_sigma=0.6,
                               subtypes={"Card": 0.65, "BankTransfer": 0.25, "PaymentPlan": 0.10}),
            "Adjusted": _o(0.18, "Positive", "Billing", agent_lift=0.1,
                           amount_type="Credit", amount_median=30.0, amount_sigma=0.7,
                           subtypes={"Goodwill": 0.4, "BillingError": 0.4, "ProRata": 0.2}),
            "Disputed": _o(0.12, "Negative", "Billing", agent_lift=-0.2, follow_up_prob=1.0,
                           subtypes={"Charge": 0.5, "Usage": 0.3, "Fee": 0.2}),
            "Info": _o(0.27, "Neutral", "NoChange"),
            "Escalated": _o(0.10, "Negative", "Escalation", agent_lift=-0.3, follow_up_prob=1.0),
        },
        "Collections": {
            "PromiseToPay": _o(0.33, "Positive", "Collections", agent_lift=0.45, follow_up_prob=1.0,
                               amount_type="Promise", amount_median=180.0, amount_sigma=0.6),
            "Paid": _o(0.24, "Positive", "Collections", agent_lift=0.3,
                       amount_type="Payment", amount_median=160.0, amount_sigma=0.6,
                       subtypes={"Card": 0.7, "BankTransfer": 0.3}),
            "Refused": _o(0.15, "Negative", "NoChange", agent_lift=-0.3),
            "Dispute": _o(0.13, "Negative", "Collections", follow_up_prob=1.0),
            "Hardship": _o(0.08, "Neutral", "Collections", follow_up_prob=1.0),
            "NoChange": _o(0.07, "Neutral", "NoChange"),
        },
    }


def _default_lobs() -> List[LOBConfig]:
    bo = _default_business_outcomes()
    return [
        LOBConfig(
            "Sales", "SAL",
            business_outcomes=bo["Sales"],
            outcome_weights={"normal": 0.60, "short": 0.10, "long": 0.03, "multi_hold": 0.08, "blind_transfer": 0.06,
                             "warm_transfer": 0.04, "consult_only": 0.04, "conference": 0.01, "multi_hop": 0.01,
                             "external_transfer": 0.01, "system_drop": 0.02},
            transfer_targets={"VQ_Retention_Cancel": 0.20, "VQ_CustServ_General": 0.33, "VQ_Billing_Payments": 0.25,
                              "VQ_Tech_Tier1": 0.10, "VQ_Sales_Upgrade": 0.09, "VQ_Sales_Spanish": 0.03},
        ),
        LOBConfig(
            "Retention", "RET",
            business_outcomes=bo["Retention"],
            outcome_weights={"normal": 0.48, "short": 0.04, "long": 0.08, "multi_hold": 0.14, "blind_transfer": 0.05,
                             "warm_transfer": 0.07, "consult_only": 0.08, "conference": 0.02, "multi_hop": 0.01,
                             "external_transfer": 0.01, "system_drop": 0.02},
            transfer_targets={"VQ_Sales_Upgrade": 0.25, "VQ_Billing_Disputes": 0.28, "VQ_CustServ_Account": 0.28,
                              "VQ_Tech_Tier1": 0.15, "VQ_Retention_VIP": 0.04},
        ),
        LOBConfig(
            "CustomerService", "CS",
            business_outcomes=bo["CustomerService"],
            outcome_weights={"normal": 0.55, "short": 0.08, "long": 0.03, "multi_hold": 0.08, "blind_transfer": 0.09,
                             "warm_transfer": 0.05, "consult_only": 0.05, "conference": 0.01, "multi_hop": 0.02,
                             "external_transfer": 0.02, "system_drop": 0.02},
            transfer_targets={"VQ_Billing_Payments": 0.30, "VQ_Tech_Tier1": 0.25, "VQ_Sales_New": 0.15,
                              "VQ_Retention_Cancel": 0.15, "VQ_CustServ_Account": 0.15},
        ),
        LOBConfig(
            "TechSupport", "TEC",
            business_outcomes=bo["TechSupport"],
            outcome_weights={"normal": 0.50, "short": 0.05, "long": 0.09, "multi_hold": 0.12, "blind_transfer": 0.06,
                             "warm_transfer": 0.06, "consult_only": 0.05, "conference": 0.02, "multi_hop": 0.02,
                             "external_transfer": 0.01, "system_drop": 0.02},
            transfer_targets={"VQ_Tech_Tier2": 0.50, "VQ_CustServ_General": 0.20, "VQ_Billing_Payments": 0.15,
                              "VQ_Sales_Upgrade": 0.10, "VQ_Tech_Enterprise": 0.05},
        ),
        LOBConfig(
            "Billing", "BIL",
            business_outcomes=bo["Billing"],
            outcome_weights={"normal": 0.58, "short": 0.07, "long": 0.03, "multi_hold": 0.09, "blind_transfer": 0.07,
                             "warm_transfer": 0.04, "consult_only": 0.05, "conference": 0.01, "multi_hop": 0.02,
                             "external_transfer": 0.02, "system_drop": 0.02},
            transfer_targets={"VQ_Billing_Disputes": 0.35, "VQ_CustServ_General": 0.25, "VQ_Retention_Cancel": 0.20,
                              "VQ_Collections_Inbound": 0.20},
        ),
        LOBConfig(
            "Collections", "COL",
            business_outcomes=bo["Collections"],
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
    outcomes: OutcomesConfig = field(default_factory=OutcomesConfig)
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
            if not l.business_outcomes:
                raise ValueError(f"LOB {l.name} has no business_outcomes")
            if sum(o.weight for o in l.business_outcomes.values()) <= 0:
                raise ValueError(f"LOB {l.name} business_outcomes weights must sum to a positive number")
            for name, o in l.business_outcomes.items():
                _validate_outcome_def(f"LOB {l.name} outcome {name}", o)
        _validate_outcome_def("outcomes.cross_sell", self.outcomes.cross_sell)
        if not (0 <= self.outcomes.cross_sell_prob <= 1):
            raise ValueError("outcomes.cross_sell_prob must be within [0, 1]")
        for key in ("follow_up_days", "promise_days"):
            win = getattr(self.outcomes, key)
            if len(win) != 2 or win[0] < 0 or win[1] < win[0]:
                raise ValueError(f"outcomes.{key} must be [min_days, max_days] with 0 <= min <= max")
        for camp, lob in self.outbound.campaigns.items():
            if lob not in lob_names:
                raise ValueError(f"Campaign {camp} references unknown LOB {lob}")
        if self.days < 1:
            raise ValueError("days must be >= 1")
        if self.calls_per_day < 1:
            raise ValueError("calls_per_day must be >= 1")


def _validate_outcome_def(label: str, o: OutcomeDef) -> None:
    if o.weight < 0:
        raise ValueError(f"{label}: weight must be >= 0")
    if o.polarity not in OUTCOME_POLARITIES:
        raise ValueError(f"{label}: polarity must be one of {OUTCOME_POLARITIES}")
    if o.category not in OUTCOME_CATEGORIES:
        raise ValueError(f"{label}: category must be one of {OUTCOME_CATEGORIES}")
    if o.amount_type is not None and o.amount_type not in AMOUNT_TYPES:
        raise ValueError(f"{label}: amount_type must be one of {AMOUNT_TYPES} or null")
    if o.amount_type is not None and o.amount_median <= 0:
        raise ValueError(f"{label}: amount_median must be > 0 when amount_type is set")
    for p in ("offer_prob", "follow_up_prob"):
        if not (0 <= getattr(o, p) <= 1):
            raise ValueError(f"{label}: {p} must be within [0, 1]")
    if o.subtypes and sum(o.subtypes.values()) <= 0:
        raise ValueError(f"{label}: subtypes weights must sum to a positive number")


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
        elif origin is dict and val is not None and is_dataclass(get_args(t)[1]):
            inner = get_args(t)[1]
            kwargs[f.name] = {k: _build(inner, x) for k, x in val.items()}
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
