"""Queue catalogue resolution: decide which LOBs and virtual queues exist.

Works on the plain config dict before the dataclasses are built, so YAML authors can leave fields
out or write `auto` and have them filled from per-LOB templates:

  catalogue.mode: default  built-in 6 LOB / 18 VQ catalogue (`lobs:` / `vqs:` as they are)
  catalogue.mode: manual   user lists `vqs:` (name + lob is enough); `lobs:` optional (derived)
  catalogue.mode: auto     user gives n_vqs / lobs / shares; names, weights, hours, AHT, PBR,
                           overflow, sites and transfer maps are designed here

Every LOB known to the library (the six built-in ones) brings a VQ template (the largest built-in
queue of that LOB), an outcome catalogue and a handling mix; unknown LOB names borrow the
CustomerService behaviour so any catalogue validates and generates.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import fields
from typing import Any, Dict, List, Optional, Sequence

from .config import LOBConfig, VQConfig, _default_lobs, _default_vqs

AUTO = "auto"

# Naming / behaviour library per known LOB. `share` is the LOB's default inbound volume share,
# `suffixes` the queue names used in order, `pbr` whether the LOB is a natural PBR candidate,
# `may_24x7` whether its leading queues may be open around the clock.
LOB_LIBRARY: Dict[str, Dict[str, Any]] = {
    "CustomerService": dict(abbrev="CustServ", share=0.32, pbr=False, may_24x7=True,
                            suffixes=["General", "Account", "Orders", "Moves", "Complaints", "Web", "Loyalty", "Accessibility"]),
    "Sales": dict(abbrev="Sales", share=0.25, pbr=True, may_24x7=False,
                  suffixes=["New", "Upgrade", "Business", "Devices", "Broadband", "Web", "Winback", "Spanish"]),
    "TechSupport": dict(abbrev="Tech", share=0.18, pbr=False, may_24x7=True,
                        suffixes=["Tier1", "Tier2", "Mobile", "Broadband", "TV", "Enterprise", "Field", "Spanish"]),
    "Billing": dict(abbrev="Billing", share=0.12, pbr=False, may_24x7=False,
                    suffixes=["Payments", "Disputes", "Adjustments", "Business", "Refunds", "Spanish"]),
    "Retention": dict(abbrev="Retention", share=0.10, pbr=True, may_24x7=False,
                      suffixes=["Cancel", "Save_Offers", "Loyalty", "Business", "VIP", "Winback"]),
    "Collections": dict(abbrev="Collections", share=0.03, pbr=False, may_24x7=False,
                        suffixes=["Inbound", "PastDue", "Hardship", "Business", "Legal"]),
}
GENERIC_LOB = "CustomerService"
GENERIC_TEMPLATE = "_generic"
GENERIC_SUFFIXES = ["General", "Priority", "Tier2", "Business", "Spanish", "VIP", "Web", "Overflow"]

# VQ fields copied from the LOB template when missing / `auto`.
TEMPLATE_FIELDS = ("talk_median_s", "talk_sigma", "hold_prob", "hold_mean_s", "acw_mean_s", "base_asa_s",
                   "patience_median_s", "patience_sigma", "service_level_s", "pbr_skew", "pbr_premium_boost",
                   "open_hour", "close_hour", "days")
# Optional per-queue behaviour overrides: `auto` / missing simply means "use the global value" (null).
NULLABLE_FIELDS = ("short_abandon_prob", "abandon_while_ringing_prob", "rona_prob", "ivr_contained_prob")


def _is_auto(v: Any) -> bool:
    """Missing / null / the word `auto` all mean "fill it in for me"."""
    return v is None or _is_auto_word(v)


def _is_auto_word(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower() == AUTO


def _zipf(n: int, skew: float) -> List[float]:
    w = [1.0 / (i + 1) ** skew for i in range(n)]
    s = sum(w)
    return [x / s for x in w]


def _abbrev(lob: str) -> str:
    return LOB_LIBRARY.get(lob, {}).get("abbrev", lob)


def _short(lob: str, shorts: Dict[str, str]) -> str:
    """Declared short code, else initials of a CamelCase name (HelpDesk -> HD), else first 3 letters."""
    if lob in shorts:
        return shorts[lob]
    caps = "".join(c for c in lob if c.isupper())
    return (caps if len(caps) >= 2 else lob[:3]).upper()


def _declared_shorts(lobs: Any) -> Dict[str, str]:
    shorts = {l.name: l.short for l in _default_lobs()}
    for l in lobs or []:
        if isinstance(l, dict) and not _is_auto(l.get("name")) and not _is_auto(l.get("short")):
            shorts[l["name"]] = l["short"]
    return shorts


def _vq_templates() -> Dict[str, VQConfig]:
    """Per LOB: the biggest built-in VQ of that LOB (its AHT / patience / SL profile). The entry under
    GENERIC_TEMPLATE is used for LOBs the library does not know: CustomerService profile, business hours."""
    best: Dict[str, VQConfig] = {}
    for v in _default_vqs():
        if v.lob not in best or v.weight > best[v.lob].weight:
            best[v.lob] = v
    best[GENERIC_TEMPLATE] = dataclasses.replace(best[GENERIC_LOB], open_hour=8, close_hour=20, days="Mon-Sun")
    return best


def _lob_dicts_from_defaults() -> Dict[str, Dict[str, Any]]:
    return {l.name: dataclasses.asdict(l) for l in _default_lobs()}


# --------------------------------------------------------------------------- #
# Filling partial definitions (manual mode, and the skeletons produced by auto mode)
# --------------------------------------------------------------------------- #
def _fill_vqs(vqs: List[Dict[str, Any]], sites: Sequence[Dict[str, Any]], manual: Dict[str, Any],
              shorts: Dict[str, str]) -> List[Dict[str, Any]]:
    templates = _vq_templates()
    generic = templates[GENERIC_TEMPLATE]
    vq_fields = {f.name for f in fields(VQConfig)}
    site_names = [s["name"] for s in sorted(sites, key=lambda s: -s["weight"])] or [""]
    offshore = [s["name"] for s in sites if s.get("offshore")]
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(vqs):
        if not isinstance(raw, dict) or _is_auto(raw.get("name")):
            raise ValueError(f"vqs[{i}]: every VQ needs at least a `name` and a `lob`")
        if _is_auto(raw.get("lob")):
            raise ValueError(f"VQ {raw['name']}: `lob` is required")
        unknown = set(raw) - vq_fields
        if unknown:
            raise ValueError(f"VQ {raw['name']}: unknown fields {sorted(unknown)}")
        v = dict(raw)
        tpl = templates.get(v["lob"], generic)
        for f in TEMPLATE_FIELDS:
            if _is_auto(v.get(f)):
                v[f] = getattr(tpl, f)
        for f in NULLABLE_FIELDS:
            if _is_auto(v.get(f)):
                v[f] = None
        if _is_auto(v.get("wait_scale")):
            v["wait_scale"] = 1.0
        if _is_auto(v.get("pbr_enabled")):
            v["pbr_enabled"] = bool(LOB_LIBRARY.get(v["lob"], {}).get("pbr", False))
        if _is_auto(v.get("skill")):
            suffix = v["name"].split("_", 2)[-1] if "_" in v["name"] else v["name"]
            v["skill"] = f"SK_{_short(v['lob'], shorts)}_{suffix}"
        if _is_auto(v.get("home_site")):
            is_24x7 = v["open_hour"] == 0 and v["close_hour"] == 24
            v["home_site"] = (offshore[0] if is_24x7 and offshore else site_names[i % len(site_names)])
        out.append(v)

    # ---- weights: explicit ones are kept, `auto` ones share the rest Zipf-style ----
    auto_idx = [i for i, v in enumerate(out) if _is_auto(v.get("weight"))]
    if auto_idx:
        explicit_sum = sum(float(v["weight"]) for i, v in enumerate(out) if i not in auto_idx)
        share = float(manual.get("auto_weight_share", 0.2))
        total = explicit_sum * share / (1.0 - share) if explicit_sum > 0 else 1.0
        for i, z in zip(auto_idx, _zipf(len(auto_idx), float(manual.get("volume_skew", 1.3)))):
            out[i]["weight"] = round(total * z, 6)

    # ---- overflow: the word `auto` => biggest other VQ of the same LOB (LOB leaders get none);
    #      missing or null => no overflow ----
    by_lob: Dict[str, List[Dict[str, Any]]] = {}
    for v in out:
        by_lob.setdefault(v["lob"], []).append(v)
    for v in out:
        if _is_auto_word(v.get("overflow_vq")):
            peers = sorted((p for p in by_lob[v["lob"]] if p is not v), key=lambda p: -p["weight"])
            v["overflow_vq"] = peers[0]["name"] if peers and peers[0]["weight"] > v["weight"] else None
        else:
            v.setdefault("overflow_vq", None)
    return out


def _auto_transfer_targets(lob: str, vqs: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """40 % of transfers stay inside the LOB (to its other queues), 60 % go to the leaders of the
    other LOBs in proportion to their volume."""
    same = sorted((v for v in vqs if v["lob"] == lob), key=lambda v: -v["weight"])
    leaders: Dict[str, Dict[str, Any]] = {}
    lob_volume: Dict[str, float] = {}
    for v in vqs:
        lob_volume[v["lob"]] = lob_volume.get(v["lob"], 0.0) + float(v["weight"])
        if v["lob"] != lob and (v["lob"] not in leaders or v["weight"] > leaders[v["lob"]]["weight"]):
            leaders[v["lob"]] = v
    targets: Dict[str, float] = {}
    inside = same[1:] if len(same) > 1 else same
    inside_w = sum(float(v["weight"]) for v in inside) or 1.0
    inside_share = 0.4 if leaders else 1.0
    for v in inside:
        targets[v["name"]] = round(inside_share * float(v["weight"]) / inside_w, 4)
    other_w = sum(lob_volume[l] for l in leaders) or 1.0
    for l, v in leaders.items():
        targets[v["name"]] = round(targets.get(v["name"], 0.0) + 0.6 * lob_volume[l] / other_w, 4)
    return targets


def _fill_lobs(lobs: Optional[List[Dict[str, Any]]], vqs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    library = _lob_dicts_from_defaults()
    shorts = _declared_shorts(lobs)
    lob_fields = {f.name for f in fields(LOBConfig)}
    # LOBs referenced by a VQ but not declared are added with library / generic behaviour.
    lobs = [({"name": l} if isinstance(l, str) else dict(l)) for l in (lobs or [])]
    declared = {l.get("name") for l in lobs}
    for v in vqs:
        if v["lob"] not in declared:
            lobs.append({"name": v["lob"]})
            declared.add(v["lob"])
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(lobs):
        if not isinstance(raw, dict) or _is_auto(raw.get("name")):
            raise ValueError(f"lobs[{i}]: every LOB needs a `name`")
        unknown = set(raw) - lob_fields
        if unknown:
            raise ValueError(f"LOB {raw['name']}: unknown fields {sorted(unknown)}")
        l = dict(raw)
        tpl = library.get(l["name"], library[GENERIC_LOB])
        if _is_auto(l.get("short")):
            l["short"] = _short(l["name"], shorts)
        for f in ("business_outcomes", "outcome_weights"):
            if _is_auto(l.get(f)):
                l[f] = tpl[f]
        if _is_auto(l.get("transfer_targets")):
            l["transfer_targets"] = _auto_transfer_targets(l["name"], vqs)
        out.append(l)
    return out


# --------------------------------------------------------------------------- #
# Auto mode: design a catalogue from a handful of numbers
# --------------------------------------------------------------------------- #
def _auto_skeleton(auto: Dict[str, Any]) -> List[Dict[str, Any]]:
    lobs: List[str] = list(auto["lobs"])
    n = int(auto["n_vqs"])
    if n < len(lobs):
        raise ValueError(f"catalogue.auto.n_vqs ({n}) must be >= the number of LOBs ({len(lobs)})")
    shares_raw = {l: float(auto.get("lob_shares", {}).get(l, LOB_LIBRARY.get(l, {}).get("share", 1.0 / len(lobs))))
                  for l in lobs}
    for l in auto.get("lob_shares", {}):
        if l not in lobs:
            raise ValueError(f"catalogue.auto.lob_shares references LOB {l} which is not in catalogue.auto.lobs")
    tot = sum(shares_raw.values()) or 1.0
    shares = {l: s / tot for l, s in shares_raw.items()}

    # ---- number of VQs per LOB: at least one, the rest by share (largest remainder) ----
    spare = n - len(lobs)
    exact = {l: shares[l] * spare for l in lobs}
    counts = {l: 1 + int(math.floor(exact[l])) for l in lobs}
    for l in sorted(lobs, key=lambda l: -(exact[l] - math.floor(exact[l])))[: n - sum(counts.values())]:
        counts[l] += 1

    # ---- names and weights ----
    skew = float(auto["volume_skew"])
    pattern = auto.get("name_pattern", "VQ_{abbrev}_{suffix}")
    vqs: List[Dict[str, Any]] = []
    shorts = _declared_shorts(None)
    for lob in lobs:
        suffixes = LOB_LIBRARY.get(lob, {}).get("suffixes", GENERIC_SUFFIXES)
        for rank, z in enumerate(_zipf(counts[lob], skew)):
            suffix = suffixes[rank] if rank < len(suffixes) else f"Q{rank + 1}"
            name = pattern.format(lob=lob, abbrev=_abbrev(lob), short=_short(lob, shorts), suffix=suffix, n=rank + 1)
            vqs.append({"name": name, "lob": lob, "weight": round(shares[lob] * z, 6), "_rank": rank})
    names = [v["name"] for v in vqs]
    if len(set(names)) != len(names):
        raise ValueError("catalogue.auto.name_pattern does not produce unique VQ names; include {suffix} or {n}")

    # ---- hours: biggest 24x7-eligible queues run around the clock, smallest are weekday-only ----
    templates = _vq_templates()
    generic = templates[GENERIC_TEMPLATE]
    order = sorted(range(len(vqs)), key=lambda i: -vqs[i]["weight"])
    n_24 = int(round(float(auto["always_open_share"]) * n))
    n_wd = int(round(float(auto["weekday_only_share"]) * n))
    picked_24 = [i for i in order if LOB_LIBRARY.get(vqs[i]["lob"], {}).get("may_24x7", False)][:n_24]
    picked_wd = [i for i in reversed(order) if i not in picked_24][:n_wd]
    for i, v in enumerate(vqs):
        tpl = templates.get(v["lob"], generic)
        if i in picked_24:
            v["open_hour"], v["close_hour"], v["days"] = 0, 24, "Mon-Sun"
        elif i in picked_wd:
            v["open_hour"], v["close_hour"], v["days"] = 8, 20, "Mon-Fri"
        elif tpl.open_hour == 0 and tpl.close_hour == 24:
            v["open_hour"], v["close_hour"], v["days"] = 7, 23, "Mon-Sun"
        else:
            v["open_hour"], v["close_hour"], v["days"] = tpl.open_hour, tpl.close_hour, tpl.days

    # ---- PBR: natural candidates (Sales / Retention) first, biggest first ----
    n_pbr = int(round(float(auto["pbr_share"]) * n))
    cand = [i for i in order if LOB_LIBRARY.get(vqs[i]["lob"], {}).get("pbr", False)]
    cand += [i for i in order if i not in cand]
    for i in cand[:n_pbr]:
        vqs[i]["pbr_enabled"] = True
    for v in vqs:
        v.setdefault("pbr_enabled", False)

    # ---- overflow: a share of the non-leader queues overflow to their LOB leader ----
    non_leaders = [i for i in reversed(order) if vqs[i]["_rank"] > 0]
    n_of = int(round(float(auto["overflow_share"]) * len(non_leaders)))
    for i in non_leaders[:n_of]:
        vqs[i]["overflow_vq"] = AUTO
    for v in vqs:
        v.setdefault("overflow_vq", None)

    # ---- AHT / ASA: niche queues take longer and answer slower ----
    spread = float(auto["aht_spread"])
    for v in vqs:
        tpl = templates.get(v["lob"], generic)
        r = v.pop("_rank")
        v["talk_median_s"] = round(tpl.talk_median_s * (1.0 + spread * r), 1)
        v["base_asa_s"] = round(tpl.base_asa_s * (1.0 + 0.4 * r), 1)
    return vqs


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def resolve_catalogue(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the config dict with complete `lobs` / `vqs` lists according to `catalogue.mode`."""
    cat = data.get("catalogue") or {}
    mode = cat.get("mode", "default")
    sites = data.get("sites") or []
    manual = cat.get("manual") or {}
    shorts = _declared_shorts(data.get("lobs"))

    if mode == "auto":
        auto = cat.get("auto") or {}
        vqs = _auto_skeleton(auto)
        vqs = _fill_vqs(vqs, sites, {"volume_skew": auto.get("volume_skew", 1.3)}, shorts)
        lobs = _fill_lobs([{"name": l} for l in auto["lobs"]], vqs)
    elif mode == "manual":
        if not data.get("vqs"):
            raise ValueError("catalogue.mode is manual but no `vqs:` list was given")
        vqs = _fill_vqs(list(data["vqs"]), sites, manual, shorts)
        lobs = _fill_lobs(data.get("lobs"), vqs)
    elif mode == "default":
        vqs = _fill_vqs(list(data.get("vqs") or []), sites, manual, shorts)
        lobs = _fill_lobs(data.get("lobs"), vqs)
    else:
        raise ValueError(f"catalogue.mode must be one of default, manual, auto (got {mode!r})")

    data = dict(data)
    data["lobs"], data["vqs"] = lobs, vqs

    # Built-in outbound campaigns point at library LOBs; drop those whose LOB does not exist in a
    # custom catalogue (user-declared campaigns are left alone so typos still fail validation).
    lob_names = {l["name"] for l in lobs}
    from .config import OutboundConfig
    builtin = OutboundConfig().campaigns
    campaigns = dict((data.get("outbound") or {}).get("campaigns") or {})
    for camp, lob in list(campaigns.items()):
        if lob not in lob_names and builtin.get(camp) == lob:
            del campaigns[camp]
    if not campaigns and mode in ("manual", "auto"):
        campaigns = {f"CAMP_{_abbrev(l)}_Outbound": l for l in sorted(lob_names)}
    data.setdefault("outbound", {})
    data["outbound"] = dict(data["outbound"], campaigns=campaigns)
    return data


def describe_catalogue(cfg) -> List[Dict[str, Any]]:
    """Rows for the `catalogue` CLI command: one per VQ with its share and derived expectations."""
    total = sum(v.weight for v in cfg.vqs) or 1.0
    inbound_per_day = cfg.calls_per_day * cfg.mix.inbound
    rows = []
    for v in sorted(cfg.vqs, key=lambda v: -v.weight):
        rows.append({
            "VQ": v.name, "LOB": v.lob, "share_%": round(100 * v.weight / total, 2),
            "calls_per_weekday": int(round(inbound_per_day * v.weight / total)),
            "hours": f"{v.open_hour:02d}-{v.close_hour:02d} {v.days}",
            "routing": "PBR" if v.pbr_enabled else "ACD",
            "talk_median_s": int(v.talk_median_s), "SL_s": v.service_level_s,
            "overflow_to": v.overflow_vq or "", "home_site": v.home_site,
        })
    return rows
