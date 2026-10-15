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
from models import audit, connect, init_db, jload, utcnow_iso  # noqa: E402

settings = Settings()
app = FastAPI(title="PakLead Engine API", version="1.0.0")


def _conn():
    conn = connect(settings.DB_PATH)
    init_db(conn)
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
