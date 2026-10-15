"""
Source health table + run-history summaries (B31, Wave B).

Reads from the ``source`` table (models.py — schema NOT altered here) and
the ``source_yield`` table built by :mod:`engine.yield_monitor` (B19).
This module adds no new tables: it defines one read-only SQL VIEW
(``source_health``) joining each enabled source with its most recent
yield row, and derives per-source health classification on top of the
same zero-yield / thin-market semantics yield_monitor already uses.

Integration contract
--------------------
1. On startup (or first use) call :func:`ensure_health_view` with the
   open connection. It creates the ``source_health`` VIEW idempotently
   (``CREATE VIEW IF NOT EXISTS``) and ensures the ``source_yield``
   table exists via ``yield_monitor.ensure_yield_table``. It does not
   touch any existing tables or rows.
2. Wire ``GET /sources/health`` (dashboard API) to::

       engine.source_health.health_report(conn)

   and return the dict as JSON. It is read-only and cheap (one query
   per source; sources number in the low hundreds).
3. In the daily digest (``run_daily``), call
   ``engine.source_health.stale_sources(conn)`` and render each
   returned dict as a warnings-section entry ("no successful run in the
   last 26 h — cron may have missed this source"). For silent
   zero-yield alarms, prefer ``yield_monitor.check_alarms(conn)``; the
   two are complementary: stale_sources catches *missed runs*,
   check_alarms catches *quiet runs*.
4. For a per-source run log (API detail pane or dashboard drill-down),
   call ``run_history(conn, source_id, limit=...)``.

Health states per enabled source
--------------------------------
* ``never_run`` — no row in ``source_yield`` yet (source configured
  but never collected). Runs-24h is 0, streak is 0.
* ``healthy`` — last run status ``ok`` and no quiet streak.
* ``quiet`` — 1-2 consecutive zero-fetched ``ok`` runs (below the
  alarm threshold of 3; watch-listed, not alarmed).
* ``zero_yield_alarm`` — the last 3 runs all have ``fetched == 0``
  with ``status == "ok"`` AND the source has yielded records before.
  Same definition as ``yield_monitor.check_alarms``.
* ``thin_market`` — the same 3-run quiet streak, but the source has
  NEVER yielded anything in its recorded history. Needs human review,
  not an alarm (thin market, or a source that should be disabled).
* ``failing`` — last recorded run did not have status ``ok``
  (``failed``, ``partial``, ``aborted_*`` ...).

stdlib only (sqlite3, logging, json, datetime). Never touches the live
DB except through the caller-supplied connection.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3

from engine import yield_monitor

log = logging.getLogger(__name__)

#: Consecutive zero-fetched ok-runs that promote "quiet" to an alarm
#: (mirrors yield_monitor.check_alarms' lookback_runs default).
ZERO_YIELD_LOOKBACK = 3

#: Window for the "runs_24h" count.
RECENT_RUNS_HOURS = 24

#: Default staleness window (hours) for stale_sources(): slightly more
#: than a day so a normally-once-daily cron is not flagged as missed.
STALE_AFTER_HOURS = 26

#: Run statuses counted as "successful" for staleness purposes. A run
#: that failed or was aborted is not a successful collection.
SUCCESS_STATUSES = ("ok", "partial")

HEALTH_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS source_health AS
SELECT
    s.source_id        AS source_id,
    s.platform         AS platform,
    s.city_scope       AS city_scope,
    s.access_route     AS access_route,
    s.boundary_status  AS boundary_status,
    s.enabled          AS enabled,
    y.fetched          AS last_fetched,
    y.stored           AS last_stored,
    y.status           AS last_status,
    y.error_class      AS last_error_class,
    y.ran_at           AS last_run_at
FROM source s
LEFT JOIN source_yield y
  ON y.source_id = s.source_id
 AND y.rowid = (
        SELECT y2.rowid FROM source_yield y2
        WHERE y2.source_id = s.source_id
        ORDER BY y2.ran_at DESC, y2.rowid DESC LIMIT 1
    );
"""


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (matches models.utcnow_iso)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def ensure_health_view(conn: sqlite3.Connection) -> None:
    """Create the read-only ``source_health`` VIEW if missing.

    Idempotent migration: ``CREATE VIEW IF NOT EXISTS`` plus an ensure
    of the ``source_yield`` table via :func:`engine.yield_monitor.
    ensure_yield_table`. Creates no tables of its own and alters no
    existing schema (models.py untouched).

    Args:
        conn: open SQLite connection (the module never opens its own).
    """
    yield_monitor.ensure_yield_table(conn)
    conn.execute(HEALTH_VIEW_SQL)
    conn.commit()


def _source_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All sources ordered by platform then source_id."""
    return conn.execute(
        "SELECT source_id, platform, city_scope, access_route,"
        " boundary_status, enabled FROM source"
        " ORDER BY platform, source_id"
    ).fetchall()


def _parse_city_scope(raw: str | None) -> list:
    """Parse the JSON ``city_scope`` column; tolerate garbage."""
    if not raw:
        return []
    try:
        scope = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("unparseable city_scope for a source: %r", raw)
        return []
    return scope if isinstance(scope, list) else [scope]


def _zero_yield_streak(conn: sqlite3.Connection, source_id: str,
                       lookback: int | None = None) -> int:
    """Consecutive newest-first runs with ``fetched == 0`` and ``status == "ok"``.

    The streak stops at the first run that fetched anything or whose
    status was not "ok" (failures/aborts are surfaced as ``failing``,
    not as zero-yield). Only recent runs are scanned; the scan stops
    once the streak breaks, so it is bounded by the streak length.
    """
    limit = lookback or ZERO_YIELD_LOOKBACK
    # Scan a few more than the threshold so we can report a streak
    # length beyond the alarm threshold (capped for safety).
    rows = conn.execute(
        "SELECT fetched, status FROM source_yield WHERE source_id=?"
        " ORDER BY ran_at DESC, rowid DESC LIMIT ?",
        (source_id, limit + 20),
    ).fetchall()
    streak = 0
    for row in rows:
        if row["fetched"] == 0 and row["status"] == "ok":
            streak += 1
        else:
            break
    return streak


def _ever_yielded(conn: sqlite3.Connection, source_id: str) -> bool:
    """True if the source ever fetched at least one record."""
    row = conn.execute(
        "SELECT 1 FROM source_yield WHERE source_id=? AND fetched > 0 LIMIT 1",
        (source_id,),
    ).fetchone()
    return row is not None


def _last_run(conn: sqlite3.Connection, source_id: str) -> dict | None:
    """Newest source_yield row for a source, or None when never run."""
    row = conn.execute(
        "SELECT fetched, stored, status, error_class, ran_at FROM source_yield"
        " WHERE source_id=? ORDER BY ran_at DESC, rowid DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "fetched": row["fetched"],
        "stored": row["stored"],
        "status": row["status"],
        "error_class": row["error_class"],
        "ran_at": row["ran_at"],
    }


def _runs_24h(conn: sqlite3.Connection, source_id: str) -> int:
    """Number of recorded runs for the source in the last 24 hours."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=RECENT_RUNS_HOURS)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) n FROM source_yield WHERE source_id=? AND ran_at >= ?",
        (source_id, cutoff),
    ).fetchone()
    return int(row["n"])


def _classify(last: dict | None, streak: int, ever: bool) -> str:
    """Derive the health state for one source.

    Args:
        last: newest yield row (None when the source never ran).
        streak: consecutive zero-fetched ok-runs (newest first).
        ever: whether the source ever fetched anything.
    """
    if last is None:
        return "never_run"
    if last["status"] not in ("ok",):
        return "failing"
    if streak >= ZERO_YIELD_LOOKBACK:
        return "zero_yield_alarm" if ever else "thin_market"
    if streak > 0:
        return "quiet"
    return "healthy"


def health_report(conn: sqlite3.Connection) -> dict:
    """Per-enabled-source health summary for ``GET /sources/health``.

    Builds on ``yield_monitor``'s ``source_yield`` table (no duplicate
    storage). Each source entry carries: ``source_id``, ``platform``,
    ``city_scope`` (parsed JSON list), ``access_route``, ``last_run``
    (``ran_at``/``fetched``/``stored``/``status``/``error_class`` or
    ``None`` when never run), ``runs_24h`` count,
    ``zero_yield_streak``, ``thin_market`` flag, ``boundary_status``,
    ``enabled`` (bool) and the derived ``health`` state (one of
    ``never_run`` / ``healthy`` / ``quiet`` / ``zero_yield_alarm`` /
    ``thin_market`` / ``failing``).

    Sources that never ran report ``health == "never_run"`` with
    ``runs_24h == 0`` and ``zero_yield_streak == 0``.

    Args:
        conn: open SQLite connection.

    Returns:
        ``{"generated_at": ..., "sources": [ {...}, ... ]}``.
    """
    ensure_health_view(conn)
    out: list[dict] = []
    for src in _source_rows(conn):
        source_id = src["source_id"]
        if not src["enabled"]:
            continue
        last = _last_run(conn, source_id)
        streak = _zero_yield_streak(conn, source_id) if last else 0
        ever = _ever_yielded(conn, source_id)
        thin_market = (streak >= ZERO_YIELD_LOOKBACK) and not ever
        out.append({
            "source_id": source_id,
            "platform": src["platform"],
            "city_scope": _parse_city_scope(src["city_scope"]),
            "access_route": src["access_route"],
            "last_run": last,
            "runs_24h": _runs_24h(conn, source_id) if last else 0,
            "zero_yield_streak": streak,
            "thin_market": thin_market,
            "boundary_status": src["boundary_status"],
            "enabled": bool(src["enabled"]),
            "health": _classify(last, streak, ever),
        })
    return {"generated_at": utcnow_iso(), "sources": out}


def run_history(conn: sqlite3.Connection, source_id: str,
                limit: int = 20) -> list[dict]:
    """Recent yield rows for one source, newest first (API/drill-down).

    Each entry carries ``run_id``, ``fetched``, ``stored``, ``status``,
    ``error_class`` and ``ran_at``. Unknown source ids return an empty
    list (no error — the dashboard calls this with source ids from
    ``health_report``).

    Args:
        conn: open SQLite connection.
        source_id: the ``source.source_id`` to inspect.
        limit: maximum rows to return (default 20).
    """
    yield_monitor.ensure_yield_table(conn)
    rows = conn.execute(
        "SELECT run_id, fetched, stored, status, error_class, ran_at"
        " FROM source_yield WHERE source_id=?"
        " ORDER BY ran_at DESC, rowid DESC LIMIT ?",
        (source_id, max(int(limit or 0), 0)),
    ).fetchall()
    return [{
        "run_id": r["run_id"],
        "fetched": r["fetched"],
        "stored": r["stored"],
        "status": r["status"],
        "error_class": r["error_class"],
        "ran_at": r["ran_at"],
    } for r in rows]


def stale_sources(conn: sqlite3.Connection,
                  hours: float = STALE_AFTER_HOURS) -> list[dict]:
    """Enabled sources with no successful run in the window (cron-miss detector).

    A source is stale when it has NO ``source_yield`` row with
    ``ran_at >= now - hours`` and status in ``SUCCESS_STATUSES``
    (``ok``/``partial``) — this covers sources whose cron runs never
    fired, died before recording, or failed outright, as well as
    configured-but-never-run sources.

    Feeds the digest warnings section. Silent zero-yield runs are NOT
    stale — those belong to ``yield_monitor.check_alarms``.

    Args:
        conn: open SQLite connection.
        hours: staleness window (default 26 — just over a day, so a
            normally-once-daily cron is not flagged).

    Returns:
        List of dicts with keys: ``source_id``, ``platform``,
        ``last_successful_run`` (``ran_at`` string or ``None`` for
        never), and a human ``message``.
    """
    ensure_health_view(conn)
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
    placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
    stale: list[dict] = []
    for src in _source_rows(conn):
        source_id = src["source_id"]
        if not src["enabled"]:
            continue
        ok = conn.execute(
            f"SELECT 1 FROM source_yield WHERE source_id=? AND status IN"
            f" ({placeholders}) AND ran_at >= ? LIMIT 1",
            (source_id, *SUCCESS_STATUSES, cutoff),
        ).fetchone()
        if ok:
            continue
        last_ok = conn.execute(
            f"SELECT ran_at FROM source_yield WHERE source_id=? AND status IN"
            f" ({placeholders}) ORDER BY ran_at DESC, rowid DESC LIMIT 1",
            (source_id, *SUCCESS_STATUSES),
        ).fetchone()
        last_ok_at = last_ok["ran_at"] if last_ok else None
        if last_ok_at is None:
            detail = "never had a successful run"
        else:
            detail = f"last successful run at {last_ok_at}"
        stale.append({
            "source_id": source_id,
            "platform": src["platform"],
            "last_successful_run": last_ok_at,
            "message": (f"{source_id}: no successful run in the last"
                        f" {hours:g} h ({detail}) — cron may have missed"
                        " this source."),
        })
    return stale
