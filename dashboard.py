"""
PakLead Engine dashboard (Streamlit).

Lead table with score breakdowns, filters, loophole view, quarantine
queue, run history, lead-detail max-information view, and a bulk-import
view. Read-only except operator actions (import, status changes) which
act through this dashboard's own DB writes.

IMPORTANT (classifier parity, 2026-10-15): this dashboard performs NO
classification or scoring of its own. Every intent/score it displays is
read verbatim from the engine's tables (``post.intent_primary``,
``lead_score.*``) or produced by calling the ENGINE's own functions
(``engine.classifier.classify_post`` for imports). The property-intent
correction (``engine/classifier.py::_property_intent_fix``) therefore
applies everywhere automatically; there is no dashboard-side copy of
those rules to drift. A static parity check in the fleet verification
asserts this file contains no scoring/classification implementation.

    streamlit run dashboard.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover
    raise SystemExit("streamlit is not installed — run: pip install -r requirements.txt")

from config import Settings, load_phrase_bank, load_scoring_config  # noqa: E402
from models import audit, connect, init_db, jdump, jload, new_id, utcnow_iso  # noqa: E402
from engine import buyer_registry as BR  # noqa: E402
from engine import classifier as C  # noqa: E402
from engine import price_history as PH  # noqa: E402
from engine import yield_monitor as YM  # noqa: E402

settings = Settings()
DB = settings.DB_PATH


def _db() -> sqlite3.Connection:
    conn = connect(DB)
    init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Sample detection ("hide samples" toggle).
#
# Demo/seeding runs label their rows in three overlapping ways, matching how
# the pipeline and sample_data tag them (see pipeline.py demo seeding and
# sample_data/sample_posts.json): a "demo:" post_id, a source_id containing
# "sample"/"demo", or an author handle starting with "sample.".
# ---------------------------------------------------------------------------

_SAMPLE_SOURCE_RE = re.compile(r"(demo|sample)", re.I)
_SAMPLE_HANDLE_RE = re.compile(r"^sample[._-]", re.I)


def is_sample_post(post: dict) -> bool:
    """True when a post row carries any demo/sample label."""
    return (
        str(post.get("post_id", "")).startswith("demo:")
        or bool(_SAMPLE_SOURCE_RE.search(str(post.get("source_id") or "")))
        or bool(_SAMPLE_HANDLE_RE.search(str(post.get("author_handle") or "")))
        or "/sample/" in str(post.get("url") or "")
    )


def is_sample_lead(conn: sqlite3.Connection, lead_id: str) -> bool:
    """A lead counts as sample when NONE of its merged posts is a real post."""
    row = conn.execute("SELECT merged_post_ids FROM lead WHERE lead_id=?",
                       (lead_id,)).fetchone()
    if not row:
        return True
    mids = jload(row["merged_post_ids"])
    if not mids:
        return True
    posts = _posts_by_ids(conn, mids)
    return not any(not is_sample_post(p) for p in posts)


# ---------------------------------------------------------------------------
# Lead listing + max-information lead-detail sections (B30).
# ---------------------------------------------------------------------------

_LEAD_LIST_SQL = (
    "SELECT l.lead_id, l.merged_post_ids, l.intent, l.city, l.vertical,"
    " l.status, l.first_seen, l.expires_at, l.cluster_explanation,"
    " l.user_confirmed_identity, l.touch_log, l.operator_notes,"
    " s.computed_at, s.scoring_config_version, s.intent_strength, s.recency,"
    " s.budget, s.urgency, s.cross_platform, s.contactability, s.total,"
    " s.hot_flag, s.hot_trigger, s.score_history"
    " FROM lead l LEFT JOIN lead_score s ON l.lead_id = s.lead_id WHERE 1=1"
)


def query_leads(conn: sqlite3.Connection, city: str = "", intent: str = "",
                min_score: int = 0, hot_only: bool = False,
                hide_samples: bool = True, limit: int = 200) -> list[dict]:
    """Filtered lead list (newest-first by score)."""
    q, params = _LEAD_LIST_SQL, []
    if city:
        q += " AND l.city LIKE ?"; params.append(f"%{city}%")
    if intent:
        q += " AND l.intent LIKE ?"; params.append(f"%{intent}%")
    if min_score:
        q += " AND COALESCE(s.total,0) >= ?"; params.append(min_score)
    if hot_only:
        q += " AND s.hot_flag = 1"
    q += " ORDER BY COALESCE(s.total,0) DESC LIMIT ?"; params.append(limit * 2)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if hide_samples:
        rows = [r for r in rows if not is_sample_lead(conn, r["lead_id"])]
    return rows[:limit]


def _posts_by_ids(conn: sqlite3.Connection, post_ids: list[str]) -> list[dict]:
    if not post_ids:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM post WHERE post_id IN (%s) ORDER BY seen_at" %
        ",".join("?" * len(post_ids)), post_ids).fetchall()]


def _comments_for(conn: sqlite3.Connection, post_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT comment_id, author_handle, text, published_at,"
        " is_provider_reply, intent_primary, contact_published, seen_at"
        " FROM comment WHERE post_id=? ORDER BY seen_at", (post_id,)).fetchall()]


def breakdown_lines(score: dict | None, cfg: dict) -> list[dict]:
    """Line-by-line score breakdown with config-derived ceilings/explanations.

    Pure formatting of the engine's stored ``lead_score`` row — never a
    re-computation. Each line carries the stored value plus the config
    constant that bounds it, so "why is this lead 78?" is never a mystery
    (TRD NFR-PK-6).
    """
    if not score:
        return []
    def line(component, value, ceiling, note):
        return {"component": component, "value": value or 0,
                "ceiling": ceiling, "note": note}
    return [
        line("intent_strength", score["intent_strength"],
             f"category_weight × intent_multiplier ({cfg.get('intent_multiplier')})"
             + (f" + inquiry bonus {cfg.get('price_inquiry_bonus')}"
                if score.get("price_inquiry") else ""),
             "category weight from phrase bank × config multiplier"),
        line("recency", score["recency"], cfg.get("recency_max"),
             f"full points within {cfg.get('recency_full_hours')}h,"
             " linear decay to 0 at the intent freshness window"),
        line("budget", score["budget"],
             f"parsed {cfg.get('budget_parsed')} / phrase-only {cfg.get('budget_phrase_only')}",
             "parsed PKR amount vs phrase-only mention"),
        line("urgency", score["urgency"],
             f"{cfg.get('urgency_per_marker')} per marker, cap {cfg.get('urgency_cap')}",
             "urgency phrases from the phrase bank"),
        line("cross_platform", score["cross_platform"],
             f"2 platforms {cfg.get('cross_platform_two')},"
             f" 3+ platforms {cfg.get('cross_platform_three_plus')}",
             "distinct platforms across merged posts"),
        line("contactability", score["contactability"],
             f"phone {cfg.get('contact_phone_published')} / open-thread"
             f" {cfg.get('contact_open_thread')} / inbox {cfg.get('contact_inbox_gate')}",
             "public phone, open comment thread, or inbox-gated contact"),
        line("TOTAL", score["total"], f"hot ≥ {cfg.get('hot_threshold')}",
             f"hot_flag={bool(score['hot_flag'])}"
             + (f" · trigger: {score['hot_trigger']}" if score.get("hot_trigger") else "")),
    ]


def lead_detail_sections(conn: sqlite3.Connection, lead_id: str,
                         cfg: dict | None = None) -> dict:
    """Everything the DB holds for one lead, in section dicts.

    Render-equivalent entry point for tests: ``render_lead_detail()``
    below is a thin Streamlit pass over this structure. No field we hold
    is hidden — the renderer expands every section by default.
    """
    cfg = cfg or load_scoring_config()
    lead = conn.execute("SELECT * FROM lead WHERE lead_id=?",
                        (lead_id,)).fetchone()
    if not lead:
        return {"lead_id": lead_id, "missing": True}
    lead = dict(lead)
    lead["merged_post_ids"] = jload(lead["merged_post_ids"])
    lead["touch_log"] = jload(lead["touch_log"])

    score_row = conn.execute("SELECT * FROM lead_score WHERE lead_id=?",
                             (lead_id,)).fetchone()
    score = dict(score_row) if score_row else None
    if score:
        score["score_history"] = jload(score["score_history"])

    posts = _posts_by_ids(conn, lead["merged_post_ids"])
    for p in posts:
        p["urgency_markers"] = jload(p["urgency_markers"])
        p["comments"] = _comments_for(conn, p["post_id"])
        p["is_sample"] = is_sample_post(p)

    # Price history + drop evidence per merged listing (B20 signal).
    price: dict = {}
    for p in posts:
        key = p["post_id"]  # platform:platform_post_id — stable listing key
        snaps = [dict(r) for r in conn.execute(
            "SELECT price_pkr, seen_at FROM price_snapshots"
            " WHERE listing_key=? ORDER BY seen_at", (key,)).fetchall()] \
            if _table_exists(conn, "price_snapshots") else []
        price[key] = {
            "snapshots": snaps,
            "first_seen_price": PH.first_seen_price(conn, key),
            "drop": PH.price_drop(conn, key),
            "current_price": p.get("budget_pkr"),
        }

    # Matched buyers with reasons (buyer registry, read-only).
    buyers = BR.get_buyers_for_lead(conn, lead_id, limit=10)

    # Yield/health per source of the merged posts.
    source_ids = sorted({p["source_id"] for p in posts if p.get("source_id")})
    health = _source_health(conn, source_ids)

    # Author profiles (public fields only — SHR-2/9).
    handles = {(p["platform"], p["author_handle"]) for p in posts
               if p.get("author_handle")}
    for p in posts:
        for c in p["comments"]:
            if c.get("author_handle"):
                handles.add((p["platform"], c["author_handle"]))
    authors = []
    for plat, handle in sorted(handles):
        row = conn.execute(
            "SELECT platform, handle, name_public, profile_url, first_seen,"
            " suppressed FROM author_profile WHERE platform=? AND handle=?",
            (plat, handle)).fetchone()
        authors.append(dict(row) if row else
                       {"platform": plat, "handle": handle,
                        "name_public": None, "profile_url": None,
                        "first_seen": None, "suppressed": 0,
                        "note": "no author_profile row"})

    # Recent audit trail touching this lead (append-only, SHR-13).
    audit_rows = [dict(r) for r in conn.execute(
        "SELECT timestamp, actor, action, target, detail FROM audit_entry"
        " WHERE target=? ORDER BY timestamp DESC LIMIT 50",
        (lead_id,)).fetchall()]

    return {
        "lead_id": lead_id,
        "lead": lead,
        "score": score,
        "breakdown": breakdown_lines(score, cfg),
        "scoring_config_version": (score or {}).get("scoring_config_version"),
        "posts": posts,
        "price_history": price,
        "buyers": buyers,
        "source_health": health,
        "authors": authors,
        "audit": audit_rows,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def _source_health(conn: sqlite3.Connection,
                   source_ids: list[str]) -> list[dict]:
    """Source row + yield-monitor summary (last run + 7-day totals)."""
    try:
        summ = YM.summary(conn)["sources"]
    except Exception:
        summ = []
    by_src = {s["source_id"]: s for s in summ}
    out = []
    for sid in source_ids:
        row = conn.execute("SELECT source_id, platform, access_route,"
                           " display_name, archetype, city_scope, watermark,"
                           " boundary_status, boundary_note, enabled"
                           " FROM source WHERE source_id=?", (sid,)).fetchone()
        info = dict(row) if row else {"source_id": sid, "note": "no source row"}
        info["city_scope"] = jload(info.get("city_scope"))
        info["yield"] = by_src.get(sid)
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Sync/import view (B30).
#
# Standalone import path (independent of api.py's token-gated
# /sync/bulk-upsert): implements the import directly against the same
# idempotent key the pipeline uses:
#     post_id = f"{platform}:{platform_post_id}"   (pipeline.py::_ingest_record)
# Rows already present are left untouched (INSERT OR IGNORE, idempotent
# re-runs per SHR-15); comments are inserted with INSERT OR IGNORE as well.
#
# Payload contract (JSON):
#   {"posts": [{"platform": "olx", "platform_post_id": "ad123",
#               "text_original": "...", "url": "...",
#               "author_handle": "...", "published_at": "...",
#               "source_id": "...", "comments": [
#                 {"platform_comment_id": "c1", "author_handle": "...",
#                  "text": "...", "published_at": "..."}]}]}
# Imported posts are classified with the ENGINE classifier
# (classify_post + classify_comment) so stored intent/budget/contact
# fields stay engine-consistent. Price snapshots are recorded for posts
# with a parsed price (B20 signal). Leads are NOT clustered here — that
# happens in the pipeline; imported posts enter the normal flow.
# ---------------------------------------------------------------------------

_REQUIRED_POST_FIELDS = ("platform", "platform_post_id",
                         "text_original", "url")


def parse_import_payload(raw: str) -> tuple[dict | None, str | None]:
    """Parse + structural-validate the JSON payload. Returns (payload, err)."""
    try:
        payload = json.loads(raw)
    except Exception as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(payload, dict) or not isinstance(
            payload.get("posts"), list):
        return None, 'payload must be an object with a "posts" array'
    for i, rec in enumerate(payload["posts"]):
        if not isinstance(rec, dict):
            return None, f"posts[{i}] is not an object"
        missing = [f for f in _REQUIRED_POST_FIELDS
                   if not str(rec.get(f) or "").strip()]
        if missing:
            return None, (f"posts[{i}] missing required fields: "
                          f"{', '.join(missing)}")
    return payload, None


def _import_bank():
    return load_phrase_bank()


def preview_import(conn: sqlite3.Connection, payload: dict,
                   bank: dict | None = None) -> dict:
    """Dry-run preview: counts by platform/intent + new-vs-existing keys.

    Pure read of the payload (+ key-existence checks); never writes.
    """
    bank = bank or _import_bank()
    posts = payload["posts"]
    by_platform: dict[str, int] = {}
    by_intent: dict[str, int] = {}
    quarantined = 0
    new_keys: list[str] = []
    existing_keys: list[str] = []
    invalid: list[int] = []
    for i, rec in enumerate(posts):
        plat = str(rec.get("platform", ""))
        by_platform[plat] = by_platform.get(plat, 0) + 1
        post_id = f"{plat}:{rec.get('platform_post_id')}"
        exists = bool(conn.execute("SELECT 1 FROM post WHERE post_id=?",
                                   (post_id,)).fetchone())
        (existing_keys if exists else new_keys).append(post_id)
        try:
            cls = C.classify_post(str(rec.get("text_original", "")), bank,
                                  platform=plat)
        except Exception:
            invalid.append(i)
            continue
        intent = cls["intent_primary"]
        by_intent[intent] = by_intent.get(intent, 0) + 1
        if cls["scam_suspect"] or cls["language_mode"] == "undetected" \
                or cls["intent_primary"] == "complaint_pain":
            quarantined += 1
    return {
        "total_posts": len(posts),
        "by_platform": by_platform,
        "by_intent": by_intent,
        "new_keys": new_keys,
        "existing_keys": existing_keys,
        "quarantined_estimate": quarantined,
        "invalid_indexes": invalid,
    }


def apply_import(conn: sqlite3.Connection, payload: dict,
                 bank: dict | None = None,
                 actor: str = "dashboard") -> dict:
    """Apply the import idempotently. Writes to ``conn`` (caller-owned).

    Mirrors ``pipeline._ingest_record``: same post_id key, same
    classification call, same quarantine rules. Never overwrites an
    existing post row (idempotent re-run, SHR-15); comments insert with
    INSERT OR IGNORE. Returns a result summary.
    """
    bank = bank or _import_bank()
    seen = utcnow_iso()
    # Auto-provision any source_ids the payload references: post.source_id
    # has a foreign key, and an undeclared source must not silently drop
    # posts (INSERT OR IGNORE would swallow the FK failure).
    for rec in payload["posts"]:
        sid = rec.get("source_id")
        if sid and not conn.execute("SELECT 1 FROM source WHERE source_id=?",
                                    (sid,)).fetchone():
            conn.execute(
                "INSERT OR IGNORE INTO source (source_id, platform,"
                " access_route, display_name, city_scope, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, rec["platform"], "import", sid, "[]", seen))
    inserted, already_present, comments_added = 0, 0, 0
    inserted_ids: list[str] = []

    for rec in payload["posts"]:
        plat = str(rec["platform"])
        post_id = f"{plat}:{rec['platform_post_id']}"
        if conn.execute("SELECT 1 FROM post WHERE post_id=?",
                        (post_id,)).fetchone():
            already_present += 1
        else:
            text = str(rec["text_original"])
            cls = C.classify_post(
                text, bank,
                source_cities=_source_cities(conn, rec.get("source_id")),
                has_comments=bool(rec.get("comments")),
                platform=plat)
            quarantined, quarantine_reason = 0, None
            if cls["scam_suspect"]:
                quarantined, quarantine_reason = 1, "scam_red_flag"
            elif cls["intent_primary"] == "complaint_pain":
                quarantined, quarantine_reason = 1, "complaint_review"
            elif cls["language_mode"] == "undetected":
                quarantined, quarantine_reason = 1, "undetected_language"
            conn.execute(
                "INSERT OR IGNORE INTO post (post_id, platform, source_id,"
                " author_handle, text_original, text_normalized, language_mode,"
                " city_tag, area_tag, intent_primary, intent_secondary,"
                " price_inquiry, budget_pkr, budget_qualifier, urgency_markers,"
                " contact_published, contact_type, url, published_at, seen_at,"
                " quarantined, quarantine_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (post_id, plat, rec.get("source_id"), rec.get("author_handle"),
                 text, cls["text_normalized"], cls["language_mode"],
                 cls["city_tag"], cls["area_tag"], cls["intent_primary"],
                 cls["intent_secondary"], int(cls["price_inquiry"]),
                 cls["budget_pkr"], cls["budget_qualifier"],
                 jdump(cls["urgency_markers"]), cls["contact_published"],
                 cls["contact_type"], str(rec["url"]),
                 rec.get("published_at"), seen, quarantined,
                 quarantine_reason))
            inserted += 1
            inserted_ids.append(post_id)
            if cls["budget_pkr"]:
                PH.snapshot(conn, post_id, cls["budget_pkr"], seen)
        for c in rec.get("comments") or []:
            cid = f"{plat}:{c.get('platform_comment_id', '')}"
            cc = C.classify_comment(str(c.get("text", "")), bank)
            cur = conn.execute(
                "INSERT OR IGNORE INTO comment (comment_id, post_id,"
                " author_handle, text, published_at, is_provider_reply,"
                " seen_at, intent_primary, contact_published)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, post_id, c.get("author_handle"), c.get("text", ""),
                 c.get("published_at"), int(cc["is_provider_reply"]),
                 seen, cc["intent_primary"], cc["contact_published"]))
            comments_added += cur.rowcount

    audit(conn, actor, "import_bulk_upsert", None,
          {"inserted": inserted, "already_present": already_present,
           "comments_added": comments_added})
    conn.commit()
    return {
        "inserted": inserted,
        "already_present": already_present,
        "comments_added": comments_added,
        "inserted_ids": inserted_ids,
    }


def _source_cities(conn: sqlite3.Connection,
                   source_id: str | None) -> list[str]:
    if not source_id:
        return []
    row = conn.execute("SELECT city_scope FROM source WHERE source_id=?",
                       (source_id,)).fetchone()
    return jload(row["city_scope"]) if row else []


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def _lead_label(lead: dict) -> str:
    fire = "🔥 " if lead.get("hot_flag") else ""
    return (f"{fire}{lead['lead_id']} — {lead.get('intent')} —"
            f" {lead.get('city')} (score {lead.get('total') or 0},"
            f" {lead.get('status')})")


def render_lead_detail(sections: dict) -> None:
    """Max-information lead view. Every section expanded by default."""
    if sections.get("missing"):
        st.warning(f"Lead {sections['lead_id']} not found.")
        return
    lead, score = sections["lead"], sections["score"]

    st.header(f"{'🔥 ' if lead.get('hot_flag') else ''}"
              f"Lead {lead['lead_id']}")
    with st.expander("Lead record — all fields", expanded=True):
        st.json({
            "lead_id": lead["lead_id"], "intent": lead["intent"],
            "city": lead["city"], "vertical": lead["vertical"],
            "status": lead["status"], "first_seen": lead["first_seen"],
            "expires_at": lead["expires_at"],
            "cluster_explanation": lead["cluster_explanation"],
            "user_confirmed_identity": bool(lead["user_confirmed_identity"]),
            "merged_post_ids": lead["merged_post_ids"],
            "operator_notes": lead["operator_notes"],
            "touch_log": lead["touch_log"],
        })
    with st.expander("Score breakdown — line by line", expanded=True):
        if score:
            st.caption(f"config version {score['scoring_config_version']} ·"
                       f" computed {score['computed_at']}")
            st.table(sections["breakdown"])
            if score["score_history"]:
                st.write("**Score history:**", score["score_history"])
        else:
            st.info("No lead_score row for this lead.")
    with st.expander(f"Merged posts ({len(sections['posts'])}) — full text",
                     expanded=True):
        for p in sections["posts"]:
            st.markdown(f"**[{p['platform']}] {p['post_id']}**"
                        f"{' — sample' if p['is_sample'] else ''}")
            st.write(p["text_original"])
            st.caption(f"author {p['author_handle']} · url {p['url']}"
                       f" · published {p['published_at']}"
                       f" · seen {p['seen_at']}"
                       f" · lang {p['language_mode']} · city {p['city_tag']}"
                       f" · area {p['area_tag']}")
            st.write(f"intent **{p['intent_primary']}**"
                     f" (secondary: {p['intent_secondary']}) ·"
                     f" price_inquiry={bool(p['price_inquiry'])} ·"
                     f" budget {p['budget_pkr']} ({p['budget_qualifier']}) ·"
                     f" urgency {p['urgency_markers']} ·"
                     f" contact {p['contact_type']}={p['contact_published']}"
                     f" · deleted={bool(p['is_deleted'])}"
                     f" · quarantined={bool(p['quarantined'])}"
                     f" ({p['quarantine_reason']})")
            if p["text_normalized"]:
                st.caption(f"normalized: {p['text_normalized'][:400]}")
            for c in p["comments"]:
                st.write(f"↳ 💬 {c['author_handle']}: {c['text']}")
                st.caption(f"{c['comment_id']} · {c['published_at']} ·"
                           f" intent {c['intent_primary']} · provider_reply="
                           f"{bool(c['is_provider_reply'])} · contact"
                           f" {c['contact_published']}")
    with st.expander("Price history + drop evidence", expanded=True):
        for pid, ph in sections["price_history"].items():
            st.markdown(f"**{pid}** — current price in post:"
                        f" {ph['current_price']}"
                        f" · first-seen price: {ph['first_seen_price']}")
            if ph["snapshots"]:
                st.table(ph["snapshots"])
            else:
                st.caption("no snapshots recorded")
            if ph["drop"]:
                st.success(f"📉 PRICE DROP: {ph['drop']}")
            else:
                st.caption("no drop detected (window: 30d, ≥10%)")
    with st.expander(f"Matched buyers ({len(sections['buyers'])})",
                     expanded=True):
        if not sections["buyers"]:
            st.caption("No buyer matches for this seller lead.")
        for b in sections["buyers"]:
            st.markdown(f"**{b['buyer_id']}** — match"
                        f" {b['match_score']}/100")
            for reason in b.get("match_reasons", []):
                st.write(f"- {reason}")
            st.caption(f"towns {b.get('towns_of_interest')} · budget"
                       f" {b.get('budget_min_pkr')}–{b.get('budget_max_pkr')}"
                       f" · types {b.get('property_types')} · activity"
                       f" {b.get('activity_count')} · last_seen"
                       f" {b.get('last_seen')} · platforms"
                       f" {b.get('source_platforms')}"
                       f" · anonymous={bool(b.get('anonymous'))}")
    with st.expander("Source yield / health per source", expanded=True):
        for s in sections["source_health"]:
            y = s.get("yield") or {}
            st.markdown(f"**{s.get('display_name') or s['source_id']}**"
                        f" [{s.get('platform')}]")
            st.write(f"route {s.get('access_route')} · archetype"
                     f" {s.get('archetype')} · boundary"
                     f" {s.get('boundary_status')}"
                     f"{' — ' + s['boundary_note'] if s.get('boundary_note') else ''}"
                     f" · watermark {s.get('watermark')} · enabled="
                     f"{bool(s.get('enabled'))} · cities {s.get('city_scope')}")
            if y:
                st.write(f"last run {y['last_run_at']}: fetched"
                         f" {y['last_fetched']} / stored {y['last_stored']}"
                         f" ({y['last_status']}) · 7d: fetched"
                         f" {y['fetched_7d']} / stored {y['stored_7d']} over"
                         f" {y['runs_7d']} runs")
            else:
                st.caption("no yield-monitor data for this source")
    with st.expander(f"Authors ({len(sections['authors'])})", expanded=True):
        st.table(sections["authors"])
    with st.expander(f"Audit trail ({len(sections['audit'])})", expanded=True):
        for a in sections["audit"]:
            st.write(f"- {a['timestamp']} **{a['actor']}** {a['action']}"
                     f" ({a['detail']})")


def render_import_view(conn: sqlite3.Connection) -> None:
    """Sync/import: paste or upload a JSON payload, preview, then apply."""
    st.header("Bulk import — leads from JSON payload")
    st.caption("Idempotent key: (platform, platform_post_id) →"
               " post_id, mirroring pipeline._ingest_record. Rows already"
               " present are left untouched (SHR-15). Imported posts are"
               " classified with the engine classifier; leads are clustered"
               " later by the pipeline.")
    uploaded = st.file_uploader("Upload JSON payload file", type=["json"])
    pasted = st.text_area("…or paste the payload JSON", height=160)
    raw = (uploaded.read().decode("utf-8") if uploaded else None) or pasted
    hide_samples = st.checkbox("hide samples", True,
                               help="Exclude demo/sample rows from the"
                                    " preview counts")
    if not raw:
        st.info("Provide a payload to preview the import.")
        return
    payload, err = parse_import_payload(raw)
    if err:
        st.error(err)
        return
    bank = _import_bank()
    preview = preview_import(conn, payload, bank)
    st.subheader("Preview (dry run — nothing written)")
    st.write(f"**{preview['total_posts']}** posts ·"
             f" **{len(preview['new_keys'])}** new ·"
             f" **{len(preview['existing_keys'])}** already present ·"
             f" ~**{preview['quarantined_estimate']}** would quarantine")
    st.write("by platform:", preview["by_platform"])
    st.write("by intent (engine classification):", preview["by_intent"])
    if preview["invalid_indexes"]:
        st.warning(f"classification failed for posts"
                   f" {preview['invalid_indexes']}")
    st.json({"new_keys": preview["new_keys"][:50],
             "existing_keys": preview["existing_keys"][:50]})
    if st.button("Apply import (writes to DB)"):
        result = apply_import(conn, payload, bank)
        st.success(f"imported {result['inserted']} posts,"
                   f" {result['already_present']} already present,"
                   f" {result['comments_added']} comments")
        st.write("inserted ids:", result["inserted_ids"])


def main() -> None:
    st.set_page_config(page_title="PakLead Engine", layout="wide")
    st.title("PakLead Engine — Pakistan Lead Intelligence")

    tab_leads, tab_detail, tab_import, tab_loops, tab_quar, tab_runs = st.tabs(
        ["Leads", "Lead detail", "Import", "Loopholes", "Quarantine",
         "Runs & Audit"])

    with tab_leads:
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            city = st.text_input("City", "")
        with col2:
            intent = st.text_input("Intent", "")
        with col3:
            min_score = st.number_input("Min score", 0, 100, 0)
        with col4:
            hot_only = st.checkbox("Hot only", False)
        with col5:
            hide_samples = st.checkbox("Hide samples", True)
        conn = _db()
        rows = query_leads(conn, city, intent, min_score, hot_only,
                           hide_samples)
        st.caption(f"{len(rows)} leads")
        st.table([{"lead_id": r["lead_id"], "intent": r["intent"],
                   "city": r["city"], "vertical": r["vertical"],
                   "status": r["status"], "total": r["total"],
                   "hot": bool(r["hot_flag"]),
                   "first_seen": r["first_seen"]} for r in rows])
        conn.close()

    with tab_detail:
        conn = _db()
        rows = query_leads(conn, hide_samples=True)
        conn.close()
        if not rows:
            st.info("No leads to show.")
        else:
            picked = st.selectbox("Lead", [_lead_label(r) for r in rows])
            lead_id = picked.split(" — ")[0].lstrip("🔥 ").strip()
            conn = _db()
            sections = lead_detail_sections(conn, lead_id)
            conn.close()
            render_lead_detail(sections)

    with tab_import:
        conn = _db()
        render_import_view(conn)
        conn.close()

    with tab_loops:
        conn = _db()
        loops = [dict(r) for r in conn.execute(
            "SELECT * FROM loophole_report WHERE status='active'"
            " ORDER BY ratio DESC").fetchall()]
        conn.close()
        if not loops:
            st.info("No active loopholes in the current window.")
        for lp in loops:
            st.subheader(f"{lp['report_type']} — {lp['city']} ({lp['vertical']})")
            st.write(f"seekers {lp['seeker_count']} / providers {lp['provider_count']} "
                     f"→ ratio {lp['ratio']}")
            if lp["complaint_themes"]:
                st.write("themes:", json.loads(lp["complaint_themes"]))
            if lp["price_gap_pct"]:
                st.write(f"price gap: {lp['price_gap_pct']}%")
            st.caption(f"window {lp['window_start']} → {lp['window_end']} · id {lp['report_id']}")

    with tab_quar:
        conn = _db()
        rows = [dict(r) for r in conn.execute(
            "SELECT post_id, platform, author_handle, text_original, quarantine_reason,"
            " url FROM post WHERE quarantined=1 ORDER BY seen_at DESC LIMIT 100"
            ).fetchall()]
        conn.close()
        st.caption(f"{len(rows)} quarantined posts awaiting operator review")
        for r in rows:
            st.write(f"- **{r['quarantine_reason']}** [{r['platform']}] {r['text_original'][:200]}")
            st.caption(r["url"])

    with tab_runs:
        conn = _db()
        runs = [dict(r) for r in conn.execute(
            "SELECT run_id, platform, source_id, access_route_used, started_at,"
            " items_fetched, items_stored, rate_limit_events, status"
            " FROM collection_run ORDER BY started_at DESC LIMIT 30").fetchall()]
        conn.close()
        st.table(runs)


if __name__ == "__main__":
    main()
