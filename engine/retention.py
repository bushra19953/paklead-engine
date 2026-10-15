"""
180-day data retention job (fleet builder B34, Wave B).

Legal boundary: collected post data must not be retained longer than
``RETENTION_DAYS`` (180). This module reports how much data is past the
retention window and — under a two-key safety — purges it.

Public functions
----------------
- retention_report(conn, days=180) -> dict
      Read-only. Counts rows older than the cutoff in ``post``,
      ``lead_score`` (joined via ``lead``), ``comment`` (via parent post),
      and the sibling-module snapshot tables ``price_snapshots`` and
      ``source_yield``. Returns per-table counts plus the oldest
      timestamps among the stale rows. Never deletes anything.
- purge(conn, days=180, dry_run=True, confirm=False) -> dict
      ``dry_run=True`` (default) returns exactly what *would* be deleted
      (counts + sample ids) without touching the database.
      ``dry_run=False`` performs the deletes in dependency order
      (children before parents) inside a single transaction with rollback
      on error. It REFUSES to run unless ``confirm=True`` is also passed
      (two-key safety: the caller must explicitly name both keys).

Retention policy (which rows are stale)
---------------------------------------
- ``post``: ``seen_at`` older than the cutoff. (``seen_at`` is the
  ingestion timestamp the engine always writes; ``published_at`` may be
  absent on web-collected records.)
- ``lead``: purgeable only when ``first_seen`` is older than the cutoff
  AND every ``merged_post_ids`` entry is itself stale or gone. A lead
  that still touches a recent post is live evidence and is kept.
- ``lead_score``: rows whose parent ``lead`` is purgeable (joined via
  ``lead``).
- ``comment``: rows whose parent ``post`` is stale. Comments are
  contactability evidence; a comment on a kept post is kept.
- ``price_snapshots``: ``seen_at`` older than the cutoff (stale price
  observations, whatever the listing's current status).
- ``source_yield``: ``ran_at`` older than the cutoff (old run telemetry).

NEVER deleted (protected tables)
---------------------------------
``source``, ``buyer``, ``audit_entry``, ``suppression_entry``,
``collection_run``, ``author_profile``, ``loophole_report``.

Why ``buyer`` survives: the buyer table is *derived demand* — anonymised
buyer profiles merged from demand signals across time. It is not the
collected post data the retention boundary targets, and deleting it would
destroy the buyer's-list workstream the wholesale book's Disposition
pillar depends on. ``source`` rows are configuration; the rest are
provenance/audit records that must outlive the data they describe.

Delete order (children before parents)
---------------------------------------
lead_score -> lead -> comment -> price_snapshots -> post -> source_yield.
All deletes run in one transaction; any error rolls everything back.

Integration contract (monthly cron)
-----------------------------------
A monthly scheduler (e.g. cron ``0 2 1 * *`` PKT) would call::

    import sqlite3
    from engine import retention
    from models import connect

    conn = connect("/path/to/pak_leads_live.db")
    try:
        report = retention.retention_report(conn, days=180)
        log.info("retention report: %s", report)   # review before purging
        result = retention.purge(conn, days=180, dry_run=False, confirm=True)
        log.info("purged: %s", result["deleted"])
    finally:
        conn.close()

The cron operator reviews the ``retention_report`` output before the real
purge runs; both functions print nothing and log at DEBUG level, so the
caller owns all logging/persistence.

stdlib only (sqlite3, datetime, json, logging). Never touches the live
DB except through the caller-supplied connection; verify on a temp DB.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3

log = logging.getLogger(__name__)

#: Legal maximum retention for collected post data (days).
DEFAULT_RETENTION_DAYS = 180

#: Tables that are never purged, whatever their age. See module docstring
#: for why (buyer = derived demand, source = config, rest = provenance).
PROTECTED_TABLES = (
    "source",
    "buyer",
    "audit_entry",
    "suppression_entry",
    "collection_run",
    "author_profile",
    "loophole_report",
)

#: How many sample ids to include per table in a dry-run report.
_DRY_RUN_SAMPLE_LIMIT = 5

#: Chunk size for IN-clause deletes (keeps statements small).
_DELETE_CHUNK = 500


class RetentionSafetyError(ValueError):
    """Raised when purge() is asked to really delete without confirm=True."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when ``name`` exists in this database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _cutoff_iso(days: int) -> str:
    """ISO-8601 cutoff: now (UTC) minus ``days``."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days
    )
    return cutoff.isoformat(timespec="seconds")


def _parse_ts(value) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp to an aware datetime (naive => UTC).

    Returns None for NULL/empty/unparseable values so they are never
    treated as stale.
    """
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _is_stale(value, cutoff: datetime.datetime) -> bool:
    """True when a timestamp value is strictly older than the cutoff."""
    dt = _parse_ts(value)
    return dt is not None and dt < cutoff


def _min_ts(values) -> str | None:
    """Oldest raw timestamp string among ``values`` (None if none parse)."""
    parsed = [(_parse_ts(v), v) for v in values]
    parsed = [(dt, raw) for dt, raw in parsed if dt is not None]
    if not parsed:
        return None
    return min(parsed, key=lambda p: p[0])[1]


def _select(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Run a SELECT and return rows."""
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# stale-row selection (Python-side so report == dry_run == real purge)
# ---------------------------------------------------------------------------


def _stale_post_ids(conn: sqlite3.Connection,
                    cutoff: datetime.datetime) -> list[str]:
    """Ids of ``post`` rows with ``seen_at`` older than the cutoff."""
    if not _table_exists(conn, "post"):
        return []
    rows = _select(conn, "SELECT post_id, seen_at FROM post")
    return [r["post_id"] for r in rows if _is_stale(r["seen_at"], cutoff)]


def _stale_comment_ids(conn: sqlite3.Connection,
                       stale_post_ids: list[str]) -> list[str]:
    """Ids of ``comment`` rows whose parent post is stale."""
    if not _table_exists(conn, "comment") or not stale_post_ids:
        return []
    ids = []
    for i in range(0, len(stale_post_ids), _DELETE_CHUNK):
        chunk = stale_post_ids[i:i + _DELETE_CHUNK]
        q = "SELECT comment_id FROM comment WHERE post_id IN (%s)" % ",".join(
            "?" * len(chunk)
        )
        ids.extend(r["comment_id"] for r in _select(conn, q, chunk))
    return ids


def _purgeable_lead_ids(conn: sqlite3.Connection, cutoff: datetime.datetime,
                         stale_post_ids: list[str]) -> list[str]:
    """Ids of ``lead`` rows that are both old and fully stale.

    A lead is purgeable only when ``first_seen`` is older than the cutoff
    AND every ``merged_post_ids`` entry is stale (or already gone). A lead
    that still references a recent post is live evidence and is kept.
    """
    if not _table_exists(conn, "lead"):
        return []
    stale_posts = set(stale_post_ids)
    purgeable = []
    for row in _select(conn, "SELECT lead_id, merged_post_ids, first_seen FROM lead"):
        if not _is_stale(row["first_seen"], cutoff):
            continue
        try:
            merged = json.loads(row["merged_post_ids"] or "[]")
        except (ValueError, TypeError):
            merged = []
        # A merged id keeps the lead alive when it belongs to a post that
        # is NOT stale (recent post or a post still in the DB and recent).
        if merged:
            q = ("SELECT post_id, seen_at FROM post WHERE post_id IN "
                 "(%s)" % ",".join("?" * len(merged)))
            live_posts = _select(conn, q, tuple(merged))
        else:
            live_posts = []
        live = {r["post_id"] for r in live_posts
                if not _is_stale(r["seen_at"], cutoff)}
        if not live:
            purgeable.append(row["lead_id"])
    return purgeable


def _stale_lead_score_ids(conn: sqlite3.Connection,
                          purgeable_lead_ids: list[str]) -> list[str]:
    """Ids of ``lead_score`` rows whose parent lead is purgeable."""
    if not _table_exists(conn, "lead_score") or not purgeable_lead_ids:
        return []
    ids = []
    for i in range(0, len(purgeable_lead_ids), _DELETE_CHUNK):
        chunk = purgeable_lead_ids[i:i + _DELETE_CHUNK]
        q = "SELECT lead_id FROM lead_score WHERE lead_id IN (%s)" % ",".join(
            "?" * len(chunk)
        )
        ids.extend(r["lead_id"] for r in _select(conn, q, chunk))
    return ids


def _stale_snapshot_ids(conn: sqlite3.Connection, table: str,
                        id_col: str, ts_col: str,
                        cutoff: datetime.datetime) -> list:
    """Ids of rows in a sibling snapshot table older than the cutoff."""
    if not _table_exists(conn, table):
        return []
    rows = _select(conn, f"SELECT {id_col}, {ts_col} FROM {table}")
    return [r[id_col] for r in rows if _is_stale(r[ts_col], cutoff)]


def _collect_targets(conn: sqlite3.Connection,
                     days: int) -> tuple[dict, datetime.datetime, str]:
    """Compute every stale id-set for the purge, plus cutoff info.

    Returns (targets, cutoff_datetime, cutoff_iso) where targets maps a
    table name to its list of ids to delete.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days!r}")
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days
    )
    cutoff_iso = cutoff.isoformat(timespec="seconds")

    stale_posts = _stale_post_ids(conn, cutoff)
    purgeable_leads = _purgeable_lead_ids(conn, cutoff, stale_posts)
    targets = {
        "lead_score": _stale_lead_score_ids(conn, purgeable_leads),
        "lead": purgeable_leads,
        "comment": _stale_comment_ids(conn, stale_posts),
        "price_snapshots": _stale_snapshot_ids(
            conn, "price_snapshots", "snapshot_id", "seen_at", cutoff
        ),
        "post": stale_posts,
        "source_yield": _stale_snapshot_ids(
            conn, "source_yield", "run_id", "ran_at", cutoff
        ),
    }
    return targets, cutoff, cutoff_iso


def _id_column(table: str) -> str:
    """Primary-key column used for deletes/sample ids."""
    return {
        "lead_score": "lead_id",
        "lead": "lead_id",
        "comment": "comment_id",
        "price_snapshots": "snapshot_id",
        "post": "post_id",
        "source_yield": "run_id",
    }[table]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def retention_report(conn: sqlite3.Connection, days: int = 180) -> dict:
    """Report how much data is older than the retention window.

    Read-only: inspects counts and oldest timestamps, never deletes.

    Returns a dict with ``days``, the ``cutoff`` ISO timestamp, and a
    ``tables`` mapping each covered table to ``count_old``,
    ``count_total``, and the oldest timestamp among the stale rows
    (``oldest_old``). ``lead_score`` rows are counted via their parent
    ``lead`` (join); ``comment`` rows via their parent ``post``.
    """
    targets, cutoff, cutoff_iso = _collect_targets(conn, days)
    tables = {}
    ts_col = {
        "lead_score": "computed_at",
        "lead": "first_seen",
        "comment": "seen_at",
        "price_snapshots": "seen_at",
        "post": "seen_at",
        "source_yield": "ran_at",
    }
    for table, ids in targets.items():
        if not _table_exists(conn, table):
            tables[table] = {"count_old": 0, "count_total": 0,
                             "oldest_old": None, "table_missing": True}
            continue
        total = _select(conn, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        if ids:
            stale_ts = []
            id_col = _id_column(table)
            for i in range(0, len(ids), _DELETE_CHUNK):
                chunk = ids[i:i + _DELETE_CHUNK]
                q = (f"SELECT {ts_col[table]} AS ts FROM {table} "
                     f"WHERE {id_col} IN ({','.join('?' * len(chunk))})")
                stale_ts.extend(r["ts"] for r in _select(conn, q, chunk))
            oldest_old = _min_ts(stale_ts)
        else:
            oldest_old = None
        tables[table] = {
            "count_old": len(ids),
            "count_total": total,
            "oldest_old": oldest_old,
        }
        log.debug("retention report %s: %d/%d stale", table, len(ids), total)
    return {"days": days, "cutoff": cutoff_iso, "tables": tables}


def purge(conn: sqlite3.Connection, days: int = DEFAULT_RETENTION_DAYS,
          dry_run: bool = True, confirm: bool = False) -> dict:
    """Purge rows older than the retention window.

    With ``dry_run=True`` (default) nothing is deleted: the returned
    ``would_delete`` mapping shows per-table counts plus up to
    ``_DRY_RUN_SAMPLE_LIMIT`` sample ids, exactly what a real run would
    remove.

    With ``dry_run=False`` the caller must ALSO pass ``confirm=True``
    (two-key safety); otherwise :class:`RetentionSafetyError` is raised
    and nothing happens. The real purge deletes in dependency order
    (lead_score -> lead -> comment -> price_snapshots -> post ->
    source_yield) inside one transaction with rollback on error.

    Protected tables (``source``, ``buyer``, ``audit_entry``,
    ``suppression_entry``, ``collection_run``, ``author_profile``,
    ``loophole_report``) are never deleted; ``buyer`` is derived demand,
    not collected post data.
    """
    if not dry_run and not confirm:
        raise RetentionSafetyError(
            "purge(dry_run=False) refused: pass confirm=True as the second "
            "key to authorize real deletion. Nothing was deleted."
        )
    targets, cutoff, cutoff_iso = _collect_targets(conn, days)

    if dry_run:
        would_delete = {}
        for table, ids in targets.items():
            would_delete[table] = {
                "count": len(ids),
                "sample_ids": ids[:_DRY_RUN_SAMPLE_LIMIT],
            }
        return {
            "dry_run": True,
            "days": days,
            "cutoff": cutoff_iso,
            "would_delete": would_delete,
            "protected_tables_kept": list(PROTECTED_TABLES),
        }

    # Real purge: children before parents, one transaction, rollback on error.
    delete_order = ["lead_score", "lead", "comment",
                    "price_snapshots", "post", "source_yield"]
    deleted = {}
    try:
        conn.execute("BEGIN")
        for table in delete_order:
            ids = targets[table]
            if not ids or not _table_exists(conn, table):
                deleted[table] = 0
                continue
            id_col = _id_column(table)
            n = 0
            for i in range(0, len(ids), _DELETE_CHUNK):
                chunk = ids[i:i + _DELETE_CHUNK]
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {id_col} IN "
                    f"({','.join('?' * len(chunk))})",
                    chunk,
                )
                n += cur.rowcount
            deleted[table] = n
            log.debug("retention purge %s: deleted %d", table, n)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        log.exception("retention purge failed; rolled back")
        raise
    return {
        "dry_run": False,
        "days": days,
        "cutoff": cutoff_iso,
        "deleted": deleted,
        "protected_tables_kept": list(PROTECTED_TABLES),
    }
