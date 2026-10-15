"""
Cross-platform comparator (TRD §1.5, FR-PK-47…52).

Tiers, in order:
  1. Source URL dedupe (SHR-15) — the same record is never stored twice.
  2. Published-phone hard key — identical normalized numbers across posts
     = same need, one lead. Phones are public-post data only (SHR-3).
  3. Text similarity — stdlib TF-IDF cosine ≥ 0.65, gated on same city,
     same primary intent, and a 72h publication window.
  4. Semantic (Chroma) fallback is reserved for the reference stack and is
     NOT implemented here — tier 3 runs with pure stdlib.

Auto-clustering links NEEDS, never identities (SHR-9): no automatic
two-handles-one-person merge exists. Every merge stores its explanation
(FR-PK-51), including deliberate non-merges of lookalikes.
"""

from __future__ import annotations

import datetime
import math
import re
from collections import Counter

STOPWORDS = {
    "hai", "hain", "haii", "mein", "main", "kya", "koi", "wala", "wale",
    "liye", "karna", "chahiye", "chahye", "the", "a", "an", "and", "or",
    "for", "to", "of", "in", "on", "is", "are", "ko", "ki", "ka", "ke",
    "se", "par", "me", "bhi", "hi", "bhai", "sir", "plz", "kindly", "urgent",
}


def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-zA-Z\u0600-\u06FF]{3,}", text.lower())
    return [t for t in toks if t not in STOPWORDS]


def tfidf_cosine(text_a: str, text_b: str) -> float:
    """Cosine similarity over simple TF vectors (binary IDF over 2 docs)."""
    ta, tb = _tokens(text_a), _tokens(text_b)
    if not ta or not tb:
        return 0.0
    va, vb = Counter(ta), Counter(tb)
    # binary idf across the two documents
    idf = {}
    for t in set(va) | set(vb):
        df = (t in va) + (t in vb)
        idf[t] = math.log(2.0 / df) + 1.0
    dot = sum(va[t] * vb[t] * idf[t] ** 2 for t in set(va) & set(vb))
    na = math.sqrt(sum((va[t] * idf[t]) ** 2 for t in va))
    nb = math.sqrt(sum((vb[t] * idf[t]) ** 2 for t in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _parse_dt(value) -> datetime.datetime | None:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


class Comparator:
    def __init__(self, cfg: dict):
        self.sim_threshold = cfg.get("cluster_text_similarity_threshold", 0.65)
        self.window_h = cfg.get("cluster_time_window_hours", 72)
        self.phone_days = cfg.get("cluster_phone_window_days", 7)

    # ------------------------------------------------------------------
    def find_cluster(self, post: dict, existing: list[dict]) -> tuple[dict | None, str]:
        """Return (matching lead-dict, explanation) or (None, non-merge note).

        `existing` items: dicts with keys lead_id, merged_posts (list of post
        dicts), city, intent, contact_published, first_seen.
        """
        # Tier 2: published-phone hard key (public-post numbers only)
        phone = post.get("contact_published")
        if phone:
            for lead in existing:
                for mp in lead["merged_posts"]:
                    if mp.get("contact_published") == phone:
                        dt_a = _parse_dt(post.get("published_at")) or _parse_dt(post.get("seen_at"))
                        dt_b = _parse_dt(mp.get("published_at")) or _parse_dt(mp.get("seen_at"))
                        if dt_a and dt_b and abs((dt_a - dt_b).total_seconds()) <= self.phone_days * 86400:
                            return lead, (f"matched on published number {phone} "
                                          f"(cross-platform hard key)")
        # Tier 3: text similarity with hard gates
        for lead in existing:
            if lead.get("city") != post.get("city_tag"):
                continue
            if lead.get("intent") != post.get("intent_primary"):
                continue
            for mp in lead["merged_posts"]:
                dt_a = _parse_dt(post.get("published_at")) or _parse_dt(post.get("seen_at"))
                dt_b = _parse_dt(mp.get("published_at")) or _parse_dt(mp.get("seen_at"))
                if dt_a and dt_b and abs((dt_a - dt_b).total_seconds()) > self.window_h * 3600:
                    continue
                sim = tfidf_cosine(post.get("text_normalized", ""),
                                   mp.get("text_normalized", ""))
                if sim >= self.sim_threshold:
                    return lead, (f"cosine {sim:.2f} >= {self.sim_threshold} + same city "
                                  f"({lead.get('city')}) + same intent + "
                                  f"{self.window_h}h window")
        return None, ("no merge: closest candidates differed on city, intent, "
                      "contact, or time window (FR-PK-51 false-merge guard)")
