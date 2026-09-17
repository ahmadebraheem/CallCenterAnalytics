"""Scenario engine: turns an arrival into one or more INTERACTION_RESOURCE_FACT rows ("legs").

All times inside this module are float seconds relative to the local midnight of the day being
generated (they may exceed 86400 for legs that spill past midnight).  The generator converts them
to timestamps.  Temporary keys start with an underscore and are dropped by the writer.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import GeneratorConfig, OutcomeDef
from .refdata import Agent, RefData, VQ
from .schema import CALL_RESULT_CODES, new_outcome_row, new_row

DAY_S = 86400.0
TRANSFERISH = {"blind_transfer", "warm_transfer", "multi_hop", "external_transfer", "conference", "consult_only"}
PENDING_OUTCOME = "__outcome__"   # sentinel: DISPOSITION to be set from the business-outcome draw


@dataclass
class Ctx:
    """State shared by all legs of one interaction tree."""
    root_iid: int
    iid: int
    call_date: date
    cust: Optional[int]
    first_flag: int
    repeat_flag: int
    seq: int = 0
    prev_call_id: Optional[str] = None
    transfer_count: int = 0
    depth: int = 0
    forced_hops: int = 0


@dataclass
class PendingCallback:
    due_abs_s: float
    vq_idx: int
    cust: int
    root_iid: int


class _Weighted:
    __slots__ = ("items", "cum")

    def __init__(self, weights: Dict[str, float]):
        self.items = list(weights)
        total = float(sum(weights.values()))
        acc, cum = 0.0, []
        for w in weights.values():
            acc += w / total
            cum.append(acc)
        self.cum = cum

    def pick(self, r: random.Random):
        return r.choices(self.items, cum_weights=self.cum)[0]


class LegBuilder:
    def __init__(self, cfg: GeneratorConfig, ref: RefData):
        self.cfg = cfg
        self.ref = ref
        self.r = random.Random(cfg.seed * 104729 + 7)
        self.iid_counter = 1_000_000_000
        self.irf_counter = 1
        self.cust_last_seen: Dict[int, float] = {}
        self.pending_callbacks: List[PendingCallback] = []
        self.dropped_callbacks = 0

        self.outcomes = {name: _Weighted(l.outcome_weights) for name, l in ref.lobs.items()}
        self.business_outcomes: Dict[str, List[Tuple[str, OutcomeDef]]] = {
            name: list(l.business_outcomes.items()) for name, l in ref.lobs.items()}
        self.subtype_pickers: Dict[int, _Weighted] = {
            id(o): _Weighted(o.subtypes) for l in ref.lobs.values() for o in l.business_outcomes.values() if o.subtypes}
        if cfg.outcomes.cross_sell.subtypes:
            self.subtype_pickers[id(cfg.outcomes.cross_sell)] = _Weighted(cfg.outcomes.cross_sell.subtypes)
        self.outcome_rows: List[dict] = []
        self.outcome_counter = 0
        self.case_counter = 0
        self.transfer_targets = {name: _Weighted(l.transfer_targets) for name, l in ref.lobs.items()}
        self.manual_results = _Weighted(cfg.outbound.manual_results)
        self.dialer_results = _Weighted(cfg.outbound.dialer_results)
        self.campaigns = list(cfg.outbound.campaigns.items())
        self.vqs_by_lob: Dict[str, List[VQ]] = {}
        for v in ref.vqs:
            self.vqs_by_lob.setdefault(v.lob, []).append(v)

        # per-day state
        self.day_index = 0
        self.day: date = date.today()
        self.dow = 0
        self.ew: Dict[int, np.ndarray] = {}

    # ------------------------------------------------------------------ #
    # day setup
    # ------------------------------------------------------------------ #
    def set_day(self, day_index: int, day: date, ew_by_vq: Dict[int, np.ndarray]) -> None:
        self.day_index = day_index
        self.day = day
        self.dow = day.weekday()
        self.ew = ew_by_vq
        self.outcome_rows = []

    # ------------------------------------------------------------------ #
    # small helpers
    # ------------------------------------------------------------------ #
    def _new_iid(self) -> int:
        self.iid_counter += 1
        return self.iid_counter

    def _new_irf(self) -> int:
        self.irf_counter += 1
        return self.irf_counter

    def _new_call_id(self) -> str:
        return f"{self.r.getrandbits(64):016x}"

    def _minute(self, t: float) -> int:
        return int(min(max(t, 0.0), DAY_S - 1) // 60)

    def _dow_hour(self, t: float) -> Tuple[int, int]:
        offset = int(t // DAY_S)
        return (self.dow + offset) % 7, int((t % DAY_S) // 3600)

    def _abs(self, t: float) -> float:
        return self.day_index * DAY_S + t

    def _pick_agent(self, vq: VQ, t: float, exclude: Optional[int] = None, premium: bool = False) -> Agent:
        """Select an agent for the VQ at time t.

        ACD queues pick uniformly among eligible agents (longest-idle behaves ~uniformly over a day).
        PBR queues pick proportionally to exp(skew * agent quality), so high-scoring agents receive
        far more calls than low-scoring ones; VIP / Enterprise customers concentrate this further.
        """
        dow, hour = self._dow_hour(t)
        key = (vq.idx, dow, hour)
        pool = self.ref.eligible.get(key)
        cum = None
        if pool:
            if vq.cfg.pbr_enabled:
                cum = self.ref.pbr_cum.get(key)
        else:
            pool = self.ref.agents_by_vq[vq.idx]
            if vq.cfg.pbr_enabled:
                cum = self.ref.pbr_cum_all.get(vq.idx)

        def draw() -> Agent:
            if cum is None:
                return self.ref.agents[self.r.choice(pool)]
            return self.ref.agents[self.r.choices(pool, cum_weights=cum[1] if premium else cum[0])[0]]

        a = draw()
        if exclude is not None and a.idx == exclude and len(pool) > 1:
            for _ in range(5):
                a = draw()
                if a.idx != exclude:
                    break
        return a

    def _is_premium(self, ctx: Ctx) -> bool:
        return ctx.cust is not None and self.ref.customer_segment[ctx.cust] in ("VIP", "Enterprise")

    def _routing(self, row: dict, vq: VQ, agent: Optional[Agent] = None) -> None:
        if vq.cfg.pbr_enabled:
            row["ROUTING_METHOD"] = "PBR"
            if agent is not None:
                row["PBR_SCORE"] = agent.pbr_score
        else:
            row["ROUTING_METHOD"] = "ACD"

    def _pick_agent_any(self, t: float, exclude: Optional[int] = None) -> Agent:
        dow, hour = self._dow_hour(t)
        pool = self.ref.on_shift.get((dow, hour)) or [a.idx for a in self.ref.agents]
        a = self.ref.agents[self.r.choice(pool)]
        if exclude is not None and a.idx == exclude and len(pool) > 1:
            for _ in range(5):
                a = self.ref.agents[self.r.choice(pool)]
                if a.idx != exclude:
                    break
        return a

    def _touch_customer(self, cust: int, t: float) -> Tuple[int, int]:
        now = self._abs(t)
        last = self.cust_last_seen.get(cust)
        self.cust_last_seen[cust] = now
        if last is None:
            return 1, 0
        return 0, int(now - last <= 7 * DAY_S)

    def _new_ctx(self, cust: Optional[int], t: float) -> Ctx:
        iid = self._new_iid()
        first, repeat = self._touch_customer(cust, t) if cust is not None else (0, 0)
        return Ctx(root_iid=iid, iid=iid, call_date=self.day, cust=cust, first_flag=first, repeat_flag=repeat)

    # ---- distributions ---- #
    def _lognorm(self, median: float, sigma: float) -> float:
        return median * math.exp(sigma * self.r.gauss(0.0, 1.0))

    def _gamma(self, shape: float, mean: float) -> float:
        return self.r.gammavariate(shape, mean / shape) if mean > 0 else 0.0

    def _ring(self) -> float:
        return self.r.uniform(2.0, 9.0) if self.r.random() < 0.85 else self.r.uniform(9.0, 20.0)

    def _talk(self, vq: VQ, agent: Agent, scale: float = 1.0) -> float:
        return min(10800.0, max(3.0, self._lognorm(vq.cfg.talk_median_s * agent.speed * scale, vq.cfg.talk_sigma)))

    def _acw(self, vq: VQ, agent: Agent, scale: float = 1.0) -> float:
        if self.r.random() < 0.05:
            return 0.0
        return min(900.0, self._gamma(2.0, vq.cfg.acw_mean_s * agent.speed * scale))

    def _hold(self, vq: VQ) -> float:
        return max(3.0, self._gamma(1.3, vq.cfg.hold_mean_s))

    def _wait(self, vq: VQ, t: float) -> float:
        ew = float(self.ew[vq.idx][self._minute(t)])
        return self._gamma(self.cfg.queue_model.wait_gamma_shape, ew) * vq.cfg.wait_scale

    @staticmethod
    def _vq_or_global(vq: VQ, name: str, default: float) -> float:
        override = getattr(vq.cfg, name)
        return default if override is None else override

    def _patience(self, vq: VQ) -> float:
        return self._lognorm(vq.cfg.patience_median_s, vq.cfg.patience_sigma)

    # ------------------------------------------------------------------ #
    # row assembly
    # ------------------------------------------------------------------ #
    def _base(self, ctx: Ctx, arrive: float, call_type: str, resource_type: str, role: str, scenario: str,
              iid: Optional[int] = None) -> dict:
        ctx.seq += 1
        row = new_row()
        row["IRF_ID"] = self._new_irf()
        row["INTERACTION_ID"] = iid if iid is not None else ctx.iid
        row["ROOT_INTERACTION_ID"] = ctx.root_iid
        row["CALL_ID"] = self._new_call_id()
        row["CALL_DATE"] = ctx.call_date
        row["SEGMENT_SEQ"] = ctx.seq
        row["PREVIOUS_CALL_ID"] = ctx.prev_call_id
        row["CALL_TYPE"] = call_type
        row["RESOURCE_TYPE"] = resource_type
        row["RESOURCE_ROLE"] = role
        row["SCENARIO"] = scenario
        row["TRANSFER_COUNT"] = ctx.transfer_count
        row["FIRST_CALL_FLAG"] = ctx.first_flag
        row["REPEAT_CALL_7D_FLAG"] = ctx.repeat_flag
        if ctx.cust is not None:
            row["CUSTOMER_ID"] = self.ref.customer_ids[ctx.cust]
            row["CUSTOMER_SEGMENT"] = self.ref.customer_segment[ctx.cust]
            row["ANI"] = self.ref.customer_ani[ctx.cust]
        row["_arrive"] = float(round(arrive))
        return row

    def _route(self, row: dict, vq: VQ, orig_vq: Optional[VQ] = None) -> None:
        row["ROUTE_POINT"] = (orig_vq or vq).route_point
        row["DNIS"] = (orig_vq or vq).dnis
        row["QUEUE_ID"] = vq.queue_id
        row["QUEUE_NAME"] = vq.queue_name
        row["VQ_ID"] = vq.vq_id
        row["VQ_NAME"] = vq.name
        row["SKILL"] = vq.skill
        row["LOB"] = vq.lob
        row["SITE"] = vq.home_site
        if orig_vq is not None:
            row["ORIGINAL_VQ_NAME"] = orig_vq.name
            row["OVERFLOW_FLAG"] = 1

    def _agent(self, row: dict, agent: Agent) -> None:
        row["AGENT_ID"] = agent.agent_id
        row["AGENT_NAME"] = agent.name
        row["AGENT_GROUP"] = agent.group
        row["AGENT_TENURE_BAND"] = agent.tenure_band
        row["SITE"] = agent.site
        if row["LOB"] is None:
            row["LOB"] = agent.lob

    def _finish(self, row: dict, ivr: float, queue: float, ring: float, answered: bool,
                talk: float = 0.0, hold: float = 0.0, hold_count: int = 0, acw: float = 0.0) -> None:
        arrive = row["_arrive"]
        ivr_i, q_i, ring_i = int(round(ivr)), int(round(queue)), int(round(ring))
        talk_i, hold_i, acw_i = (int(round(talk)), int(round(hold)), int(round(acw))) if answered else (0, 0, 0)
        connect = arrive + ivr_i + q_i + ring_i
        end = connect + talk_i + hold_i if answered else connect
        row["IVR_TIME"], row["QUEUE_TIME"], row["RING_TIME"] = ivr_i, q_i, ring_i
        row["TALK_TIME"], row["HOLD_TIME"], row["HOLD_COUNT"], row["ACW_TIME"] = talk_i, hold_i, (hold_count if answered else 0), acw_i
        row["DURATION"] = int(round(end - arrive))
        row["HANDLE_TIME"] = talk_i + hold_i + acw_i
        row["ANSWERED_FLAG"] = int(answered)
        row["_answer"] = connect if answered else None
        row["_end"] = end
        row["_acw_end"] = end + acw_i if acw_i > 0 else None

    def _result(self, row: dict, result: str, tech: str, reason: str, disconnect: Optional[str]) -> None:
        row["CALL_RESULT"] = result
        row["CALL_RESULT_CODE"] = CALL_RESULT_CODES[result]
        row["TECHNICAL_RESULT"] = tech
        row["TECHNICAL_RESULT_REASON"] = reason
        row["DISCONNECT_REASON"] = disconnect

    def _service_level(self, row: dict, vq: VQ) -> None:
        row["SERVICE_LEVEL_FLAG"] = int(row["QUEUE_TIME"] + row["RING_TIME"] <= vq.cfg.service_level_s)

    def _transfer_in(self, row: dict, transfer_type: Optional[str]) -> None:
        if transfer_type:
            row["TRANSFER_IN_FLAG"] = 1
            row["TRANSFER_TYPE"] = transfer_type

    # ------------------------------------------------------------------ #
    # business outcomes
    # ------------------------------------------------------------------ #
    def _outcome_weights(self, defs: Sequence[Tuple[str, OutcomeDef]], agent: Agent, row: dict,
                         handling: str) -> List[float]:
        """Baseline weights bent by agent quality, customer segment and the wait the customer endured."""
        oc = self.cfg.outcomes
        seg_lift = oc.segment_lift.get(row["CUSTOMER_SEGMENT"] or "", 0.0)
        wait_pen = oc.wait_penalty_per_min * max(0.0, (row["QUEUE_TIME"] or 0) / 60.0 - 1.0)
        weights: List[float] = []
        for _, o in defs:
            if handling == "short" and (o.polarity == "Positive" or o.amount_type is not None):
                weights.append(0.0)
                continue
            logit = o.agent_lift * oc.agent_effect_scale * agent.pbr_z
            if o.polarity == "Positive":
                logit += seg_lift - wait_pen
            weights.append(o.weight * math.exp(logit))
        if sum(weights) <= 0:  # every outcome excluded (e.g. LOB with only Positive outcomes) -> baseline
            weights = [o.weight for _, o in defs]
        return weights

    def _new_outcome_row(self, row: dict, agent: Agent, name: str, o: OutcomeDef, seq: int) -> dict:
        oc = self.cfg.outcomes
        self.outcome_counter += 1
        out = new_outcome_row()
        out["OUTCOME_ID"] = self.outcome_counter
        for k in ("IRF_ID", "CALL_ID", "INTERACTION_ID", "ROOT_INTERACTION_ID", "CALL_DATE", "CALL_TYPE", "SCENARIO",
                  "LOB", "VQ_NAME", "CAMPAIGN_NAME", "ROUTING_METHOD", "AGENT_ID", "AGENT_NAME", "AGENT_GROUP",
                  "AGENT_TENURE_BAND", "SITE", "CUSTOMER_ID", "CUSTOMER_SEGMENT", "QUEUE_TIME", "TALK_TIME", "HANDLE_TIME"):
            out[k] = row[k]
        out["OUTCOME_SEQ"] = seq
        out["AGENT_PBR_SCORE"] = round(agent.pbr_score, 4)
        out["OUTCOME_CATEGORY"], out["BUSINESS_RESULT"], out["OUTCOME_POLARITY"] = o.category, name, o.polarity
        picker = self.subtype_pickers.get(id(o))
        out["OUTCOME_SUBTYPE"] = picker.pick(self.r) if picker else None
        out["OFFER_MADE_FLAG"] = int(self.r.random() < o.offer_prob)

        # when: amount-bearing events (order placed, payment taken) happen inside the conversation;
        # other outcomes are coded at wrap-up (or in the last seconds of the call when there is no ACW)
        answer, end, acw_end = row["_answer"], row["_end"], row["_acw_end"]
        recorded = end + (acw_end - end) * self.r.uniform(0.15, 0.9) if acw_end is not None else end
        in_call = o.amount_type is not None or self.r.random() < oc.in_call_event_prob
        outcome_t = answer + (end - answer) * self.r.uniform(0.5, 0.97) if in_call else recorded
        out["_outcome"], out["_recorded"], out["IN_CALL_FLAG"] = float(round(outcome_t)), float(round(recorded)), int(in_call)

        if o.amount_type is not None:
            out["AMOUNT_TYPE"] = o.amount_type
            out["AMOUNT"] = round(self._lognorm(o.amount_median, o.amount_sigma), 2)
            out["CURRENCY"] = oc.currency
        if self.r.random() < o.follow_up_prob:
            self.case_counter += 1
            out["FOLLOW_UP_FLAG"] = 1
            out["CASE_ID"] = f"CS{10_000_000 + self.case_counter}"
            lo, hi = oc.promise_days if o.amount_type == "Promise" else oc.follow_up_days
            due_day = math.floor(out["_recorded"] / DAY_S) + self.r.uniform(lo, hi)
            # due at a business hour (09:00-18:00 local) of the target day
            out["_follow_up"] = float(math.floor(due_day) * DAY_S + 9 * 3600 + self.r.uniform(0, 9 * 3600))
        return out

    def _outcome(self, row: dict, lob: str, agent: Agent, handling: str) -> None:
        """Draw the business outcome(s) of a handled customer leg; sets DISPOSITION and records outcome rows."""
        oc = self.cfg.outcomes
        defs = self.business_outcomes[lob]
        weights = self._outcome_weights(defs, agent, row, handling)
        name, o = self.r.choices(defs, weights=weights)[0]
        row["DISPOSITION"] = name
        if not oc.enabled:
            return
        primary = self._new_outcome_row(row, agent, name, o, seq=1)
        self.outcome_rows.append(primary)
        if o.polarity == "Negative" or handling == "short" or oc.cross_sell_prob <= 0:
            return
        xs = oc.cross_sell
        p = min(0.6, oc.cross_sell_prob * math.exp(xs.agent_lift * oc.agent_effect_scale * agent.pbr_z
                                                   + oc.segment_lift.get(row["CUSTOMER_SEGMENT"] or "", 0.0)))
        if self.r.random() < p:
            secondary = self._new_outcome_row(row, agent, "CrossSell", xs, seq=2)
            secondary["_recorded"] = max(secondary["_recorded"], primary["_recorded"])
            self.outcome_rows.append(secondary)

    # ------------------------------------------------------------------ #
    # INBOUND
    # ------------------------------------------------------------------ #
    def inbound(self, t: float, vq_idx: int, cust: int) -> List[dict]:
        cfg = self.cfg
        vq = self.ref.vqs[vq_idx]
        ctx = self._new_ctx(cust, t)
        ivr = max(cfg.ivr.min_s, self.r.gauss(cfg.ivr.mean_s, cfg.ivr.sd_s))

        if self.r.random() < self._vq_or_global(vq, "ivr_contained_prob", cfg.ivr.contained_prob):
            row = self._base(ctx, t, "Inbound", "IVR", "Received", "ivr_self_service")
            row["ROUTE_POINT"], row["DNIS"], row["LOB"] = vq.route_point, vq.dnis, vq.lob
            self._finish(row, ivr * self.r.uniform(1.5, 4.5), 0, 0, answered=False)
            self._result(row, "SelfService", "Completed", "IVRContained", "Customer")
            row["N_CUSTOMER"] = 1
            return [row]

        dow, hour = self._dow_hour(t)
        if not vq.is_open(dow, hour):
            voicemail = self.r.random() < cfg.ivr.after_hours_voicemail_prob
            row = self._base(ctx, t, "Inbound", "IVR", "Received", "after_hours_voicemail" if voicemail else "after_hours")
            self._route(row, vq)
            extra = self.r.uniform(25, 120) if voicemail else self.r.uniform(8, 30)
            self._finish(row, ivr + extra, 0, 0, answered=False)
            if voicemail:
                self._result(row, "Voicemail", "Diverted", "AfterHoursVoicemail", "Customer")
            else:
                self._result(row, "AfterHours", "Completed", "AfterHoursAnnouncement", "Customer")
            row["N_CUSTOMER"] = 1
            return [row]

        return self._queue_and_handle(ctx, t, ivr, vq, role="Received", transfer_type=None)

    def _abandon_leg(self, ctx: Ctx, arrive: float, ivr: float, qt: float, vq: VQ, orig_vq: Optional[VQ],
                     role: str, transfer_type: Optional[str], scenario: str) -> dict:
        short = int(round(qt)) < self.cfg.short_abandon_threshold_s
        row = self._base(ctx, arrive, "Inbound", "Queue", role, scenario)
        self._route(row, vq, orig_vq)
        self._routing(row, vq)
        self._transfer_in(row, transfer_type)
        self._finish(row, ivr, qt, 0, answered=False)
        self._result(row, "ShortAbandon" if short else "Abandoned", "Abandoned", "AbandonedWhileQueued", "Customer")
        row["ABANDON_FLAG"] = 1
        row["SHORT_ABANDON_FLAG"] = int(short)
        row["N_CUSTOMER"] = 1
        row["SERVICE_LEVEL_FLAG"] = None if short else 0
        return row

    def _queue_and_handle(self, ctx: Ctx, arrive: float, ivr: float, vq: VQ, role: str,
                          transfer_type: Optional[str]) -> List[dict]:
        ib = self.cfg.inbound
        t_q = arrive + ivr
        m = self._minute(t_q)
        ew = float(self.ew[vq.idx][m])
        orig_vq: Optional[VQ] = None
        rows: List[dict] = []
        premium = self._is_premium(ctx)

        # ---- callback offer when the queue is badly backed up ---- #
        if role == "Received" and ew >= ib.callback_offer_wait_s and self.r.random() < ib.callback_accept_prob:
            qt = self.r.uniform(15, 60)
            row = self._base(ctx, arrive, "Inbound", "Queue", role, "callback_requested")
            self._route(row, vq)
            self._routing(row, vq)
            self._finish(row, ivr, qt, 0, answered=False)
            self._result(row, "CallbackRequested", "Diverted", "CallbackAccepted", "Customer")
            row["CALLBACK_REQUESTED_FLAG"] = 1
            row["N_CUSTOMER"] = 1
            row["SERVICE_LEVEL_FLAG"] = None
            due = t_q + qt + 60 + ew * self.r.uniform(0.8, 2.2)
            self.pending_callbacks.append(PendingCallback(self._abs(due), vq.idx, ctx.cust, ctx.root_iid))
            return [row]

        wait = self._wait(vq, t_q)
        patience = self._patience(vq)
        if self.r.random() < self._vq_or_global(vq, "short_abandon_prob", ib.short_abandon_prob):
            patience = self.r.uniform(0.5, self.cfg.short_abandon_threshold_s - 0.6)

        # ---- overflow to a backup VQ after waiting too long ---- #
        if (vq.overflow_idx is not None and wait > ib.overflow_wait_s and patience > ib.overflow_wait_s
                and self.r.random() < ib.overflow_prob):
            orig_vq = vq
            vq = self.ref.vqs[vq.overflow_idx]
            wait = ib.overflow_wait_s + self._wait(vq, t_q + ib.overflow_wait_s)

        if patience < wait or int(round(patience)) < self.cfg.short_abandon_threshold_s:
            scenario = "abandon_short" if int(round(patience)) < self.cfg.short_abandon_threshold_s else "abandon_queue"
            rows.append(self._abandon_leg(ctx, arrive, ivr, patience, vq, orig_vq, role, transfer_type, scenario))
            return rows

        # ---- RONA: agent alerted but did not answer, call re-queued ---- #
        if self.r.random() < self._vq_or_global(vq, "rona_prob", ib.rona_prob):
            agent = self._pick_agent(vq, t_q + wait, premium=premium)
            row = self._base(ctx, arrive, "Inbound", "Agent", role, "rona")
            self._route(row, vq, orig_vq)
            self._agent(row, agent)
            self._routing(row, vq, agent)
            self._transfer_in(row, transfer_type)
            self._finish(row, ivr, wait, ib.rona_ring_s, answered=False)
            self._result(row, "RONA", "Redirected", "RouteOnNoAnswer", None)
            row["RONA_FLAG"], row["N_AGENT"], row["N_CUSTOMER"], row["SERVICE_LEVEL_FLAG"] = 1, 1, 1, 0
            rows.append(row)
            ctx.prev_call_id = row["CALL_ID"]
            arrive = row["_end"]
            ivr = 0.0
            t_q = arrive
            wait = self._wait(vq, t_q) * 0.5
            if self._patience(vq) < wait:
                rows.append(self._abandon_leg(ctx, arrive, 0.0, wait * self.r.uniform(0.3, 0.95), vq, orig_vq, role,
                                              transfer_type, "abandon_after_rona"))
                return rows

        # ---- customer drops while the agent's phone is ringing ---- #
        ring = self._ring()
        if self.r.random() < self._vq_or_global(vq, "abandon_while_ringing_prob", ib.abandon_while_ringing_prob):
            agent = self._pick_agent(vq, t_q + wait, premium=premium)
            row = self._base(ctx, arrive, "Inbound", "Agent", role, "abandon_ringing")
            self._route(row, vq, orig_vq)
            self._agent(row, agent)
            self._routing(row, vq, agent)
            self._transfer_in(row, transfer_type)
            self._finish(row, ivr, wait, ring * self.r.uniform(0.2, 0.9), answered=False)
            self._result(row, "Abandoned", "Abandoned", "AbandonedWhileRinging", "Customer")
            row["ABANDON_FLAG"], row["N_AGENT"], row["N_CUSTOMER"], row["SERVICE_LEVEL_FLAG"] = 1, 1, 1, 0
            rows.append(row)
            return rows

        agent = self._pick_agent(vq, t_q + wait + ring, premium=premium)
        rows.extend(self._handle(ctx, arrive, ivr, wait, ring, vq, orig_vq, agent, role, transfer_type))
        return rows

    # ------------------------------------------------------------------ #
    def _handle(self, ctx: Ctx, arrive: float, ivr: float, queue: float, ring: float, vq: VQ,
                orig_vq: Optional[VQ], agent: Agent, role: str, transfer_type: Optional[str]) -> List[dict]:
        ib = self.cfg.inbound
        outcome = self.outcomes[vq.lob].pick(self.r)
        if ctx.forced_hops > 0:
            outcome = "blind_transfer"
            ctx.forced_hops -= 1
        elif outcome == "multi_hop":
            ctx.forced_hops = self.r.randint(1, 2)
            outcome = "blind_transfer"
        if ctx.depth >= ib.max_transfer_depth and outcome in TRANSFERISH:
            outcome = "normal"
            ctx.forced_hops = 0

        row = self._base(ctx, arrive, "Inbound", "Agent", role, outcome)
        self._route(row, vq, orig_vq)
        self._agent(row, agent)
        self._routing(row, vq, agent)
        self._transfer_in(row, transfer_type)
        rows = [row]
        children: List[dict] = []
        disconnect = "Customer" if self.r.random() < 0.65 else "Agent"
        result = ("Answered", "Completed", "AnsweredByAgent")
        disposition: Optional[str] = PENDING_OUTCOME
        hold, holds, acw = 0.0, 0, self._acw(vq, agent)
        n_agent = 1

        if outcome == "normal":
            talk = self._talk(vq, agent)
            if self.r.random() < vq.cfg.hold_prob:
                hold, holds = self._hold(vq), 1
        elif outcome == "short":
            talk = self.r.uniform(4, 30)
            acw = min(acw, self.r.uniform(0, 25))
            disconnect = "Customer" if self.r.random() < 0.8 else "Agent"
        elif outcome == "long":
            talk = min(7200.0, self._lognorm(1800 * agent.speed, 0.35))
            holds = self.r.randint(1, 4)
            hold = sum(self._hold(vq) for _ in range(holds))
            acw *= 1.5
        elif outcome == "multi_hold":
            talk = self._talk(vq, agent, 1.3)
            holds = self.r.randint(2, 5)
            hold = sum(self._hold(vq) for _ in range(holds))
        elif outcome == "system_drop":
            talk = self.r.uniform(5, 120)
            result = ("SystemError", "Failed", "SystemError")
            disconnect = "System"
            acw = min(acw, self.r.uniform(0, 30))
            disposition = None
        elif outcome == "blind_transfer":
            talk = self._talk(vq, agent, 0.55)
            if self.r.random() < vq.cfg.hold_prob * 0.7:
                hold, holds = self._hold(vq), 1
            result = ("Transferred", "Transferred", "BlindTransfer")
            disconnect, disposition = "Transfer", "Transferred"
            row["TRANSFER_FLAG"], row["TRANSFER_TYPE"] = 1, "Blind"
            self._finish(row, ivr, queue, ring, True, talk, hold, holds, acw)
            children = self._transfer_target(ctx, row, vq, "Blind", agent)
        elif outcome == "warm_transfer":
            talk = self._talk(vq, agent, 0.6)
            consult_start = row["_arrive"] + ivr + queue + ring + talk * self.r.uniform(0.4, 0.9)
            target_vq = self._target_vq(vq, consult_start)
            consult_agent = self._pick_agent(target_vq, consult_start, exclude=agent.idx)
            consult_dur = self._lognorm(75, 0.5)
            hold, holds = consult_dur + (self._hold(vq) if self.r.random() < 0.3 else 0.0), 1
            result = ("Transferred", "Transferred", "WarmTransfer")
            disconnect, disposition = "Transfer", "Transferred"
            row["TRANSFER_FLAG"], row["TRANSFER_TYPE"], row["CONSULT_FLAG"] = 1, "Warm", 1
            row["TRANSFER_TO"] = consult_agent.agent_id
            self._finish(row, ivr, queue, ring, True, talk, hold, holds, acw)
            children.append(self._consult_leg(ctx, row, consult_start, consult_dur, consult_agent, target_vq, "warm_transfer_consult"))
            ctx.transfer_count += 1
            ctx.depth += 1
            ctx.prev_call_id = row["CALL_ID"]
            children.append(self._received_warm_leg(ctx, row["_end"], consult_agent, target_vq, vq))
        elif outcome == "consult_only":
            talk = self._talk(vq, agent, 1.1)
            consult_start = row["_arrive"] + ivr + queue + ring + talk * self.r.uniform(0.3, 0.8)
            target_vq = self._target_vq(vq, consult_start) if self.r.random() < 0.5 else vq
            consult_agent = self._pick_agent(target_vq, consult_start, exclude=agent.idx)
            consult_dur = self._lognorm(90, 0.5)
            hold, holds = consult_dur, 1
            row["CONSULT_FLAG"] = 1
            self._finish(row, ivr, queue, ring, True, talk, hold, holds, acw)
            children.append(self._consult_leg(ctx, row, consult_start, consult_dur, consult_agent, target_vq, "consult_only"))
        elif outcome == "conference":
            talk_before = self._talk(vq, agent, 0.6)
            consult_start = row["_arrive"] + ivr + queue + ring + talk_before
            target_vq = self._target_vq(vq, consult_start)
            conf_agent = self._pick_agent(target_vq, consult_start, exclude=agent.idx)
            consult_dur = self.r.uniform(20, 60)
            conf_dur = self._lognorm(240, 0.5)
            talk = talk_before + conf_dur
            hold, holds = consult_dur, 1
            n_agent = 2
            result = ("Conferenced", "Conferenced", "ConferenceInitiated")
            row["CONFERENCE_FLAG"], row["CONSULT_FLAG"] = 1, 1
            self._finish(row, ivr, queue, ring, True, talk, hold, holds, acw)
            children.append(self._consult_leg(ctx, row, consult_start, consult_dur, conf_agent, target_vq, "conference_consult"))
            ctx.prev_call_id = row["CALL_ID"]
            children.append(self._conference_leg(ctx, consult_start + consult_dur, conf_dur, conf_agent, target_vq, vq))
        elif outcome == "external_transfer":
            talk = self._talk(vq, agent, 0.5)
            result = ("Transferred", "Transferred", "ExternalTransfer")
            disconnect, disposition = "Transfer", "Transferred"
            ext = f"1800{self.r.randint(2000000, 9999999)}"
            row["TRANSFER_FLAG"], row["TRANSFER_TYPE"], row["TRANSFER_TO"] = 1, "External", ext
            self._finish(row, ivr, queue, ring, True, talk, hold, holds, acw)
            ctx.transfer_count += 1
            ctx.prev_call_id = row["CALL_ID"]
            children.append(self._external_leg(ctx, row["_end"], ext, vq))
        else:  # pragma: no cover - unknown outcome key in config
            talk = self._talk(vq, agent)

        if "_end" not in row:
            self._finish(row, ivr, queue, ring, True, talk, hold, holds, acw)
        self._result(row, *result, disconnect)
        row["N_AGENT"], row["N_CUSTOMER"] = n_agent, 1
        self._service_level(row, vq)
        if disposition == PENDING_OUTCOME:
            self._outcome(row, vq.lob, agent, outcome)
        else:
            row["DISPOSITION"] = disposition
        rows.extend(children)
        return rows

    # ---- transfer / consult / conference helpers ---- #
    def _target_vq(self, vq: VQ, t: Optional[float] = None) -> VQ:
        """Pick a transfer/consult target VQ; prefer one that is open at time t (a few retries), else fall back."""
        dow, hour = self._dow_hour(t) if t is not None else (self.dow, 12)
        target = vq
        for _ in range(4):
            target = self.ref.vq_by_name[self.transfer_targets[vq.lob].pick(self.r)]
            if target.is_open(dow, hour):
                return target
        if not target.is_open(dow, hour):
            open_24x7 = [v for v in self.ref.vqs if v.is_24x7]
            if open_24x7:
                return self.r.choice(open_24x7)
        return target

    def _transfer_target(self, ctx: Ctx, row: dict, vq: VQ, transfer_type: str, from_agent: Agent) -> List[dict]:
        t = row["_end"]
        ctx.transfer_count += 1
        ctx.depth += 1
        ctx.prev_call_id = row["CALL_ID"]
        if self.r.random() < 0.75:
            target = self._target_vq(vq, t)
            row["TRANSFER_TO"] = target.name
            dow, hour = self._dow_hour(t)
            if not target.is_open(dow, hour):
                # transferred into a closed queue -> after-hours announcement
                leg = self._base(ctx, t, "Inbound", "IVR", "ReceivedTransfer", "transfer_to_closed_vq")
                self._route(leg, target)
                self._routing(leg, target)
                self._transfer_in(leg, transfer_type)
                self._finish(leg, self.r.uniform(8, 30), 0, 0, answered=False)
                self._result(leg, "AfterHours", "Completed", "AfterHoursAnnouncement", "Customer")
                leg["N_CUSTOMER"] = 1
                return [leg]
            return self._queue_and_handle(ctx, t, 0.0, target, role="ReceivedTransfer", transfer_type=transfer_type)
        target_agent = self._pick_agent_any(t, exclude=from_agent.idx)
        row["TRANSFER_TO"] = target_agent.agent_id
        return [self._direct_agent_leg(ctx, t, target_agent, vq, "ReceivedTransfer", transfer_type, "blind_transfer_to_agent")]

    def _direct_agent_leg(self, ctx: Ctx, t: float, agent: Agent, lob_vq: VQ, role: str,
                          transfer_type: Optional[str], scenario: str) -> dict:
        ring = self.r.uniform(3, 18)
        row = self._base(ctx, t, "Inbound", "Agent", role, scenario)
        self._agent(row, agent)
        row["LOB"] = agent.lob
        row["DNIS"] = agent.did
        row["ROUTING_METHOD"] = "Direct"
        self._transfer_in(row, transfer_type)
        talk_vq = (self.vqs_by_lob.get(agent.lob) or [lob_vq])[0]
        if self.r.random() < 0.92:
            talk = self._talk(talk_vq, agent, 0.9)
            hold, holds = (self._hold(talk_vq), 1) if self.r.random() < 0.25 else (0.0, 0)
            self._finish(row, 0, 0, ring, True, talk, hold, holds, self._acw(talk_vq, agent))
            self._result(row, "Answered", "Completed", "AnsweredByAgent", "Customer" if self.r.random() < 0.65 else "Agent")
            self._outcome(row, agent.lob, agent, "normal")
        else:
            self._finish(row, 0, 0, ring * self.r.uniform(1.0, 2.5), False)
            self._result(row, "Abandoned", "Abandoned", "AbandonedWhileRinging", "Customer")
            row["ABANDON_FLAG"] = 1
        row["N_AGENT"], row["N_CUSTOMER"] = 1, 1
        return row

    def _consult_leg(self, ctx: Ctx, parent: dict, start: float, dur: float, agent: Agent, vq: VQ, scenario: str) -> dict:
        iid = self._new_iid()
        saved_prev = ctx.prev_call_id
        ctx.prev_call_id = parent["CALL_ID"]
        row = self._base(ctx, start, "Consult", "Agent", "ReceivedConsult", scenario, iid=iid)
        ctx.prev_call_id = saved_prev
        row["PARENT_INTERACTION_ID"] = parent["INTERACTION_ID"]
        row["PARENT_CALL_ID"] = parent["CALL_ID"]
        self._route(row, vq)
        self._agent(row, agent)
        row["ROUTING_METHOD"] = "Consult"
        row["ANI"] = parent["AGENT_ID"]
        row["DNIS"] = agent.did
        row["CUSTOMER_ID"], row["CUSTOMER_SEGMENT"] = None, None
        row["FIRST_CALL_FLAG"], row["REPEAT_CALL_7D_FLAG"] = 0, 0
        ring = self.r.uniform(2, 8)
        self._finish(row, 0, 0, ring, True, max(5.0, dur - ring), 0, 0, 0)
        self._result(row, "Consulted", "Completed", "ConsultCompleted", "Agent")
        row["CONSULT_RECEIVED_FLAG"] = 1
        row["N_AGENT"], row["N_CUSTOMER"] = 2, 0
        return row

    def _received_warm_leg(self, ctx: Ctx, t: float, agent: Agent, vq: VQ, from_vq: VQ) -> dict:
        row = self._base(ctx, t, "Inbound", "Agent", "ReceivedTransfer", "warm_transfer_received")
        self._route(row, vq)
        self._agent(row, agent)
        row["ROUTING_METHOD"] = "Direct"
        self._transfer_in(row, "Warm")
        talk = self._talk(vq, agent, 0.85)
        hold, holds = (self._hold(vq), 1) if self.r.random() < vq.cfg.hold_prob * 0.6 else (0.0, 0)
        self._finish(row, 0, 0, 0, True, talk, hold, holds, self._acw(vq, agent))
        self._result(row, "Answered", "Completed", "AnsweredByAgent", "Customer" if self.r.random() < 0.65 else "Agent")
        row["N_AGENT"], row["N_CUSTOMER"] = 1, 1
        row["SERVICE_LEVEL_FLAG"] = None
        self._outcome(row, vq.lob, agent, "normal")
        return row

    def _conference_leg(self, ctx: Ctx, t: float, dur: float, agent: Agent, vq: VQ, from_vq: VQ) -> dict:
        row = self._base(ctx, t, "Inbound", "Agent", "ConferenceJoined", "conference_joined")
        self._route(row, vq)
        self._agent(row, agent)
        row["ROUTING_METHOD"] = "Conference"
        self._finish(row, 0, 0, 0, True, dur, 0, 0, self._acw(vq, agent, 0.5))
        self._result(row, "Answered", "Conferenced", "ConferenceJoined", "Agent")
        row["CONFERENCE_FLAG"] = 1
        row["DISPOSITION"] = "Assisted"   # the business outcome is recorded by the conference initiator
        row["N_AGENT"], row["N_CUSTOMER"] = 2, 1
        return row

    def _external_leg(self, ctx: Ctx, t: float, number: str, from_vq: VQ) -> dict:
        row = self._base(ctx, t, "Inbound", "External", "ReceivedTransfer", "external_transfer_leg")
        row["LOB"] = from_vq.lob
        row["DNIS"] = number
        self._transfer_in(row, "External")
        ring = self.r.uniform(3, 20)
        if self.r.random() < 0.9:
            self._finish(row, 0, 0, ring, True, self._lognorm(240, 0.6), 0, 0, 0)
            self._result(row, "Completed", "Completed", "ExternalParty", "Customer")
        else:
            self._finish(row, 0, 0, ring * 2, False)
            self._result(row, "NoAnswer", "Failed", "ExternalNoAnswer", "Customer")
        row["N_AGENT"], row["N_CUSTOMER"] = 0, 1
        return row

    # ------------------------------------------------------------------ #
    # OUTBOUND / INTERNAL / DIRECT / CALLBACK
    # ------------------------------------------------------------------ #
    def _outbound_result(self, row: dict, result: str, ring: float, talk: float, acw: float) -> None:
        """Timing + result of an outbound attempt; the caller draws the business outcome for Answered legs."""
        if result == "Answered":
            self._finish(row, 0, 0, ring, True, talk, 0, 0, acw)
            self._result(row, "Answered", "Completed", "OutboundConnected", "Customer" if self.r.random() < 0.5 else "Agent")
            row["N_CUSTOMER"] = 1
        elif result == "AnsweringMachine":
            self._finish(row, 0, 0, ring, True, self.r.uniform(10, 45), 0, 0, min(acw, 30))
            self._result(row, "AnsweringMachine", "Completed", "AnsweringMachine", "Agent")
            row["DISPOSITION"] = "LeftMessage"
            row["N_CUSTOMER"] = 1
        elif result == "Busy":
            self._finish(row, 0, 0, self.r.uniform(2, 6), False, acw=0)
            self._result(row, "Busy", "Failed", "Busy", "System")
        else:  # NoAnswer
            self._finish(row, 0, 0, self.r.uniform(20, 45), False, acw=0)
            self._result(row, "NoAnswer", "Failed", "NoAnswer", "Agent")

    def outbound_manual(self, t: float, cust: int) -> List[dict]:
        agent = self._pick_agent_any(t)
        vq = self.ref.vqs[agent.skills[0]]
        ctx = self._new_ctx(cust, t)
        row = self._base(ctx, t, "Outbound", "Agent", "Initiated", "outbound_manual")
        self._agent(row, agent)
        row["ROUTING_METHOD"] = "Manual"
        row["LOB"] = agent.lob
        row["DNIS"] = self.ref.customer_ani[cust]
        row["ANI"] = next((s.outbound_cli for s in self.cfg.sites if s.name == agent.site), None)
        result = self.manual_results.pick(self.r)
        self._outbound_result(row, result, self.r.uniform(4, 25), self._talk(vq, agent, 0.7), self._acw(vq, agent))
        row["N_AGENT"] = 1
        if result == "Answered":
            self._outcome(row, agent.lob, agent, "normal")
        return [row]

    def outbound_dialer(self, t: float, cust: int) -> List[dict]:
        campaign, lob = self.r.choice(self.campaigns)
        vq = self.r.choice(self.vqs_by_lob[lob])
        ctx = self._new_ctx(cust, t)
        result = self.dialer_results.pick(self.r)
        if result == "DialerDrop":
            row = self._base(ctx, t, "Outbound", "Dialer", "Initiated", "dialer_drop")
            row["LOB"], row["CAMPAIGN_NAME"] = lob, campaign
            row["ROUTING_METHOD"] = "Dialer"
            row["DNIS"] = self.ref.customer_ani[cust]
            self._finish(row, 0, self.r.uniform(2, 10), self.r.uniform(4, 20), False)
            self._result(row, "DialerDrop", "Abandoned", "NoAgentAvailable", "System")
            row["ABANDON_FLAG"], row["N_AGENT"], row["N_CUSTOMER"] = 1, 0, 1
            return [row]
        agent = self._pick_agent(vq, t)
        row = self._base(ctx, t, "Outbound", "Agent", "Received", "dialer_" + result.lower())
        self._agent(row, agent)
        row["LOB"], row["CAMPAIGN_NAME"] = lob, campaign
        row["ROUTING_METHOD"] = "Dialer"
        row["DNIS"] = self.ref.customer_ani[cust]
        row["ANI"] = next((s.outbound_cli for s in self.cfg.sites if s.name == agent.site), None)
        self._outbound_result(row, result, self.r.uniform(3, 20), self._talk(vq, agent, 0.75), self._acw(vq, agent))
        row["N_AGENT"] = 1
        if result == "Answered":
            row["QUEUE_TIME"] = int(round(self.r.uniform(1, 8)))   # dialer-to-agent transfer delay
            row["DURATION"] += row["QUEUE_TIME"]
            row["_answer"] += row["QUEUE_TIME"]
            row["_end"] += row["QUEUE_TIME"]
            if row["_acw_end"] is not None:
                row["_acw_end"] += row["QUEUE_TIME"]
            self._outcome(row, lob, agent, "normal")
        return [row]

    def internal(self, t: float) -> List[dict]:
        a = self._pick_agent_any(t)
        b = self._pick_agent_any(t, exclude=a.idx)
        ctx = self._new_ctx(None, t)
        ring = self.r.uniform(2, 15)
        answered = self.r.random() < self.cfg.outbound.internal_answer_prob
        talk = self._lognorm(110, 0.7) if answered else 0.0
        row1 = self._base(ctx, t, "Internal", "Agent", "Initiated", "internal_call")
        self._agent(row1, a)
        row1["ANI"], row1["DNIS"] = a.did, b.did
        self._finish(row1, 0, 0, ring, answered, talk, 0, 0, 0)
        ctx.prev_call_id = row1["CALL_ID"]
        row2 = self._base(ctx, t, "Internal", "Agent", "Received", "internal_call")
        self._agent(row2, b)
        row2["ANI"], row2["DNIS"] = a.did, b.did
        self._finish(row2, 0, 0, ring, answered, talk, 0, 0, 0)
        for row in (row1, row2):
            row["ROUTING_METHOD"] = "Internal"
            if answered:
                self._result(row, "Answered", "Completed", "InternalCall", "Agent")
            else:
                self._result(row, "NoAnswer", "Failed", "NoAnswer", "Agent")
            row["N_AGENT"], row["N_CUSTOMER"] = 2, 0
        return [row1, row2]

    def direct_did(self, t: float, cust: int) -> List[dict]:
        agent = self._pick_agent_any(t)
        vq = self.ref.vqs[agent.skills[0]]
        ctx = self._new_ctx(cust, t)
        row = self._base(ctx, t, "Inbound", "Agent", "Received", "direct_did")
        self._agent(row, agent)
        row["LOB"], row["DNIS"] = agent.lob, agent.did
        row["ROUTING_METHOD"] = "Direct"
        ring = self.r.uniform(3, 25)
        if self.r.random() < self.cfg.outbound.did_answer_prob:
            talk = self._talk(vq, agent, 0.8)
            hold, holds = (self._hold(vq), 1) if self.r.random() < 0.2 else (0.0, 0)
            self._finish(row, 0, 0, ring, True, talk, hold, holds, self._acw(vq, agent))
            self._result(row, "Answered", "Completed", "AnsweredByAgent", "Customer" if self.r.random() < 0.65 else "Agent")
            self._outcome(row, agent.lob, agent, "normal")
        else:
            vm = self.r.uniform(0, 75)
            self._finish(row, vm, 0, 25, False)
            self._result(row, "Voicemail", "Diverted", "NoAnswerVoicemail", "Customer")
        row["N_AGENT"], row["N_CUSTOMER"] = 1, 1
        return [row]

    def callback(self, t: float, pc: PendingCallback) -> List[dict]:
        vq = self.ref.vqs[pc.vq_idx]
        agent = self._pick_agent(vq, t)
        ctx = self._new_ctx(pc.cust, t)
        row = self._base(ctx, t, "Outbound", "Agent", "Initiated", "callback_outbound")
        self._route(row, vq)
        self._agent(row, agent)
        row["RELATED_INTERACTION_ID"] = pc.root_iid
        row["CALLBACK_FLAG"] = 1
        row["ROUTING_METHOD"] = "Callback"
        row["DNIS"] = self.ref.customer_ani[pc.cust]
        row["ANI"] = vq.dnis
        u = self.r.random()
        ib = self.cfg.inbound
        result = "Answered" if u < ib.callback_connect_prob else ("NoAnswer" if u < ib.callback_connect_prob + ib.callback_no_answer_prob else "Busy")
        self._outbound_result(row, result, self.r.uniform(4, 25), self._talk(vq, agent), self._acw(vq, agent))
        row["N_AGENT"] = 1
        if result == "Answered":
            self._outcome(row, vq.lob, agent, "normal")
        return [row]

    # ------------------------------------------------------------------ #
    def pop_due_callbacks(self, day_index: int) -> List[Tuple[float, PendingCallback]]:
        """Callbacks due on `day_index`; returns (seconds since that day's midnight, callback)."""
        due, keep = [], []
        lo, hi = day_index * DAY_S, (day_index + 1) * DAY_S
        for pc in self.pending_callbacks:
            if lo <= pc.due_abs_s < hi:
                due.append((pc.due_abs_s - lo, pc))
            elif pc.due_abs_s >= hi:
                keep.append(pc)
        self.pending_callbacks = keep
        due.sort(key=lambda x: x[0])
        return due
