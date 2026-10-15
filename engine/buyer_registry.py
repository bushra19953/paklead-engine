"""
Buyer registry + seller-to-buyer matcher — the Disposition (finding buyers)
pillar of the PakLead Engine.

Derived ONLY from public post data (the `post` table) and used ONLY for
registry + matching. **There are no outreach code paths in this module:**
no messaging, no commenting, no contact attempts — matching surfaces
candidate buyers for the operator's human, on-platform outreach (SHR-14).

Identity rules (SHR-9, per Wave-A audit A17 §8):
    - Identical normalized phone numbers = the SAME buyer (strong merge key).
    - Name-only or handle-only matches = separate candidate identities,
      NEVER merged. Common names ("Muhammad Usama", "Zain Ali") collide
      constantly; merging on them is forbidden.
    - Anonymous buyers (no phone, no name, no handle — the norm today: all
      10 genuine buyer posts in the live DB carry zero identity fields)
      are still recorded as town+budget DEMAND SIGNALS, flagged
      ``anonymous=True``. They can match seller leads; their reasons cite
      town + budget only, never a person.
    - The merge key actually used is stored on every buyer record, so the
      operator can always audit *why* two posts were treated as one buyer.
    - Merge on phone is one-way across intent lanes: a phone on a
      property_wanted post joins that buyer's identity; a phone that only
      ever appears on seller ads is never promoted into a buyer.

Integration contract:
    - ``derive_buyers(conn)`` runs after each scoring pass (append it to the
      pipeline right after leads are scored). It is idempotent: re-running
      updates existing buyers (activity, towns, budgets, last_seen) and
      never duplicates them.
    - ``match_buyers(conn, seller_lead, limit=10)`` is called on demand by
      the dashboard/API when the operator wants candidate buyers for a
      seller lead (Wave-B 29/30: keep this signature stable).
    - ``get_buyers_for_lead(conn, lead_id)`` bridges a merged lead to
      ``match_buyers`` using the lead's posts for town/city/price/type.
    - ``buyer_summary(conn, buyer_id)`` returns one buyer with its evidence
      posts for the dashboard view.

Buyer lane query rule (A17 §3): ``intent_primary IN ('property_wanted',
'seeking_product')``. ``intent_secondary='property_wanted'`` is 99% seller
noise and is NEVER used here.

stdlib only (sqlite3, re, logging, json, datetime, uuid).
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import uuid

log = logging.getLogger(__name__)

__all__ = [
    "BUYER_INTENTS",
    "ensure_buyer_schema",
    "normalize_phone",
    "derive_buyers",
    "match_buyers",
    "get_buyers_for_lead",
    "buyer_summary",
    "list_buyers",
]

#: Buyer lanes — hard-coded per A17 §3 (primary only, never secondary).
BUYER_INTENTS = ("property_wanted", "seeking_product")

#: Merge-key prefixes, in precedence order.
_KEY_PHONE = "phone:"
_KEY_DEMAND = "demand:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer (
    buyer_id TEXT PRIMARY KEY,          -- 'buyer-' uuid or 'demand-' cluster id
    merge_key TEXT NOT NULL UNIQUE,     -- key this buyer was merged on
    identities TEXT NOT NULL DEFAULT '[]', -- JSON [{phone?, name?, handle?, agency_id?}]
    towns_of_interest TEXT NOT NULL DEFAULT '[]', -- JSON [{town, city, evidence_count}]
    budget_min_pkr INTEGER,
    budget_max_pkr INTEGER,
    property_types TEXT NOT NULL DEFAULT '[]', -- JSON array (from vertical/inferred)
    activity_count INTEGER NOT NULL DEFAULT 0, -- distinct buyer posts seen
    evidence_post_ids TEXT NOT NULL DEFAULT '[]', -- JSON array of post_id
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    source_platforms TEXT NOT NULL DEFAULT '[]', -- JSON array
    anonymous INTEGER NOT NULL DEFAULT 1, -- 1 = no identity fields ever seen
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buyer_merge_key ON buyer(merge_key);
CREATE INDEX IF NOT EXISTS idx_buyer_last_seen ON buyer(last_seen);
"""

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def ensure_buyer_schema(conn) -> None:
    """Create the buyer table (+ indexes) if missing. Idempotent."""
    for stmt in _SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()


def _utcnow_iso() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# identity / merge keys
# ---------------------------------------------------------------------------


def normalize_phone(raw: str | None) -> str | None:
    """Normalize a Pakistani phone number to canonical ``92…`` digits.

    Returns None when the input carries nothing phone-like. Normalization
    rules: strip non-digits; collapse ``0092…`` → ``92…``; collapse a
    leading ``0`` mobile (``03…``) → ``92…``; bare 10-digit ``3…`` mobiles
    gain the ``92`` prefix. Landlines/foreign formats keep their digits and
    are still comparable — identity equality only ever happens between two
    identical normalized strings.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        return None
    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92") and len(digits) >= 11:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("3"):
        digits = "92" + digits
    return digits or None


def _identity_of(post: dict) -> dict:
    """Extract the (public-post-only) identity evidence from one post row."""
    return {
        "phone": normalize_phone(post.get("contact_published")),
        "name": None,  # post schema carries no name field; filled by collectors when public
        "handle": (post.get("author_handle") or None),
        "agency_id": None,  # no agency id exists in the post schema today
    }


def _merge_key(post: dict, identity: dict) -> str:
    """Compute the merge key for one buyer post.

    Precedence: identical normalized phone (strong key) > anonymous demand
    cluster (city + town + budget band). Handles and names NEVER form a
    merge key — they are recorded as evidence only (SHR-9).
    """
    if identity["phone"]:
        return _KEY_PHONE + identity["phone"]
    town = (post.get("area_tag") or "unknown").strip().lower()
    city = (post.get("city_tag") or "unknown").strip().lower()
    # Town-level demand pool: anonymous wanted posts in the same town share
    # one demand signal; their budgets widen the pool's min/max. Budget does
    # not split the pool — identity-less posts cannot be proven to be
    # different buyers, and a seller wants the whole town's demand at once.
    return _KEY_DEMAND + f"{city}|{town}"


# ---------------------------------------------------------------------------
# derive
# ---------------------------------------------------------------------------


def _one(cursor) -> dict | None:
    """Fetch one row as a dict — works with or without row_factory."""
    row = cursor.fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return dict(zip([d[0] for d in cursor.description], row))


def _all(cursor) -> list[dict]:
    """Fetch all rows as dicts — works with or without row_factory."""
    cols = [d[0] for d in cursor.description]
    out = []
    for r in cursor.fetchall():
        out.append(dict(r) if hasattr(r, "keys") else dict(zip(cols, r)))
    return out


def _json_array(text: str | None) -> list:
    try:
        return json.loads(text) if text else []
    except (json.JSONDecodeError, TypeError):
        return []


def _town_entry(town: str | None, city: str | None) -> dict:
    return {"town": town, "city": city, "evidence_count": 1}


def _merge_towns(existing: list, new_towns: list) -> list:
    """Merge town lists, bumping evidence_count for repeats."""
    merged = {(t.get("town"), t.get("city")): dict(t) for t in existing}
    for t in new_towns:
        key = (t.get("town"), t.get("city"))
        if key in merged:
            merged[key]["evidence_count"] += t.get("evidence_count", 1)
        else:
            merged[key] = dict(t)
    return list(merged.values())


def _upsert_buyer(conn, post: dict, identity: dict, merge_key: str,
                  seen_at: str) -> None:
    """Insert a new buyer or fold one more post into the existing buyer."""
    row = _one(conn.execute("SELECT * FROM buyer WHERE merge_key=?", (merge_key,)))

    towns = [_town_entry(post.get("area_tag"), post.get("city_tag"))]
    budget = post.get("budget_pkr")
    ptype = post.get("vertical") or post.get("intent_primary")
    platform = post.get("platform")
    has_identity = any(identity.get(k) for k in ("phone", "name", "handle"))

    if row is None:
        buyer_id = ("buyer-" + uuid.uuid4().hex[:12]) if has_identity else \
            ("demand-" + uuid.uuid4().hex[:12])
        conn.execute(
            "INSERT INTO buyer (buyer_id, merge_key, identities, "
            "towns_of_interest, budget_min_pkr, budget_max_pkr, property_types, "
            "activity_count, evidence_post_ids, first_seen, last_seen, "
            "source_platforms, anonymous, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (buyer_id, merge_key, json.dumps([identity]),
             json.dumps(towns), budget, budget,
             json.dumps([ptype] if ptype else []),
             1, json.dumps([post.get("post_id")]),
             seen_at, seen_at,
             json.dumps([platform] if platform else []),
             0 if has_identity else 1, seen_at, seen_at),
        )
        log.debug("buyer_registry: new buyer %s key=%s", buyer_id, merge_key)
        return

    # fold into existing
    buyer_id = row["buyer_id"]
    evidence = _json_array(row["evidence_post_ids"])
    if post.get("post_id") and post["post_id"] in evidence:
        # idempotent: this post was already folded in — refresh last_seen only
        conn.execute("UPDATE buyer SET last_seen=?, updated_at=? WHERE buyer_id=?",
                     (seen_at, _utcnow_iso(), buyer_id))
        return
    identities = _json_array(row["identities"])
    if identity not in identities:
        identities.append(identity)
    prev_towns = _json_array(row["towns_of_interest"])
    towns = _merge_towns(prev_towns, towns)
    ptypes = _json_array(row["property_types"])
    if ptype and ptype not in ptypes:
        ptypes.append(ptype)
    if post.get("post_id") and post["post_id"] not in evidence:
        evidence.append(post["post_id"])
    platforms = _json_array(row["source_platforms"])
    if platform and platform not in platforms:
        platforms.append(platform)
    budgets = [b for b in (row["budget_min_pkr"], row["budget_max_pkr"], budget)
               if b]
    anonymous = 0 if (row["anonymous"] == 0 or has_identity) else 1
    conn.execute(
        "UPDATE buyer SET identities=?, towns_of_interest=?, "
        "budget_min_pkr=?, budget_max_pkr=?, property_types=?, "
        "activity_count=activity_count+1, evidence_post_ids=?, "
        "last_seen=?, source_platforms=?, anonymous=?, updated_at=? "
        "WHERE buyer_id=?",
        (json.dumps(identities), json.dumps(towns),
         min(budgets) if budgets else None, max(budgets) if budgets else None,
         json.dumps(ptypes), json.dumps(evidence),
         seen_at, json.dumps(platforms), anonymous, _utcnow_iso(), buyer_id),
    )
    log.debug("buyer_registry: folded post into buyer %s", buyer_id)


def derive_buyers(conn) -> list[dict]:
    """Scan buyer-lane posts and build/update the buyer registry.

    Reads every ``post`` with ``intent_primary IN ('property_wanted',
    'seeking_product')``, computes each post's merge key (phone first,
    anonymous demand cluster otherwise), and inserts or folds the buyer.
    Handles/names are recorded as evidence — they never trigger a merge.

    Idempotent: re-running on the same posts only bumps activity/towns/
    budgets/last_seen. Returns the buyer records (dicts) it created or
    touched, in no guaranteed order.

    Intended to run after each scoring pass, before the operator views
    matches. No outreach happens here or anywhere else in this module.
    """
    ensure_buyer_schema(conn)
    cur = conn.execute(
        "SELECT post_id, platform, author_handle, city_tag, area_tag, "
        "intent_primary, budget_pkr, contact_published, url, seen_at "
        "FROM post WHERE intent_primary IN (?, ?)",
        BUYER_INTENTS,
    )
    touched: list[dict] = []
    for post in _all(cur):
        identity = _identity_of(post)
        key = _merge_key(post, identity)
        seen_at = post.get("seen_at") or _utcnow_iso()
        _upsert_buyer(conn, post, identity, key, seen_at)
        touched.append({"merge_key": key, "post_id": post.get("post_id")})
    conn.commit()
    log.info("buyer_registry: derive_buyers touched %d buyer-lane posts",
             len(touched))
    return touched


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def _recency_points(last_seen: str | None) -> tuple[int, str | None]:
    """Recency credit for match ranking: ≤7d → 10, ≤30d → 5, else 0."""
    if not last_seen:
        return 0, None
    try:
        seen = datetime.datetime.fromisoformat(last_seen)
    except ValueError:
        return 0, None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=datetime.timezone.utc)
    age_days = max(0.0, (datetime.datetime.now(datetime.timezone.utc) - seen)
                   .total_seconds() / 86400.0)
    if age_days <= 7:
        return 10, f"active recently (last seen {int(age_days)}d ago)"
    if age_days <= 30:
        return 5, f"seen within last month ({int(age_days)}d ago)"
    return 0, None


def match_buyers(conn, seller_lead: dict, limit: int = 10) -> list[dict]:
    """Rank registered buyers against a seller lead.

    ``seller_lead`` keys: ``town``, ``city``, ``price_pkr``, ``property_type``.
    Score (0–100):
        - town match: 40 (same town), 10 (same city only), 0 otherwise;
        - budget compatibility: 25 when ``budget_max >= price >=
          budget_min*0.7``; 10 when price is merely near the band (within
          1.5x of budget_max or 0.5x below budget_min); 0 when no budget
          evidence at all (stated explicitly in reasons);
        - recency of last buyer activity: 10 / 5 / 0;
        - buyer activity depth: up to 5 (one point per distinct buyer post,
          capped).

    Returns at most ``limit`` dicts with buyer record fields plus
    ``match_score`` and ``match_reasons``. Anonymous demand signals can
    match; their reasons cite town + budget only — never an identity.
    Purely read-only; registry only, no outreach.
    """
    ensure_buyer_schema(conn)
    town = (seller_lead.get("town") or "").strip().lower()
    city = (seller_lead.get("city") or "").strip().lower()
    price = seller_lead.get("price_pkr")

    scored = []
    for buyer in _all(conn.execute("SELECT * FROM buyer")):
        reasons: list[str] = []
        score = 0

        # --- town ---
        buyer_towns = _json_array(buyer["towns_of_interest"])
        buyer_town_set = {str(t.get("town") or "").strip().lower()
                          for t in buyer_towns}
        buyer_city_set = {str(t.get("city") or "").strip().lower()
                          for t in buyer_towns}
        if town and town in buyer_town_set:
            score += 40
            reasons.append(f"town match: buyer is looking in {seller_lead.get('town')}")
        elif city and city in buyer_city_set:
            score += 10
            reasons.append(f"city match (same city, different town): "
                           f"{seller_lead.get('city')}")
        else:
            continue  # no geographic overlap at all — not a candidate

        # --- budget ---
        bmin, bmax = buyer["budget_min_pkr"], buyer["budget_max_pkr"]
        if price and bmax:
            if bmax >= price >= (bmin or 0) * 0.7:
                score += 25
                reasons.append(
                    f"budget compatible: buyer range PKR {bmin or 0:,}–{bmax:,} "
                    f"vs asking PKR {price:,}")
            elif price <= bmax * 1.5 and price >= (bmin or 0) * 0.5:
                score += 10
                reasons.append(
                    f"budget near-fit: buyer range PKR {bmin or 0:,}–{bmax:,} "
                    f"vs asking PKR {price:,}")
            else:
                reasons.append(
                    f"budget mismatch: buyer range PKR {bmin or 0:,}–{bmax:,} "
                    f"vs asking PKR {price:,}")
        else:
            reasons.append("no stated buyer budget to compare — town match only")

        # --- recency ---
        rpts, rreason = _recency_points(buyer.get("last_seen"))
        score += rpts
        if rreason:
            reasons.append(rreason)

        # --- activity ---
        act = min(int(buyer.get("activity_count") or 0), 5)
        score += act
        if act:
            reasons.append(f"{buyer['activity_count']} buyer post(s) on record")

        scored.append({
            "buyer_id": buyer["buyer_id"],
            "anonymous": bool(buyer["anonymous"]),
            "merge_key": buyer["merge_key"],
            "towns_of_interest": buyer_towns,
            "budget_min_pkr": bmin,
            "budget_max_pkr": bmax,
            "property_types": _json_array(buyer["property_types"]),
            "activity_count": buyer["activity_count"],
            "last_seen": buyer["last_seen"],
            "source_platforms": _json_array(buyer["source_platforms"]),
            "identities": _json_array(buyer["identities"]),
            "match_score": score,
            "match_reasons": reasons,
        })

    scored.sort(key=lambda m: (-m["match_score"], m["buyer_id"]))
    return scored[:max(0, limit)]


# ---------------------------------------------------------------------------
# API/dashboard helpers (stable signatures for Wave-B 29/30)
# ---------------------------------------------------------------------------


def _row_to_buyer(row) -> dict:
    """Convert a raw buyer row to the public buyer record dict."""
    b = dict(row)
    return {
        "buyer_id": b["buyer_id"],
        "identities": _json_array(b["identities"]),
        "merge_key": b["merge_key"],
        "towns_of_interest": _json_array(b["towns_of_interest"]),
        "budget_min_pkr": b["budget_min_pkr"],
        "budget_max_pkr": b["budget_max_pkr"],
        "property_types": _json_array(b["property_types"]),
        "activity_count": b["activity_count"],
        "first_seen": b["first_seen"],
        "last_seen": b["last_seen"],
        "source_platforms": _json_array(b["source_platforms"]),
        "anonymous": bool(b["anonymous"]),
    }


def list_buyers(conn, limit: int = 100, anonymous_only: bool = False) -> list[dict]:
    """List buyer records, most-recent first. Read-only."""
    ensure_buyer_schema(conn)
    q = "SELECT * FROM buyer"
    if anonymous_only:
        q += " WHERE anonymous=1"
    q += " ORDER BY last_seen DESC LIMIT ?"
    return [_row_to_buyer(r) for r in _all(conn.execute(q, (limit,)))]


def buyer_summary(conn, buyer_id: str) -> dict | None:
    """One buyer record plus its evidence posts for the dashboard.

    Returns None when the buyer_id is unknown. Read-only.
    """
    ensure_buyer_schema(conn)
    row = _one(conn.execute("SELECT * FROM buyer WHERE buyer_id=?", (buyer_id,)))
    if not row:
        return None
    buyer = _row_to_buyer(row)
    post_ids = _json_array(row["evidence_post_ids"])
    posts = []
    if post_ids:
        posts = _all(conn.execute(
            "SELECT post_id, platform, author_handle, text_original, url, "
            "city_tag, area_tag, intent_primary, budget_pkr, seen_at "
            "FROM post WHERE post_id IN (%s)" % ",".join("?" * len(post_ids)),
            post_ids))
    buyer["evidence_posts"] = posts
    return buyer


def get_buyers_for_lead(conn, lead_id: str, limit: int = 10) -> list[dict]:
    """Rank candidate buyers for a merged seller lead.

    Derives the seller context from the lead's merged posts: town = first
    non-empty ``area_tag`` (falling back to any), city = first non-empty
    ``city_tag``, price = the highest ``budget_pkr`` across its posts,
    property_type = the lead's ``vertical``. Returns the same ranked shape
    as ``match_buyers``; empty list when the lead is unknown or has no
    seller posts with usable fields. Read-only.
    """
    ensure_buyer_schema(conn)
    lead = _one(conn.execute("SELECT * FROM lead WHERE lead_id=?", (lead_id,)))
    if not lead:
        return []
    post_ids = _json_array(lead["merged_post_ids"])
    if not post_ids:
        return []
    posts = _all(conn.execute(
        "SELECT post_id, platform, city_tag, area_tag, intent_primary, "
        "budget_pkr FROM post WHERE post_id IN (%s)"
        % ",".join("?" * len(post_ids)),
        post_ids)) if post_ids else []
    towns = [p["area_tag"] for p in posts if p.get("area_tag")]
    cities = [p["city_tag"] for p in posts if p.get("city_tag")]
    budgets = [p["budget_pkr"] for p in posts if p.get("budget_pkr")]
    seller = {
        "town": towns[0] if towns else None,
        "city": cities[0] if cities else None,
        "price_pkr": max(budgets) if budgets else None,
        "property_type": lead["vertical"],
    }
    if not seller["town"] and not seller["city"]:
        return []
    return match_buyers(conn, seller, limit=limit)
