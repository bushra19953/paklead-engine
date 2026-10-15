"""
PakLead Engine dashboard (Streamlit).

Lead table with score breakdowns, filters, loophole view, quarantine
queue, and run history. Read-only except operator status actions (which
require the API token via the API service).

    streamlit run dashboard.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover
    raise SystemExit("streamlit is not installed — run: pip install -r requirements.txt")

from config import Settings  # noqa: E402
from models import connect, init_db, jload  # noqa: E402

settings = Settings()
DB = settings.DB_PATH


def _db():
    conn = connect(DB)
    init_db(conn)
    return conn


st.set_page_config(page_title="PakLead Engine", layout="wide")
st.title("PakLead Engine — Pakistan Lead Intelligence")

tab_leads, tab_loops, tab_quar, tab_runs = st.tabs(
    ["Leads", "Loopholes", "Quarantine", "Runs & Audit"])

with tab_leads:
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        city = st.text_input("City", "")
    with col2:
        intent = st.text_input("Intent", "")
    with col3:
        min_score = st.number_input("Min score", 0, 100, 0)
    with col4:
        hot_only = st.checkbox("Hot only", False)
    conn = _db()
    q = ("SELECT l.lead_id, l.intent, l.city, l.vertical, l.status, l.first_seen,"
         " s.total, s.hot_flag, s.intent_strength, s.recency, s.budget,"
         " s.urgency, s.cross_platform, s.contactability"
         " FROM lead l LEFT JOIN lead_score s ON l.lead_id = s.lead_id WHERE 1=1")
    params: list = []
    if city:
        q += " AND l.city LIKE ?"; params.append(f"%{city}%")
    if intent:
        q += " AND l.intent LIKE ?"; params.append(f"%{intent}%")
    if min_score:
        q += " AND COALESCE(s.total,0) >= ?"; params.append(min_score)
    if hot_only:
        q += " AND s.hot_flag = 1"
    q += " ORDER BY COALESCE(s.total,0) DESC LIMIT 200"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    st.caption(f"{len(rows)} leads")
    for lead in rows:
        label = (f"🔥 {lead['lead_id']} — {lead['intent']} — {lead['city']}"
                 if lead["hot_flag"] else
                 f"{lead['lead_id']} — {lead['intent']} — {lead['city']}")
        with st.expander(f"{label} (score {lead['total'] or 0}, {lead['status']})"):
            st.write("**Score breakdown:**",
                     {k: lead[k] for k in
                      ("intent_strength", "recency", "budget", "urgency",
                       "cross_platform", "contactability") if lead[k] is not None})
            mids = jload(lead["merged_post_ids"]) if "merged_post_ids" in lead else []
            conn2 = _db()
            posts = [dict(p) for p in conn2.execute(
                "SELECT platform, author_handle, text_original, url, budget_pkr,"
                " contact_published FROM post WHERE post_id IN (%s)" %
                ",".join("?" * len(mids)), mids).fetchall()] if mids else []
            for p in posts:
                st.write(f"- [{p['platform']}] {p['text_original'][:220]}")
                st.caption(f"{p['url']} | budget {p['budget_pkr']} | "
                           f"contact {'yes' if p['contact_published'] else 'no'}")
            conn2.close()
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
