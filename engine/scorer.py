"""
Lead scorer — 6-input model (TRD §1.4, FR-PK-39…46).

Every score carries its six-input breakdown (NFR-PK-6: "why is this lead
78?" is never a mystery). Weights live in the versioned config; the score
record stores the config version. Deterministic: same inputs, same score.
"""

from __future__ import annotations

import datetime

from config import load_scoring_config

# ----------------------------------------------------------------------
# Rental-cap rule (B26, wholesale/disposition use case).
#
# Pure rental listings (daily-rent flats, industrial land for rent) used to
# classify as property_offered and land in the HOT band, drowning the
# disposition signal. The rule is data-driven: marker lists and the cap
# value live in the scoring config; these module constants are only the
# fallbacks so the rule works on configs that predate the keys.
# ----------------------------------------------------------------------
DEFAULT_RENTAL_MARKERS = [
    # English + Roman-Urdu rental phrasing (substring match, case-insensitive)
    "for rent", "kiraye par", "rent par", "monthly rent", "per day",
    "daily rent", "on rent", "kiraya",
]
DEFAULT_SALE_MARKERS = [
    # Explicit sale phrasing — "demand" is the Pakistan property-portal word
    # for asking price (a sale signal), distinct from rental pricing.
    "for sale", "sale k liye", "bechna hai", "bech raha", "farokht",
    "demand", "for sell",
]
DEFAULT_RENTAL_SCORE_CAP = 64  # below hot_threshold (70): rentals stay out of HOT


def _is_pure_rental(post_texts: list[str], cfg: dict) -> bool:
    """True when merged post texts carry rental markers but no sale markers.

    A lead is "pure rental" only when its posts show explicit rental intent
    AND nothing that reads as a sale. Mixed or ambiguous posts (both kinds
    of markers, or none) return False and are left to the normal scorer.
    """
    texts = [t.lower() for t in (post_texts or []) if t]
    if not texts:
        return False
    rental_markers = cfg.get("rental_markers") or DEFAULT_RENTAL_MARKERS
    sale_markers = cfg.get("sale_markers") or DEFAULT_SALE_MARKERS
    has_rental = any(m.lower() in t for t in texts for m in rental_markers)
    if not has_rental:
        return False
    has_sale = any(m.lower() in t for t in texts for m in sale_markers)
    return not has_sale


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
    platform_count, contact_type, serious_buyer, replacement_demand,
    post_texts (optional list of str: the merged post texts; drives the
    B26 rental-cap rule — omit it and the rule simply does not fire).
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

    # --- rental cap (B26, wholesale use case): pure-rental listings are low
    #     value for disposition and must not flood the HOT band. The cap is
    #     overridden only by genuine urgency in the scorer's own vocabulary:
    #     an explicit urgency marker ("urgent"/"foran"/deadline — counted in
    #     urgency_count) PLUS a published contact channel (phone/WhatsApp).
    #     Why: an urgent rental with a published phone number is an
    #     immediately actionable, motivated lessor — a real disposition
    #     signal. Urgency without a way to reach the poster (open_thread /
    #     inbox only) is noise, not a hot lead, and stays capped.
    rental_cap = None
    if _is_pure_rental(lead.get("post_texts") or [], cfg):
        cap = cfg.get("rental_score_cap", DEFAULT_RENTAL_SCORE_CAP)
        overridden = (lead.get("urgency_count", 0) >= 1
                      and ctype in ("phone", "whatsapp"))
        rental_cap = {
            "cap": int(cap),
            "pre_cap_total": int(total),
            "applied": False,
            "overridden": bool(overridden),
            "reason": "urgency_with_published_contact" if overridden
                      else "pure_rental",
        }
        if not overridden and total > cap:
            total = cap
            rental_cap["applied"] = True
    rental_cap_applied = bool(rental_cap and rental_cap["applied"])

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

    # A rental-capped lead stays below the HOT band: the score cap alone
    # would silence score_threshold, but business-rule triggers must not
    # re-promote it either (only the urgency override above lifts the cap).
    if rental_cap_applied:
        hot_trigger = None

    result = {
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
    # Explainability (NFR-PK-6): the rental rule leaves its own line in the
    # breakdown whenever it fires — whether it capped the score or was
    # overridden by genuine urgency with a published contact.
    if rental_cap is not None:
        result["rental_cap"] = rental_cap
    return result
