"""
Cross-platform comparator (TRD §1.5, FR-PK-47…52).

Tiers, in order:
  1. Source URL dedupe (SHR-15) — the same record is never stored twice.
  2. Published-phone hard key — identical normalized numbers across posts
     = same need, one lead. Phones are public-post data only (SHR-3).
     Intent-agnostic by design (B28 tweak 4): the pipeline feeds T2
     candidates across intent labels; the SQL intent prefilter applies to
     the text tiers only.
  3. Property identity — the same property cross-posted on two portals:
     same city + same intent family + near-identical title (synonym-
     normalized Jaccard ≥ 0.75, ≥6 content tokens) + asking price inside a
     LEAD-WIDE envelope (2%; 10% for buyer intents) + area agreement.
  4. Text similarity — stdlib TF-IDF cosine ≥ 0.65, gated on same city,
     same primary intent, a 72h publication window, a lead-wide price
     envelope (2% property / 10% buyer / 20% other), an area-mismatch veto
     for property content, and a rent-vs-sale veto.
  5. Semantic (Chroma) fallback is reserved for the reference stack and is
     NOT implemented here — tier 4 runs with pure stdlib.

Property-ness is decided from CONTENT (asking price + size token), never
from the intent label alone (B28 tweak 3) — `unclear`-labeled property
posts get the 2% gate too.

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

# B28 tweak 5: synonym normalization before Jaccard. Unit words
# (kanal/marla) stay as-is; listing-type synonyms unify so template
# variants of the same property still collide.
SYNONYMS = {
    "flat": "apartment", "apartment": "apartment", "apartments": "apartment",
    "plot": "plot", "plots": "plot", "land": "plot",
    "story": "storey", "storey": "storey", "stories": "storey",
    "marla": "marla", "marlas": "marla",
    "kanal": "kanal", "kanals": "kanal",
    "house": "house", "houses": "house",
    "villa": "villa", "villas": "villa",
}

# B28 tweak 5: rent-vs-sale veto word lists (English + Roman Urdu + Urdu).
RENT_WORDS = {
    "rent", "rental", "rented", "renting",
    "kiraya", "kirae", "kiraaye", "karaya", "kariye",
    "کرایہ", "كرایہ",
}
SALE_WORDS = {
    "sale", "sell", "selling", "sold",
    "demand", "dimnd", "demnd",
    "forokht", "farokht",
    "فروخت",
}

# B28 tweak 3: content-based property detection — asking price + size token.
SIZE_RE = re.compile(
    r"\b(marla|marlas|kanal|kanals|sq\s*ft|sqft|square\s*feet|"
    r"sq\s*yd|sqyd|sq\s*yards?|square\s*yards?|acre|acres)\b",
    re.IGNORECASE,
)

# A17: buyer-lane intents whose template merges need area + 10% budget gates.
BUYER_INTENTS = {"property_wanted", "seeking_product"}

# B28 tweak 5: T2.5 needs a substantive title before Jaccard can fire.
MIN_TITLE_TOKENS = 6


def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-zA-Z\u0600-\u06FF]{3,}", text.lower())
    return [t for t in toks if t not in STOPWORDS]


def _norm_tokens(text: str) -> list[str]:
    """Stopword-stripped tokens with listing-type synonyms unified."""
    return [SYNONYMS.get(t, t) for t in _tokens(text)]


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


def _canon_phone(value) -> str:
    """Canonical digit string for phone comparison.

    Mirrors engine.classifier.normalize_phone's 92/0092 handling so
    +923115850472, 00923115850472 and 03115850472 all match. (Stored
    contact_published values are already canonical 03XXXXXXXXX, so this
    is mostly a safety net.)
    """
    if not value:
        return ""
    d = re.sub(r"\D", "", str(value))
    if d.startswith("0092"):
        d = "0" + d[4:]
    elif d.startswith("92") and len(d) == 12:
        d = "0" + d[2:]
    return d


def _is_property_content(post: dict) -> bool:
    """True when the post reads as a property listing: an asking price AND
    a size token (marla/kanal/sqft/…) — regardless of intent_primary."""
    if not post.get("budget_pkr"):
        return False
    blob = " ".join([post.get("text_original") or "",
                     post.get("text_normalized") or ""])
    return bool(SIZE_RE.search(blob))


def _tier_family(post: dict) -> str:
    """'buyer' | 'property' | 'other' — decided from content, not labels."""
    ip = post.get("intent_primary") or ""
    if ip in BUYER_INTENTS:
        return "buyer"
    if ip.startswith("property") or _is_property_content(post):
        return "property"
    return "other"


def _price_tol(family: str) -> float:
    # B28 tweak 1: lead-wide envelope tolerances. Price IS identity for
    # property (2%); buyer ads get 10% (A17); everything else 20%.
    return {"buyer": 0.10, "property": 0.02}.get(family, 0.20)


def _envelope_ok(post: dict, lead: dict, tol: float) -> bool:
    """Lead-wide price envelope (B28 tweak 1): the new post must not push
    the lead's total price spread (max-min over ALL member posts) beyond
    tolerance. This stops chain-walking (2.9 → 4.65 Cr), which the old
    per-pair gate allowed."""
    prices = [mp.get("budget_pkr") for mp in lead["merged_posts"]
              if mp.get("budget_pkr")]
    if post.get("budget_pkr"):
        prices.append(post["budget_pkr"])
    if len(prices) < 2:
        return True
    lo, hi = min(prices), max(prices)
    return (hi - lo) / hi <= tol


def _norm_area(value) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _area_conflict(a: dict, b: dict) -> bool:
    """B28 tweak 2: two EXPLICITLY different areas veto a property merge.
    (Missing area on either side is not evidence — it passes.)"""
    aa, ab = _norm_area(a.get("area_tag")), _norm_area(b.get("area_tag"))
    return bool(aa and ab and aa != ab)


def _same_area(a: dict, b: dict) -> bool:
    """A17: buyer template-merges require the SAME explicit area_tag."""
    aa, ab = _norm_area(a.get("area_tag")), _norm_area(b.get("area_tag"))
    return bool(aa and ab and aa == ab)


def _tenure_side(post: dict) -> str | None:
    """'rent' | 'sale' | None — None when ambiguous (both/neither side)."""
    blob = " ".join([post.get("text_original") or "",
                     post.get("text_normalized") or ""]).lower()
    words = set(re.findall(r"[\w\u0600-\u06FF]+", blob))
    rent = bool(words & RENT_WORDS)
    sale = bool(words & SALE_WORDS)
    if rent and not sale:
        return "rent"
    if sale and not rent:
        return "sale"
    return None


def _rent_sale_veto(a: dict, b: dict) -> bool:
    """B28 tweak 5: a rent marker on one side and a sale marker on the
    other vetoes the merge (F6: rentals fused into for-sale leads)."""
    sa, sb = _tenure_side(a), _tenure_side(b)
    return bool(sa and sb and sa != sb)


def _title_tokens(post: dict) -> set[str]:
    first = (post.get("text_original") or "").split(".")[0]
    return set(_norm_tokens(first))


def _jaccard_ok(post: dict, mp: dict) -> bool:
    ta, tb = _title_tokens(post), _title_tokens(mp)
    if not ta or not tb:
        return False
    if len(ta | tb) < MIN_TITLE_TOKENS:
        return False  # B28 tweak 5: floor the title length (F3)
    return len(ta & tb) / len(ta | tb) >= 0.75


def _parse_dt(value) -> datetime.datetime | None:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _within_window(post: dict, mp: dict, seconds: float) -> bool:
    dt_a = _parse_dt(post.get("published_at")) or _parse_dt(post.get("seen_at"))
    dt_b = _parse_dt(mp.get("published_at")) or _parse_dt(mp.get("seen_at"))
    return bool(dt_a and dt_b and abs((dt_a - dt_b).total_seconds()) <= seconds)


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
        fam = _tier_family(post)
        tol = _price_tol(fam)

        # Tier 2: published-phone hard key (public-post numbers only).
        # Intent-agnostic: the same normalized number in-window merges
        # regardless of intent label (B28 tweak 4 — the pipeline no longer
        # prefilters T2 candidates by intent).
        phone = _canon_phone(post.get("contact_published"))
        if phone:
            for lead in existing:
                for mp in lead["merged_posts"]:
                    if _canon_phone(mp.get("contact_published")) == phone:
                        if _within_window(post, mp, self.phone_days * 86400):
                            return lead, (f"matched on published number {phone} "
                                          f"(cross-platform hard key)")

        # Tier 2.5: property identity — the same property cross-posted on
        # two portals. B28 tweaks: content-based property detection (3),
        # lead-wide price envelope (1), area-agreement gate (2), synonym
        # normalization + rent/sale veto (5); A17 buyer gate.
        if fam in ("buyer", "property"):
            pi = post.get("intent_primary") or ""
            for lead in existing:
                if lead.get("city") != post.get("city_tag"):
                    continue
                li = lead.get("intent")
                if li != pi:
                    # Content-based relaxation: an unclear-labeled property
                    # post may identity-match a property lead; otherwise the
                    # intent gate stands (buyer lanes never cross).
                    if not (fam == "property" and "unclear" in (li, pi)):
                        continue
                if not _envelope_ok(post, lead, tol):
                    continue  # B28 tweak 1: lead-wide, not per-pair
                for mp in lead["merged_posts"]:
                    mpi = mp.get("intent_primary") or ""
                    mfam = _tier_family(mp)
                    if mfam != fam:
                        continue
                    if pi != mpi and not (fam == "property"
                                          and "unclear" in (pi, mpi)):
                        continue
                    if _rent_sale_veto(post, mp):
                        continue
                    if fam == "buyer":
                        # A17: buyer template-merges need the SAME area_tag
                        # and the 10% envelope already enforced above.
                        if not _same_area(post, mp):
                            continue
                    elif _area_conflict(post, mp):
                        continue  # B28 tweak 2
                    if _jaccard_ok(post, mp):
                        return lead, ("property identity: same title + asking "
                                      f"price {post.get('budget_pkr')} "
                                      f"(cross-portal same property)")

        # Tier 3: text similarity with hard gates
        for lead in existing:
            if lead.get("city") != post.get("city_tag"):
                continue
            if lead.get("intent") != post.get("intent_primary"):
                continue
            if not _envelope_ok(post, lead, tol):
                continue  # B28 tweak 1: lead-wide price envelope
            for mp in lead["merged_posts"]:
                if not _within_window(post, mp, self.window_h * 3600):
                    continue
                if fam in ("buyer", "property"):
                    if _rent_sale_veto(post, mp):
                        continue  # B28 tweak 5
                    if fam == "buyer":
                        if not _same_area(post, mp):
                            continue  # A17
                    elif _area_conflict(post, mp):
                        continue  # B28 tweak 2
                sim = tfidf_cosine(post.get("text_normalized", ""),
                                   mp.get("text_normalized", ""))
                if sim >= self.sim_threshold:
                    return lead, (f"cosine {sim:.2f} >= {self.sim_threshold} + same city "
                                  f"({lead.get('city')}) + same intent + "
                                  f"{self.window_h}h window")
        return None, ("no merge: closest candidates differed on city, intent, "
                      "contact, or time window (FR-PK-51 false-merge guard)")
