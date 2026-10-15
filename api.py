"""
Dashboard API (TRD §1.9, FR-PK-69…73).

Read endpoints are public to the operator's deployment; write endpoints
(operator actions only) require the DASHBOARD_API_TOKEN bearer token when
set. There is deliberately NO endpoint that can message, comment, or
contact any author (SHR-14) — outreach stays human, on-platform.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fastapi import Depends, FastAPI, Header, HTTPException, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

from config import Settings  # noqa: E402
from models import audit, connect, init_db, jdump, jload, new_id, utcnow_iso  # noqa: E402
from engine import buyer_registry, price_history, yield_monitor  # noqa: E402

settings = Settings()
app = FastAPI(title="PakLead Engine API", version="1.0.0")

#: Per-platform detail keys accepted in ``lead_detail`` (B29). Matches the
#: enrichers' merge contracts: graana defines DETAIL_KEY="graana_detail";
#: the olx/zameen detail payloads live under the same-shaped keys.
DETAIL_KEYS = ("olx_detail", "zameen_detail", "graana_detail")

#: Post columns the bulk-upsert path writes (whitelist — nothing else).
POST_COLS = ("post_id", "platform", "source_id", "author_handle",
             "text_original", "text_normalized", "language_mode", "city_tag",
             "area_tag", "intent_primary", "intent_secondary", "price_inquiry",
             "budget_pkr", "budget_qualifier", "urgency_markers",
             "contact_published", "contact_type", "url", "published_at",
             "seen_at", "is_deleted", "quarantined", "quarantine_reason")

#: Lead columns the bulk-upsert path writes (whitelist — nothing else).
LEAD_COLS = ("lead_id", "merged_post_ids", "intent", "city", "vertical",
             "status", "first_seen", "expires_at", "cluster_explanation",
             "user_confirmed_identity", "touch_log", "operator_notes",
             "hidden")

#: Lead-score columns the bulk-upsert path writes (whitelist).
SCORE_COLS = ("lead_id", "computed_at", "scoring_config_version",
              "intent_strength", "recency", "budget", "urgency",
              "cross_platform", "contactability", "total", "hot_flag",
              "hot_trigger", "score_history")

_JSON_COLS = {"urgency_markers", "merged_post_ids", "touch_log",
              "score_history"}

#: Sample leads are identified by their labeled fixture authors/URLs from
#: sample_data/sample_posts.json (author_handle "sample.*", "/sample/"
#: URLs). hide_samples flags exactly these — never live data.
_SAMPLE_AUTHOR_LIKE = "sample%"
_SAMPLE_URL_LIKE = "%/sample/%"


def _ensure_api_tables(conn):
    """B29 schema additions. Idempotent; additive only — no row is ever
    modified here.

    * ``lead.hidden`` — flag for the 12 original labeled sample leads
      (set via POST /sync/bulk-upsert with ``hide_samples=true``; samples
      are flagged, never deleted).
    * ``lead_detail`` — per-platform enriched-detail JSON store keyed
      (lead_id, platform_key), the destination the detail enrichers'
      merge contracts named (coordinator-chosen key, now canonical).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lead)").fetchall()}
    if "hidden" not in cols:
        conn.execute("ALTER TABLE lead ADD COLUMN hidden INTEGER DEFAULT 0")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lead_detail (
               lead_id TEXT NOT NULL,
               platform_key TEXT NOT NULL,  -- olx_detail|zameen_detail|graana_detail
               detail_json TEXT NOT NULL,   -- enriched payload, JSON object
               enriched_at TEXT,
               PRIMARY KEY (lead_id, platform_key)
           )"""
    )
    conn.commit()


def _conn():
    conn = connect(settings.DB_PATH)
    init_db(conn)
    _ensure_api_tables(conn)
    return conn


def _require_token(authorization: str | None = Header(default=None)):
    if settings.DASHBOARD_API_TOKEN:
        expected = f"Bearer {settings.DASHBOARD_API_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid token")


def _lead_row(conn, lead_id: str) -> dict:
    row = conn.execute("SELECT * FROM lead WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="lead not found")
    lead = dict(row)
    score = conn.execute("SELECT * FROM lead_score WHERE lead_id=?",
                         (lead_id,)).fetchone()
    post_ids = jload(lead["merged_post_ids"])
    posts = [dict(p) for p in conn.execute(
        "SELECT post_id, platform, author_handle, text_original, url, city_tag,"
        " area_tag, intent_primary, budget_pkr, quarantined, published_at"
        " FROM post WHERE post_id IN (%s)" % ",".join("?" * len(post_ids)),
        post_ids).fetchall()] if post_ids else []
    return {
        "lead": {k: (jload(v) if k in ("merged_post_ids", "touch_log") else v)
                 for k, v in lead.items()},
        "score": dict(score) if score else None,
        "posts": posts,
    }


@app.get("/leads")
def list_leads(intent: str | None = None, city: str | None = None,
               vertical: str | None = None, status: str | None = None,
               min_score: int = 0, hot_only: bool = False,
               date_from: str | None = None, limit: int = 50,
               offset: int = 0):
    conn = _conn()
    q = ("SELECT l.*, s.total, s.hot_flag FROM lead l LEFT JOIN lead_score s"
         " ON l.lead_id = s.lead_id WHERE 1=1")
    params: list = []
    if intent:
        q += " AND l.intent=?"; params.append(intent)
    if city:
        q += " AND l.city=?"; params.append(city)
    if vertical:
        q += " AND l.vertical=?"; params.append(vertical)
    if status:
        q += " AND l.status=?"; params.append(status)
    if min_score:
        q += " AND COALESCE(s.total,0) >= ?"; params.append(min_score)
    if hot_only:
        q += " AND s.hot_flag=1"
    if date_from:
        q += " AND l.first_seen >= ?"; params.append(date_from)
    q += " ORDER BY COALESCE(s.total,0) DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(q, params).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM lead").fetchone()["c"]
    return {"total_leads": total, "returned": len(rows),
            "leads": [dict(r) for r in rows]}


@app.get("/leads/{lead_id}")
def get_lead(lead_id: str):
    return _lead_row(_conn(), lead_id)


@app.post("/leads/{lead_id}/status")
def set_status(lead_id: str, payload: dict, _=Depends(_require_token)):
    """Operator action: mark a lead contacted/dead/etc. Dead leads stop
    resurfacing and are logged as negative examples (TRD FR-PK-44)."""
    conn = _conn()
    status = payload.get("status")
    if status not in ("new", "hot", "contacted", "dead", "expired"):
        raise HTTPException(status_code=400, detail="bad status")
    note = payload.get("note", "")
    conn.execute("UPDATE lead SET status=? WHERE lead_id=?", (status, lead_id))
    if note:
        row = conn.execute("SELECT touch_log FROM lead WHERE lead_id=?",
                           (lead_id,)).fetchone()
        log_entries = jload(row["touch_log"]) if row else []
        log_entries.append({"at": utcnow_iso(), "note": note})
        conn.execute("UPDATE lead SET touch_log=? WHERE lead_id=?",
                     (json.dumps(log_entries, ensure_ascii=False), lead_id))
    audit(conn, "operator", "lead_status", lead_id,
          {"status": status, "note": note})
    return {"lead_id": lead_id, "status": status}


@app.get("/loopholes")
def list_loopholes(status: str = "active"):
    conn = _conn()
    rows = conn.execute("SELECT * FROM loophole_report WHERE status=?"
                        " ORDER BY ratio DESC", (status,)).fetchall()
    return {"loopholes": [dict(r) for r in rows]}


@app.get("/loopholes/underserved-cities")
def underserved_cities():
    conn = _conn()
    rows = conn.execute("SELECT * FROM loophole_report WHERE status='active'"
                        " AND report_type='underserved_city'").fetchall()
    return {"cities": [dict(r) for r in rows]}


@app.get("/kpis")
def kpis():
    conn = _conn()
    posts = conn.execute("SELECT COUNT(*) c FROM post").fetchone()["c"]
    leads = conn.execute("SELECT COUNT(*) c FROM lead").fetchone()["c"]
    hot = conn.execute("SELECT COUNT(*) c FROM lead WHERE status='hot'").fetchone()["c"]
    contacted = conn.execute("SELECT COUNT(*) c FROM lead WHERE status='contacted'").fetchone()["c"]
    by_source = [dict(r) for r in conn.execute(
        "SELECT platform, COUNT(*) c FROM post GROUP BY platform").fetchall()]
    return {"posts_seen": posts, "leads": leads, "hot": hot,
            "contacted": contacted, "yield_by_platform": by_source}


@app.get("/runs")
def runs(limit: int = 20):
    conn = _conn()
    rows = conn.execute("SELECT * FROM collection_run ORDER BY started_at DESC"
                        " LIMIT ?", (limit,)).fetchall()
    return {"runs": [dict(r) for r in rows]}


@app.get("/audit")
def audit_log(limit: int = 100):
    conn = _conn()
    rows = conn.execute("SELECT * FROM audit_entry ORDER BY timestamp DESC"
                        " LIMIT ?", (limit,)).fetchall()
    return JSONResponse({"audit": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# B29: lead-detail API — maximum information surfacing
# ---------------------------------------------------------------------------

def _full_post_rows(conn, post_ids: list) -> list[dict]:
    """All post columns for the merged posts of a lead (B29: /full never
    truncates to the /leads/{id} subset)."""
    if not post_ids:
        return []
    rows = conn.execute(
        "SELECT * FROM post WHERE post_id IN (%s)"
        % ",".join("?" * len(post_ids)), post_ids).fetchall()
    by_id = {}
    for r in rows:
        d = dict(r)
        for k in _JSON_COLS:
            if k in d and k not in ("merged_post_ids", "touch_log",
                                    "score_history"):
                d[k] = jload(d[k])
        by_id[d["post_id"]] = d
    return [by_id[pid] for pid in post_ids if pid in by_id]


def _price_evidence(conn, post_ids: list) -> dict:
    """Per-post price history + drop evidence (price_history module).
    Posts without any snapshots are absent, never invented."""
    evidence: dict = {}
    for pid in post_ids:
        drop = price_history.price_drop(conn, pid)
        first = price_history.first_seen_price(conn, pid)
        if drop is not None or first is not None:
            entry: dict = {}
            if first is not None:
                entry["first_seen_price_pkr"] = first
            if drop is not None:
                entry["price_drop"] = drop
            evidence[pid] = entry
    return evidence


def _contactability(conn, lead_id: str, score: dict | None,
                    posts: list[dict]) -> dict:
    """Everything held about whether/how this lead can be reached.

    Source of truth is public-post data only (SHR-2/9): published contact
    fields, the scorer's contactability component, and non-provider
    comments (open_thread contactability)."""
    with_contact = [
        {"post_id": p["post_id"], "contact_published": p.get("contact_published"),
         "contact_type": p.get("contact_type")}
        for p in posts if p.get("contact_published")
    ]
    post_ids = [p["post_id"] for p in posts]
    open_thread = 0
    if post_ids:
        open_thread = conn.execute(
            "SELECT COUNT(*) c FROM comment WHERE is_provider_reply=0"
            " AND post_id IN (%s)" % ",".join("?" * len(post_ids)),
            post_ids).fetchone()["c"]
    out = {
        "posts_with_published_contact": with_contact,
        "non_provider_comment_count": open_thread,
    }
    if score is not None and score.get("contactability") is not None:
        out["score_points"] = score["contactability"]
    return out


@app.get("/leads/{lead_id}/full")
def get_lead_full(lead_id: str, buyer_limit: int = 5):
    """B29: the complete lead dossier — one lead with EVERYTHING held:

    * ``lead`` — full lead row (incl. ``hidden`` flag)
    * ``score`` — full lead_score row = the score breakdown
    * ``posts`` — ALL columns of every merged post (not the /leads/{id} subset)
    * ``enriched_detail`` — per-platform enriched detail payloads, keyed
      ``olx_detail`` / ``zameen_detail`` / ``graana_detail`` (absent when
      never enriched)
    * ``price_evidence`` — per-post first-seen price + price-drop evidence
      from the price-history module (absent when no snapshots)
    * ``buyers`` — candidate buyers ranked by the buyer registry
    * ``contactability`` — published contact fields + scorer component +
      open-thread comment count

    Sections with no data are absent keys, never invented values."""
    conn = _conn()
    row = conn.execute("SELECT * FROM lead WHERE lead_id=?",
                       (lead_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="lead not found")
    lead = {k: (jload(v) if k in ("merged_post_ids", "touch_log") else v)
            for k, v in dict(row).items()}
    post_ids = lead["merged_post_ids"] or []
    score_row = conn.execute("SELECT * FROM lead_score WHERE lead_id=?",
                             (lead_id,)).fetchone()
    score = dict(score_row) if score_row else None
    if score is not None and "score_history" in score:
        score["score_history"] = jload(score["score_history"])
    posts = _full_post_rows(conn, post_ids)
    detail_rows = conn.execute(
        "SELECT platform_key, detail_json, enriched_at FROM lead_detail"
        " WHERE lead_id=?", (lead_id,)).fetchall()
    enriched_detail = {r["platform_key"]:
                       {"enriched_at": r["enriched_at"],
                        **json.loads(r["detail_json"])}
                       for r in detail_rows}
    buyers = buyer_registry.get_buyers_for_lead(conn, lead_id,
                                                limit=buyer_limit)
    resp = {
        "lead": lead,
        "posts": posts,
        "contactability": _contactability(conn, lead_id, score, posts),
    }
    if score is not None:
        resp["score"] = score
    if enriched_detail:
        resp["enriched_detail"] = enriched_detail
    price_evidence = _price_evidence(conn, post_ids)
    if price_evidence:
        resp["price_evidence"] = price_evidence
    if buyers:
        resp["buyers"] = buyers
    return resp


@app.get("/buyers")
def list_buyers_endpoint(limit: int = 100, anonymous_only: bool = False):
    """B29: list buyer-registry records, most-recent first. Read-only."""
    return {"buyers": buyer_registry.list_buyers(_conn(), limit=limit,
                                                 anonymous_only=anonymous_only)}


@app.get("/buyers/{buyer_id}")
def get_buyer(buyer_id: str):
    """B29: one buyer-registry record plus its evidence posts."""
    buyer = buyer_registry.buyer_summary(_conn(), buyer_id)
    if buyer is None:
        raise HTTPException(status_code=404, detail="buyer not found")
    return buyer


@app.get("/leads/{lead_id}/buyers")
def get_lead_buyers(lead_id: str, limit: int = 10):
    """B29: candidate buyers ranked for a merged seller lead, derived from
    the lead's posts (town/city/price/vertical) via the buyer registry."""
    conn = _conn()
    if not conn.execute("SELECT 1 FROM lead WHERE lead_id=?",
                        (lead_id,)).fetchone():
        raise HTTPException(status_code=404, detail="lead not found")
    return {"lead_id": lead_id,
            "buyers": buyer_registry.get_buyers_for_lead(conn, lead_id,
                                                         limit=limit)}


@app.get("/sources/health")
def source_health():
    """B29: per-source last-run fetched/stored/status + 7-day totals
    (yield_monitor.summary), enriched with source-table metadata, plus
    zero-yield alarms and thin-market flags (yield_monitor.check_alarms)."""
    conn = _conn()
    summary = yield_monitor.summary(conn)
    meta = {r["source_id"]: dict(r) for r in conn.execute(
        "SELECT source_id, platform, display_name, access_route, enabled,"
        " boundary_status FROM source").fetchall()}
    for s in summary["sources"]:
        m = meta.get(s["source_id"], {})
        for k in ("platform", "display_name", "access_route", "enabled",
                  "boundary_status"):
            s[k] = m.get(k)
    alarms = yield_monitor.check_alarms(conn)
    return {
        "generated_at": summary["generated_at"],
        "sources": summary["sources"],
        "zero_yield_alarms": [a for a in alarms
                              if a["alarm_type"] == "zero_yield_alarm"],
        "thin_market_flags": [a for a in alarms
                              if a["alarm_type"] == "thin_market_candidate"],
    }


def _hide_sample_leads(conn) -> int:
    """Flag the 12 original labeled sample leads as hidden (B29).

    Matches leads whose merged posts are the fixture rows from
    sample_data/sample_posts.json (``sample.*`` author handles and
    ``/sample/`` URLs). Live leads are never matched. Returns how many
    leads were flagged."""
    cur = conn.execute(
        """UPDATE lead SET hidden=1
           WHERE hidden != 1 AND EXISTS (
               SELECT 1 FROM post p
               WHERE lead.merged_post_ids LIKE '%"' || p.post_id || '"%'
                 AND (p.author_handle LIKE ? OR p.url LIKE ?)
           )""", (_SAMPLE_AUTHOR_LIKE, _SAMPLE_URL_LIKE))
    conn.commit()
    return cur.rowcount


def _upsert_bulk_record(conn, rec: dict, now: str) -> dict:
    """Upsert one bulk record (idempotent INSERT OR IGNORE semantics).

    Record keys:
      platform / platform_post_id (or ``post_id`` directly) → post;
      optional ``lead`` (dict → lead row), ``lead_score`` (dict),
      ``detail`` ({"olx_detail": {...}, ...} → lead_detail rows, upserted).
    Returns {"post_inserted", "lead_inserted", "detail_count", "lead_id"}."""
    platform = rec.get("platform")
    platform_post_id = rec.get("platform_post_id")
    post_id = rec.get("post_id")
    if not post_id:
        if not (platform and platform_post_id):
            raise ValueError("record needs post_id or platform+platform_post_id")
        post_id = f"{platform}:{platform_post_id}"
    post_vals = {"post_id": post_id,
                 "url": rec.get("url") or f"dashboard://{post_id}",
                 "text_original": rec.get("text_original") or "",
                 "seen_at": rec.get("seen_at") or now,
                 "platform": platform}
    for k in POST_COLS:
        if k in rec and k not in post_vals:
            v = rec[k]
            if k in _JSON_COLS and isinstance(v, (list, dict)):
                v = jdump(v)
            post_vals[k] = v
    cols = ", ".join(post_vals.keys())
    cur = conn.execute(
        f"INSERT OR IGNORE INTO post ({cols})"
        f" VALUES ({','.join('?' * len(post_vals))})",
        tuple(post_vals.values()))
    post_inserted = cur.rowcount > 0

    if rec.get("budget_pkr"):
        price_history.snapshot(conn, post_id, rec["budget_pkr"],
                               post_vals["seen_at"])

    lead_id = None
    lead_inserted = False
    detail_count = 0
    lead = rec.get("lead")
    if lead:
        lead_id = lead.get("lead_id") or new_id("lead")
        lead_vals = {"lead_id": lead_id, "status": "new",
                     "first_seen": now, "merged_post_ids": jdump([]),
                     "touch_log": jdump([]), "operator_notes": "",
                     "hidden": 0}
        for k in LEAD_COLS:
            if k in lead:
                v = lead[k]
                if k in _JSON_COLS and isinstance(v, (list, dict)):
                    v = jdump(v)
                lead_vals[k] = v
        cols = ", ".join(lead_vals.keys())
        cur = conn.execute(
            f"INSERT OR IGNORE INTO lead ({cols})"
            f" VALUES ({','.join('?' * len(lead_vals))})",
            tuple(lead_vals.values()))
        lead_inserted = cur.rowcount > 0

        score = rec.get("lead_score")
        if score:
            score_vals = {"lead_id": lead_id, "computed_at": now,
                          "scoring_config_version": "bulk-upsert"}
            for k in SCORE_COLS:
                if k in score:
                    v = score[k]
                    if k in _JSON_COLS and isinstance(v, (list, dict)):
                        v = jdump(v)
                    score_vals[k] = v
            cols = ", ".join(score_vals.keys())
            conn.execute(
                f"INSERT OR IGNORE INTO lead_score ({cols})"
                f" VALUES ({','.join('?' * len(score_vals))})",
                tuple(score_vals.values()))

        detail = rec.get("detail") or {}
        for key, payload in detail.items():
            if key not in DETAIL_KEYS:
                raise ValueError(f"unknown detail key {key!r}")
            if not isinstance(payload, dict):
                raise ValueError(f"detail[{key}] must be an object")
            conn.execute(
                "INSERT INTO lead_detail (lead_id, platform_key, detail_json,"
                " enriched_at) VALUES (?,?,?,?)"
                " ON CONFLICT(lead_id, platform_key) DO UPDATE SET"
                " detail_json=excluded.detail_json,"
                " enriched_at=excluded.enriched_at",
                (lead_id, key, json.dumps(payload, ensure_ascii=False), now))
            detail_count += 1

    return {"post_inserted": post_inserted, "lead_inserted": lead_inserted,
            "detail_count": detail_count, "lead_id": lead_id}


@app.post("/sync/bulk-upsert")
def bulk_upsert(payload: dict, _=Depends(_require_token)):
    """B29: dashboard-sync path. Bulk upsert of lead/post records with
    idempotent INSERT OR IGNORE semantics (keyed by post ``post_id`` =
    ``platform:platform_post_id``, or the record's ``url`` when a record
    already uses that as its identity).

    Body: ``{"records": [...], "hide_samples": true|false}``. Each record
    holds post columns plus optional ``lead`` (dict), ``lead_score``
    (dict) and ``detail`` ({"olx_detail"/"zameen_detail"/"graana_detail":
    {...}}). Post prices are snapshotted into price history. Records with
    an unknown detail key or unusable identity are skipped and listed in
    ``errors`` — nothing is ever invented to fill a gap.

    ``hide_samples=true`` flags the 12 original labeled sample leads as
    ``hidden`` (they are never deleted). Operator write: requires the
    DASHBOARD_API_TOKEN bearer token when set."""
    conn = _conn()
    records = payload.get("records") or []
    now = utcnow_iso()
    posts_upserted = posts_skipped = leads_upserted = leads_skipped = 0
    details_upserted = 0
    errors: list = []
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append({"index": idx, "error": "record must be an object"})
            continue
        try:
            res = _upsert_bulk_record(conn, rec, now)
            if res["post_inserted"]:
                posts_upserted += 1
            else:
                posts_skipped += 1
            if rec.get("lead"):
                if res["lead_inserted"]:
                    leads_upserted += 1
                else:
                    leads_skipped += 1
            details_upserted += res["detail_count"]
        except ValueError as exc:
            errors.append({"index": idx, "error": str(exc)})
    conn.commit()

    hidden_count = 0
    if payload.get("hide_samples"):
        hidden_count = _hide_sample_leads(conn)

    audit(conn, "operator", "bulk_upsert", None,
          {"records": len(records), "posts_upserted": posts_upserted,
           "details_upserted": details_upserted,
           "hidden_samples": hidden_count, "errors": len(errors)})
    return {"records_received": len(records), "posts_upserted": posts_upserted,
            "posts_skipped_duplicate": posts_skipped,
            "leads_upserted": leads_upserted, "leads_skipped": leads_skipped,
            "details_upserted": details_upserted,
            "hidden_samples": hidden_count, "errors": errors}
