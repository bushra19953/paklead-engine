"""
Loophole engine (TRD §1.6, FR-PK-53…58).

Finds unmet demand over a rolling 30-day window:
  - ratio_gap:      seeker:provider ratio >= 3 with a seeker floor
  - underserved_city: seeker volume with near-zero provider replies
  - complaint_theme: clustered pain points (themes reported, never as
                     accusations against named providers — TRD FR-PK-54)
  - price_gap:      seeker budgets vs. market anchor prices

Reports are immutable once issued; staleness is handled by the pipeline
(FR-PK-57/58).
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict

SEEKER_INTENTS = {
    "seeking_service", "seeking_product", "job_seeking",
    "freelance_seeking", "property_wanted",
}
PROVIDER_INTENTS = {
    "freelance_offering", "selling", "property_offered", "hiring",
}

COMPLAINT_THEME_WORDS = re.compile(
    r"(late|no[- ]?show|advance|fraud|scam|overcharg|mehnga|expensive|"
    r"rude|badtameez|unprofessional|delay|ghaib|disappear)", re.I)


def _theme_of(text: str) -> str:
    words = sorted(set(m.group(0).lower() for m in COMPLAINT_THEME_WORDS.finditer(text)))
    return "+".join(words[:3]) if words else "general_complaint"


def detect_loopholes(leads: list[dict], posts: list[dict], comments: list[dict],
                      cfg: dict, now: datetime.datetime | None = None,
                      anchors: dict | None = None) -> list[dict]:
    """leads: lead dicts (intent, city, vertical, lead_id).
    posts: post dicts (post_id, intent_primary, city_tag, is_provider_reply
      implied via comments). comments: comment dicts (post_id,
      is_provider_reply). Returns report dicts ready for the DB.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    window_days = cfg.get("loophole_window_days", 30)
    window_start = now - datetime.timedelta(days=window_days)
    min_ratio = cfg.get("loophole_min_ratio", 3.0)
    min_seekers = cfg.get("loophole_min_seekers", 5)
    us_min = cfg.get("underserved_min_seekers", 10)
    us_max_rate = cfg.get("underserved_max_provider_reply_rate", 0.05)
    price_gap_pct = cfg.get("price_gap_pct", 25.0)
    reports: list[dict] = []

    # Provider replies per post (FR-PK-04 flags)
    provider_reply_posts = {c["post_id"] for c in comments if c.get("is_provider_reply")}

    # seeker vs provider counts per (vertical, city)
    seekers: Counter = Counter()
    providers: Counter = Counter()
    seeker_post_ids: defaultdict = defaultdict(list)
    for lead in leads:
        if lead.get("status") == "dead":
            continue
        key = (lead.get("vertical") or "services", lead.get("city") or "Unknown")
        if lead.get("intent") in SEEKER_INTENTS:
            seekers[key] += 1
        elif lead.get("intent") in PROVIDER_INTENTS:
            providers[key] += 1
    for post in posts:
        if post.get("intent_primary") in SEEKER_INTENTS:
            seeker_post_ids[(post.get("vertical") or "services",
                             post.get("city_tag") or "Unknown")].append(post["post_id"])

    # --- ratio gaps ---
    for (vertical, city), s_count in seekers.items():
        p_count = providers.get((vertical, city), 0)
        ratio = s_count / p_count if p_count else float(s_count)
        if s_count >= min_seekers and ratio >= min_ratio:
            reports.append({
                "report_type": "ratio_gap",
                "vertical": vertical, "city": city,
                "seeker_count": s_count, "provider_count": p_count,
                "ratio": round(ratio, 2),
                "evidence_post_ids": seeker_post_ids[(vertical, city)][:5],
                "window_start": window_start.isoformat(), "window_end": now.isoformat(),
            })

    # --- underserved cities ---
    seeker_posts_by_city: Counter = Counter()
    replied_by_city: Counter = Counter()
    for post in posts:
        if post.get("intent_primary") in SEEKER_INTENTS and post.get("city_tag"):
            seeker_posts_by_city[post["city_tag"]] += 1
            if post["post_id"] in provider_reply_posts:
                replied_by_city[post["city_tag"]] += 1
    for city, count in seeker_posts_by_city.items():
        rate = replied_by_city.get(city, 0) / count if count else 0
        if count >= us_min and rate < us_max_rate:
            reports.append({
                "report_type": "underserved_city",
                "vertical": "services", "city": city,
                "seeker_count": count, "provider_count": replied_by_city.get(city, 0),
                "ratio": round(count / max(replied_by_city.get(city, 0), 1), 2),
                "evidence_post_ids": [],
                "window_start": window_start.isoformat(), "window_end": now.isoformat(),
            })

    # --- complaint themes (themes only, never accusations) ---
    theme_counts: Counter = Counter()
    theme_posts: defaultdict = defaultdict(list)
    for post in posts:
        if post.get("intent_primary") == "complaint_pain" and post.get("city_tag"):
            theme = _theme_of(post.get("text_original", ""))
            key = (theme, post["city_tag"])
            theme_counts[key] += 1
            theme_posts[key].append(post["post_id"])
    for (theme, city), count in theme_counts.items():
        if count >= 3:
            reports.append({
                "report_type": "complaint_theme",
                "vertical": "services", "city": city,
                "seeker_count": count, "provider_count": 0,
                "ratio": float(count),
                "complaint_themes": {theme: count},
                "evidence_post_ids": theme_posts[(theme, city)][:5],
                "window_start": window_start.isoformat(), "window_end": now.isoformat(),
            })

    # --- price gaps (seeker budgets vs market anchors) ---
    anchors = anchors or {}
    for post in posts:
        if post.get("budget_pkr") and post.get("intent_primary") in SEEKER_INTENTS:
            key = (post.get("intent_primary"), post.get("city_tag"))
            anchor = anchors.get(key)
            if anchor and anchor > 0:
                gap = abs(post["budget_pkr"] - anchor) / anchor * 100.0
                if gap >= price_gap_pct:
                    reports.append({
                        "report_type": "price_gap",
                        "vertical": post.get("vertical") or "services",
                        "city": post.get("city_tag"),
                        "seeker_count": 1, "provider_count": 0, "ratio": 0.0,
                        "price_gap_pct": round(gap, 1),
                        "evidence_post_ids": [post["post_id"]],
                        "window_start": window_start.isoformat(),
                        "window_end": now.isoformat(),
                    })
    return reports
