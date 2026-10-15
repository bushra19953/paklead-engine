"""
Per-source yield monitor + zero-yield alarms (B19, Wave B).

A source that silently returns 0 records run after run is a missed-lead
risk. This module keeps a compact ``source_yield`` table — one row per
source per collection run — and raises alarms when a source that used to
produce records suddenly goes quiet.

Integration contract (for the pipeline and daily digest)
--------------------------------------------------------
1. At startup (or first use), call :func:`ensure_yield_table` with the
   open connection. It is an idempotent migration: ``CREATE TABLE IF NOT
   EXISTS`` plus column backfill for older databases.
2. In ``Pipeline._run_source``, immediately after the existing
   ``UPDATE collection_run ...`` (both the success path and the
   ``except`` failure path), call::

       yield_monitor.record_run(
           self.conn,
           source_id=source["source_id"],
           fetched=result["fetched"],
           stored=len(result["records"]),
           status=result["status"],            # "ok" | "partial" | ...
           error_class=result["error_class"],  # None when healthy
           ran_at=started,                     # ISO-8601 UTC string
       )

   In the ``except`` path, record with ``status="failed"``,
   ``fetched=0``, ``stored=0`` and ``error_class=type(exc).__name__``.
3. In the daily digest (``run_daily``), call
   ``yield_monitor.check_alarms(conn)`` and render each returned dict as
   a warnings-section entry; call ``yield_monitor.summary(conn)`` for the
   per-source health panel on the dashboard.

Alarm rules
-----------
* ``zero_yield_alarm`` — the last ``lookback_runs`` runs (default 3) all
  have ``fetched == 0`` with ``status == "ok"``, AND the source has
  produced records at some point in its history. A healthy-status quiet
  streak on a previously productive source means the collector is
  silently missing data.
* ``thin_market_candidate`` — the same zero quiet-streak, but the source
  has *never* yielded anything in its whole history. These need human
  review, not an alarm (e.g. Zameen Quetta houses — 5 listings total, a
  genuinely thin market). Such sources should be reviewed and either
  annotated in the source table or disabled.
* Runs with ``status != "ok"`` (``partial``, ``failed``,
  ``aborted_rate_limit``, ...) never trigger a zero-yield alarm —
  they are handled as health failures elsewhere.

stdlib only (sqlite3). Never touches the live DB except through the
caller-supplied connection.
"""

from __future__ import annotations

import datetime
import sqlite3
import uuid

#: Sources are flagged as thin-market candidates, never alarmed, when they
#: have never produced a single record in their whole recorded history.
THIN_MARKET_NEVER_YIELDED = True

YIELD_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_yield (
    run_id      TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    fetched     INTEGER DEFAULT 0,
    stored      INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'ok',   -- ok|partial|failed|aborted_*
    error_class TEXT,
    ran_at      TEXT NOT NULL       -- UTC ISO-8601
);
CREATE INDEX IF NOT EXISTS idx_yield_source_ran
    ON source_yield (source_id, ran_at);
"""


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (matches models.utcnow_iso)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """Short unique row id."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def ensure_yield_table(conn: sqlite3.Connection) -> None:
    """Create the ``source_yield`` table if missing (idempotent migration).

    Safe to call on every startup: older databases get the table added
    without touching existing data.
    """
    conn.executescript(YIELD_SCHEMA)
    conn.commit()


def record_run(conn: sqlite3.Connection, source_id: str, fetched: int,
               stored: int, status: str, error_class: str | None = None,
               ran_at: str | None = None) -> str:
    """Append one row per source run to ``source_yield``.

    Args:
        conn: open SQLite connection (the module never opens its own).
        source_id: the ``source.source_id`` the run belongs to.
        fetched: records the adapter fetched from the source.
        stored: records that were actually stored (after dedupe/ingest).
        status: run status (``ok`` / ``partial`` / ``failed`` / ...).
        error_class: exception class name or other error classifier,
            ``None`` when healthy.
        ran_at: UTC ISO-8601 timestamp; defaults to now.

    Returns:
        The generated ``run_id``.
    """
    ensure_yield_table(conn)
    run_id = new_id("yield")
    conn.execute(
        "INSERT INTO source_yield (run_id, source_id, fetched, stored,"
        " status, error_class, ran_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, source_id, int(fetched or 0), int(stored or 0),
         status, error_class, ran_at or utcnow_iso()),
    )
    conn.commit()
    return run_id


def _last_runs(conn: sqlite3.Connection, source_id: str,
               lookback: int) -> list[sqlite3.Row]:
    """Most recent ``lookback`` yield rows for a source, newest first."""
    return conn.execute(
        "SELECT fetched, stored, status, error_class, ran_at FROM source_yield"
        " WHERE source_id=? ORDER BY ran_at DESC, rowid DESC LIMIT ?",
        (source_id, lookback),
    ).fetchall()


def _ever_yielded(conn: sqlite3.Connection, source_id: str) -> bool:
    """True if the source ever fetched at least one record."""
    row = conn.execute(
        "SELECT 1 FROM source_yield WHERE source_id=? AND fetched > 0 LIMIT 1",
        (source_id,),
    ).fetchone()
    return row is not None


def check_alarms(conn: sqlite3.Connection,
                 lookback_runs: int = 3) -> list[dict]:
    """Return alarms for sources whose last N runs silently yielded nothing.

    A source qualifies when its last ``lookback_runs`` rows exist and ALL
    of them have ``fetched == 0`` **and** ``status == "ok"`` (runs that
    failed or were aborted/rate-limited are surfaced as health failures,
    not zero-yield alarms). Sources with fewer than ``lookback_runs``
    recorded runs are skipped — too new to judge.

    The qualifying source is then classified:

    * ``zero_yield_alarm`` — it has yielded records before, so this quiet
      streak is a genuine silent failure. Fix the collector / source.
    * ``thin_market_candidate`` — it has NEVER yielded anything in its
      whole history. Known example: Zameen Quetta houses (5 listings,
      verified). Flagged for human review, not treated as an alarm.

    Args:
        conn: open SQLite connection.
        lookback_runs: how many consecutive zero-fetched ok-runs trigger
            evaluation (default 3).

    Returns:
        List of dicts with keys: ``source_id``, ``alarm_type``
        (``zero_yield_alarm`` or ``thin_market_candidate``), ``streak``
        (list of ``{"fetched", "status", "ran_at"}`` newest-first),
        ``last_run_at`` and a human ``message``.
    """
    ensure_yield_table(conn)
    alarms: list[dict] = []
    source_ids = [r[0] for r in
                  conn.execute("SELECT DISTINCT source_id FROM source_yield").fetchall()]
    for source_id in sorted(source_ids):
        runs = _last_runs(conn, source_id, lookback_runs)
        if len(runs) < lookback_runs:
            continue  # too new to judge
        quiet = all(r["fetched"] == 0 and r["status"] == "ok" for r in runs)
        if not quiet:
            continue
        ever = _ever_yielded(conn, source_id)
        streak = [{"fetched": r["fetched"], "status": r["status"],
                   "ran_at": r["ran_at"]} for r in runs]
        last_run_at = runs[0]["ran_at"]
        if ever and THIN_MARKET_NEVER_YIELDED:
            alarms.append({
                "source_id": source_id,
                "alarm_type": "zero_yield_alarm",
                "streak": streak,
                "last_run_at": last_run_at,
                "message": (
                    f"{source_id}: {lookback_runs} consecutive ok runs fetched 0"
                    f" records (last at {last_run_at}) despite prior yield —"
                    " silent failure, investigate the collector/source."),
            })
        else:
            alarms.append({
                "source_id": source_id,
                "alarm_type": "thin_market_candidate",
                "streak": streak,
                "last_run_at": last_run_at,
                "message": (
                    f"{source_id}: never yielded any records in recorded history"
                    f" (last run at {last_run_at}) — thin market candidate, review"
                    " whether this source should be kept enabled."),
            })
    return alarms


def summary(conn: sqlite3.Connection) -> dict:
    """Per-source health summary for the dashboard.

    For every source present in ``source_yield`` returns the last run's
    ``fetched``/``stored``/``status``/``ran_at`` plus 7-day totals
    (records fetched/stored and number of runs).

    Returns:
        ``{"generated_at": ..., "sources": [ {...}, ... ]}`` where each
        source dict has keys: ``source_id``, ``last_fetched``,
        ``last_stored``, ``last_status``, ``last_run_at``,
        ``fetched_7d``, ``stored_7d``, ``runs_7d``.
    """
    ensure_yield_table(conn)
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    out: list[dict] = []
    source_ids = [r[0] for r in
                  conn.execute("SELECT DISTINCT source_id FROM source_yield"
                               " ORDER BY source_id").fetchall()]
    for source_id in source_ids:
        last = conn.execute(
            "SELECT fetched, stored, status, ran_at FROM source_yield"
            " WHERE source_id=? ORDER BY ran_at DESC, rowid DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        totals = conn.execute(
            "SELECT COALESCE(SUM(fetched),0) f, COALESCE(SUM(stored),0) s,"
            " COUNT(*) n FROM source_yield WHERE source_id=? AND ran_at >= ?",
            (source_id, cutoff),
        ).fetchone()
        out.append({
            "source_id": source_id,
            "last_fetched": last["fetched"] if last else 0,
            "last_stored": last["stored"] if last else 0,
            "last_status": last["status"] if last else None,
            "last_run_at": last["ran_at"] if last else None,
            "fetched_7d": totals["f"],
            "stored_7d": totals["s"],
            "runs_7d": totals["n"],
        })
    return {"generated_at": utcnow_iso(), "sources": out}
