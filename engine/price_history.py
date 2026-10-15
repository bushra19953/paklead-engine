"""
Price-history snapshots + price-drop detector (fleet builder B20).

BACKGROUND (verified 2026-10-15): OLX Pakistan pages expose NO price-history
field, so price-drop signals must be OUR OWN signal — we remember prices over
time and compare them ourselves.

Public functions
----------------
- snapshot(conn, listing_key, price_pkr, seen_at)
      Record one price observation for a listing.
      ``listing_key`` is ``f"{platform}:{platform_post_id}"`` — stable across
      sweeps (identical to the ``post.post_id`` convention in models.py).
      Skips silently when no price is present (``price_pkr is None``).
      Prunes snapshots older than 90 days on every write.
- price_drop(conn, listing_key, drop_pct=10.0, window_days=30) -> dict | None
      Drop evidence ``{first_price, last_price, drop_pct, first_seen,
      last_seen}`` when the latest price is ``>= drop_pct`` below the max
      price seen in the window; ``None`` otherwise.
      A drop that then recovers does NOT re-fire — we fire only when the
      latest price is also the window minimum.
- first_seen_price(conn, listing_key)
      Earliest recorded price for a listing (``None`` if unknown).

INTEGRATION CONTRACT
--------------------
1. ``snapshot()`` is called during post ingestion in the pipeline for every
   ingested post. KEY INSIGHT: only snapshot when a price is ACTUALLY present
   in the record (``price_pkr`` / ``budget_pkr`` non-null) — a missing price is
   data absence, not a zero-price drop, and snapshotting it would poison the
   history. ``snapshot()`` itself also no-ops on ``None`` prices defensively.
2. ``price_drop()`` results feed a ``price_drop`` flag on the lead (alongside
   its evidence dict) so the digest can surface "price cut" opportunities.
3. The table is created by this module on first use via
   ``ensure_price_table()`` (init-style migration), so it works on fresh DBs
   and upgrades existing ones without touching models.py.

stdlib only (sqlite3, datetime). Money in whole PKR. Timestamps ISO-8601.
"""

from __future__ import annotations

import datetime
import sqlite3

RETENTION_DAYS = 90


def ensure_price_table(conn: sqlite3.Connection) -> None:
    """Create ``price_snapshots`` (and its index) if missing. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL,      -- platform:platform_post_id
            price_pkr INTEGER NOT NULL,     -- whole PKR
            seen_at TEXT NOT NULL          -- ISO-8601, UTC
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_snapshots_key "
        "ON price_snapshots (listing_key, seen_at)"
    )
    conn.commit()


def _iso(seen_at: datetime.datetime | str) -> str:
    """Normalize a datetime or ISO string to an ISO-8601 string."""
    if isinstance(seen_at, datetime.datetime):
        return seen_at.isoformat(timespec="seconds")
    return str(seen_at)


def snapshot(conn: sqlite3.Connection, listing_key: str,
             price_pkr: int | None,
             seen_at: datetime.datetime | str) -> bool:
    """
    Record one price observation for a listing.

    Returns ``True`` when a snapshot was recorded, ``False`` when skipped
    (no price present). Snapshots older than ``RETENTION_DAYS`` are pruned
    on every write. The caller owns the transaction; we commit for
    standalone convenience.
    """
    if listing_key is None or price_pkr is None:
        return False
    ensure_price_table(conn)
    ts = _iso(seen_at)
    conn.execute(
        "INSERT INTO price_snapshots (listing_key, price_pkr, seen_at)"
        " VALUES (?,?,?)",
        (listing_key, int(price_pkr), ts),
    )
    cutoff = (datetime.datetime.fromisoformat(ts)
              - datetime.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        "DELETE FROM price_snapshots WHERE listing_key = ? AND seen_at < ?",
        (listing_key, cutoff),
    )
    conn.commit()
    return True


def _window(conn: sqlite3.Connection, listing_key: str,
            window_days: int) -> list[sqlite3.Row]:
    """Snapshots for a listing within ``window_days`` of its latest snapshot."""
    rows = conn.execute(
        "SELECT price_pkr, seen_at FROM price_snapshots"
        " WHERE listing_key = ? ORDER BY seen_at",
        (listing_key,),
    ).fetchall()
    if not rows:
        return []
    latest = rows[-1]["seen_at"]
    cutoff = (datetime.datetime.fromisoformat(latest)
              - datetime.timedelta(days=window_days)).isoformat(timespec="seconds")
    return [r for r in rows if r["seen_at"] >= cutoff]


def price_drop(conn: sqlite3.Connection, listing_key: str,
               drop_pct: float = 10.0,
               window_days: int = 30) -> dict | None:
    """
    Detect a sustained price drop for a listing.

    Fires (returns evidence) only when BOTH hold:
      1. latest price <= (1 - drop_pct/100) * window max price;
      2. latest price is the window minimum (a drop that recovered does not
         re-fire — the latest price is back up, so it is not the minimum).
    Otherwise returns ``None``.
    """
    ensure_price_table(conn)
    rows = _window(conn, listing_key, window_days)
    if len(rows) < 2:
        return None
    prices = [r["price_pkr"] for r in rows]
    first, last = rows[0], rows[-1]
    max_price = max(prices)
    min_price = min(prices)
    if max_price <= 0:
        return None
    computed = (max_price - last["price_pkr"]) / max_price * 100.0
    if computed >= drop_pct and last["price_pkr"] == min_price:
        return {
            "first_price": first["price_pkr"],
            "first_seen": first["seen_at"],
            "last_price": last["price_pkr"],
            "last_seen": last["seen_at"],
            "drop_pct": round(computed, 1),
        }
    return None


def first_seen_price(conn: sqlite3.Connection,
                     listing_key: str) -> int | None:
    """Earliest recorded price for a listing, or ``None`` if never seen."""
    ensure_price_table(conn)
    row = conn.execute(
        "SELECT price_pkr FROM price_snapshots"
        " WHERE listing_key = ? ORDER BY seen_at LIMIT 1",
        (listing_key,),
    ).fetchone()
    return row["price_pkr"] if row else None
