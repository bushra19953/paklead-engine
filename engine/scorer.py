"""
Lead scorer — 6-input model (TRD §1.4, FR-PK-39…46).

Every score carries its six-input breakdown (NFR-PK-6: "why is this lead
78?" is never a mystery). Weights live in the versioned config; the score
record stores the config version. Deterministic: same inputs, same score.
"""

from __future__ import annotations

import datetime

from config import load_scoring_config


def _family_of(intent: str, cfg: dict) -> str:
    return cfg["intent_window_family"].get(intent, "seeking")


def _freshness_days(intent: str, cfg: dict) -> int:
    return cfg["freshness_windows"][_family_of(intent, cfg)]


def score_lead(lead: dict, cfg: dict | None = None,
               now: datetime.datetime | None = None,
               cleared_complaint: bool = False) -> dict:
    """Compute the six-input score for a merged lead.

    lead keys: intent, category_weight, price_inquiry, first_seen (ISO str
    or datetime), budget_pkr, budget_phrase_matched, urgency_count,
    platform_count, contact_type, serious_buyer, replacement_demand.
    """
    cfg = cfg or load_scoring_config()
    now = now or datetime.datetime.now(datetime.timezone.utc)

    # --- 1. intent_strength (category weight x 6; PRD §8 worked examples) ---
    weight = float(lead.get("category_weight", 5))
    intent_strength = int(weight * cfg["intent_multiplier"])
    if lead.get("price_inquiry"):
        intent_strength += cfg["price_inquiry_bonus"]
    if lead.get("intent") == "complaint_pain" and cleared_complaint \
            and lead.get("replacement_demand"):
        intent_strength += cfg["complaint_replacement_bonus"]
    if lead.get("serious_buyer"):
        intent_strength += cfg["serious_buyer_bonus"]

    # --- 2. recency: 15 within 24h, linear decay to 0 at window end ---
    first_seen = lead.get("first_seen")
    if isinstance(first_seen, str):
        first_seen = datetime.datetime.fromisoformat(first_seen)
    age_days = max(0.0, (now - first_seen).total_seconds() / 86400.0)
    window = _freshness_days(lead.get("intent", ""), cfg)
    full_h = cfg["recency_full_hours"] / 24.0
    if age_days <= full_h:
        recency = cfg["recency_max"]
    elif age_days >= window:
        recency = 0
    else:
        recency = int(round(cfg["recency_max"] * (1 - (age_days - full_h) / (window - full_h))))

    # --- 3. budget ---
    if lead.get("budget_pkr"):
        budget = cfg["budget_parsed"]
    elif lead.get("budget_phrase_matched"):
        budget = cfg["budget_phrase_only"]
    else:
        budget = 0

    # --- 4. urgency ---
    urgency = min(cfg["urgency_cap"],
                  lead.get("urgency_count", 0) * cfg["urgency_per_marker"])

    # --- 5. cross_platform ---
    nplat = lead.get("platform_count", 1)
    cross_platform = (cfg["cross_platform_three_plus"] if nplat >= 3
                      else cfg["cross_platform_two"] if nplat == 2 else 0)

    # --- 6. contactability (public-post data only, SHR-3) ---
    ctype = lead.get("contact_type", "none")
    contactability = {
        "phone": cfg["contact_phone_published"],
        "whatsapp": cfg["contact_phone_published"],
        "open_thread": cfg["contact_open_thread"],
        "inbox": cfg["contact_inbox_gate"],
    }.get(ctype, 0)

    total = intent_strength + recency + budget + int(urgency) + cross_platform + contactability
    total = max(0, min(100, total))

    # --- hot triggers (TRD FR-PK-43) ---
    hot_trigger = None
    if total >= cfg["hot_threshold"]:
        hot_trigger = "score_threshold"
    if (lead.get("intent") == "complaint_pain" and cleared_complaint
            and lead.get("replacement_demand")):
        hot_trigger = "complaint_with_replacement_urgency"
    if lead.get("intent") == "hiring" and lead.get("stated_deadline"):
        hot_trigger = "hiring_with_deadline"
    if lead.get("intent", "").startswith("seeking") and urgency >= cfg["urgency_cap"] \
            and age_days <= 1.0:
        hot_trigger = "same_day_urgency_seeking"

    return {
        "scoring_config_version": cfg["version"],
        "intent_strength": int(intent_strength),
        "recency": int(recency),
        "budget": int(budget),
        "urgency": int(urgency),
        "cross_platform": int(cross_platform),
        "contactability": int(contactability),
        "total": int(total),
        "hot_flag": hot_trigger is not None,
        "hot_trigger": hot_trigger,
        "expires_at": (first_seen + datetime.timedelta(days=window)).isoformat(),
    }
