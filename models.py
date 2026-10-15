"""
SQLite schema for the PakLead Engine — the 10 TRD data models (§2.1–2.10).

stdlib only (sqlite3). Money in whole PKR. Timestamps UTC ISO-8601.
Lists / JSON fields are stored as JSON text columns.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    source_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    access_route TEXT NOT NULL,
    display_name TEXT NOT NULL,
    archetype TEXT,
    city_scope TEXT,            -- JSON array
    rate_budget_per_min INTEGER DEFAULT 30,
    enabled INTEGER DEFAULT 1,
    boundary_status TEXT DEFAULT 'allowed',
    boundary_note TEXT,
    watermark TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post (
    post_id TEXT PRIMARY KEY,   -- platform:platform_post_id
    platform TEXT NOT NULL,
    source_id TEXT REFERENCES source(source_id),
    author_handle TEXT,
    text_original TEXT NOT NULL,
    text_normalized TEXT,
    language_mode TEXT,         -- en|roman_urdu|urdu|mixed|undetected
    city_tag TEXT,
    area_tag TEXT,
    intent_primary TEXT,
    intent_secondary TEXT,
    price_inquiry INTEGER DEFAULT 0,
    budget_pkr INTEGER,
    budget_qualifier TEXT,      -- demand|final|budget|per_marla
    urgency_markers TEXT,       -- JSON array
    contact_published TEXT,     -- phone/WhatsApp ONLY if in the public post
    contact_type TEXT,          -- phone|whatsapp|inbox|none
    url TEXT NOT NULL UNIQUE,   -- dedupe key (SHR-15)
    published_at TEXT,
    seen_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0,
    quarantined INTEGER DEFAULT 0,
    quarantine_reason TEXT
);

CREATE TABLE IF NOT EXISTS comment (
    comment_id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES post(post_id),
    author_handle TEXT,
    text TEXT,
    published_at TEXT,
    is_provider_reply INTEGER DEFAULT 0,
    seen_at TEXT NOT NULL,
    intent_primary TEXT,
    contact_published TEXT
);

CREATE TABLE IF NOT EXISTS author_profile (
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    name_public TEXT,
    profile_url TEXT,
    first_seen TEXT NOT NULL,
    suppressed INTEGER DEFAULT 0,
    PRIMARY KEY (platform, handle)
    -- Forbidden: real-world identity, non-published contact, friend graphs (SHR-2/9)
);

CREATE TABLE IF NOT EXISTS lead (
    lead_id TEXT PRIMARY KEY,
    merged_post_ids TEXT NOT NULL,   -- JSON array of post FKs
    intent TEXT NOT NULL,
    city TEXT,
    vertical TEXT,
    status TEXT DEFAULT 'new',       -- new|hot|contacted|dead|expired
    first_seen TEXT NOT NULL,
    expires_at TEXT,
    cluster_explanation TEXT,
    user_confirmed_identity INTEGER DEFAULT 0,
    touch_log TEXT DEFAULT '[]',     -- JSON array of {at, note}
    operator_notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lead_score (
    lead_id TEXT PRIMARY KEY REFERENCES lead(lead_id),
    computed_at TEXT NOT NULL,
    scoring_config_version TEXT NOT NULL,
    intent_strength INTEGER DEFAULT 0,
    recency INTEGER DEFAULT 0,
    budget INTEGER DEFAULT 0,
    urgency INTEGER DEFAULT 0,
    cross_platform INTEGER DEFAULT 0,
    contactability INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    hot_flag INTEGER DEFAULT 0,
    hot_trigger TEXT,
    score_history TEXT DEFAULT '[]'  -- JSON array
);

CREATE TABLE IF NOT EXISTS loophole_report (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,  -- ratio_gap|underserved_city|complaint_theme|price_gap
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    vertical TEXT,
    city TEXT,
    seeker_count INTEGER DEFAULT 0,
    provider_count INTEGER DEFAULT 0,
    ratio REAL,
    complaint_themes TEXT,      -- JSON {theme: count}
    price_gap_pct REAL,
    evidence_post_ids TEXT,     -- JSON array
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL     -- immutable once issued
);

CREATE TABLE IF NOT EXISTS collection_run (
    run_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_id TEXT REFERENCES source(source_id),
    access_route_used TEXT NOT NULL,  -- FR-PK-20: which legitimate route produced each item
    started_at TEXT NOT NULL,
    ended_at TEXT,
    items_fetched INTEGER DEFAULT 0,
    items_stored INTEGER DEFAULT 0,
    items_purged INTEGER DEFAULT 0,
    rate_limit_events INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok',        -- ok|partial|aborted_rate_limit|aborted_boundary|failed
    error_class TEXT,
    kill_switch_state INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suppression_entry (
    entry_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    target TEXT NOT NULL,      -- author handle or post id/URL
    reason TEXT NOT NULL,      -- opt_out|content_removed|operator_decision
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_entry (
    entry_id TEXT PRIMARY KEY, -- append-only
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL,       -- scheduler|collector|scorer|operator|system
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT DEFAULT '{}'   -- JSON, immutable
);
"""


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Migration: comment columns added for comment classification (TRD FR-PK-42).
    # Upgrade existing databases cleanly — CREATE TABLE IF NOT EXISTS alone
    # would leave old DBs without these columns.
    existing = {r[1] for r in
                conn.execute("PRAGMA table_info(comment)").fetchall()}
    for col in ("intent_primary", "contact_published"):
        if col not in existing:
            conn.execute(f"ALTER TABLE comment ADD COLUMN {col} TEXT")
    conn.commit()


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str | None = None,
          detail: dict | None = None) -> None:
    """Append an immutable audit entry (SHR-13)."""
    conn.execute(
        "INSERT INTO audit_entry (entry_id, timestamp, actor, action, target, detail)"
        " VALUES (?,?,?,?,?,?)",
        (new_id("audit"), utcnow_iso(), actor, action, target,
         json.dumps(detail or {}, ensure_ascii=False)),
    )
    conn.commit()


def jdump(value) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def jload(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return json.loads(value)
