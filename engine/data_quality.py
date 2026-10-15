"""
Data-quality stats as a runnable report (B32, Wave B).

``compute_report(conn, now=None)`` reads ONLY (SELECT statements) from the
caller's SQLite connection and returns a plain-dict report:

* FIELD COMPLETENESS per platform — posts: % with price (``budget_pkr``),
  % with ``published_at``, % with location (``city_tag`` or ``area_tag``),
  % with contact info (published phone/WhatsApp or non-none contact type),
  % with a usable URL. Leads: % with a row in ``lead_score``, % HOT
  (``hot_flag=1``), % with >=2 merged posts.
* DUPLICATE RATE — % of posts sharing (platform, platform_post_id) more
  than once (post_id is ``platform:platform_post_id``; the PK makes this
  ~0, which the metric asserts). Plus a cross-platform title+price
  near-duplicate estimate on a bounded random sample.
* INTENT DISTRIBUTION — counts per ``intent_primary`` on posts, % unclear,
  and per-platform breakdown.
* FRESHNESS — median post age (days) per platform and % of dated posts
  older than 7 days, plus the share missing ``published_at`` entirely.

``main(db_path=None)`` opens the given (or default live) database
read-only, prints the human-readable summary, and returns the dict.
``selftest()`` builds a temp-DB fixture with known completeness and
asserts the report computes the exact expected percentages.

stdlib only (sqlite3, datetime, json, logging). Never writes to the live
DB: ``main`` opens the DB in read-only URI mode.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from typing import Any

LOG = logging.getLogger(__name__)

#: Bounded sample size for the cross-platform near-duplicate estimate.
NEAR_DUP_SAMPLE_SIZE = 500

#: Title fingerprint length (chars of normalized text) used for the
#: near-duplicate estimate.
TITLE_FINGERPRINT_LEN = 80

#: Freshness staleness threshold, in days.
STALE_AFTER_DAYS = 7


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def utcnow_iso() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp, returning None when unparseable."""
    if not value:
        return None
    try:
        ts = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def _pct(part: int, whole: int) -> float | None:
    """Percentage rounded to one decimal; None when the base is empty."""
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


def _median(values: list[float]) -> float | None:
    """Median of a list, rounded to one decimal; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    med = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return round(med, 1)


def _merged_post_count(merged_json: str | None) -> int:
    """Number of posts merged into a lead, from its JSON ``merged_post_ids``."""
    if not merged_json:
        return 0
    try:
        items = json.loads(merged_json)
    except (ValueError, TypeError):
        return 0
    return len(items) if isinstance(items, list) else 0


# ----------------------------------------------------------------------
# metric blocks (SELECT-only)
# ----------------------------------------------------------------------
def post_completeness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per-platform field-completeness percentages for ``post`` rows.

    A post "has" a field when the column is non-NULL and non-empty
    (where applicable). Definitions:

    * ``price`` — ``budget_pkr IS NOT NULL``
    * ``published_at`` — present
    * ``location`` — ``city_tag`` OR ``area_tag`` present
    * ``contact`` — ``contact_published`` present OR ``contact_type``
      present and not ``'none'``
    * ``url`` — present (URL is the NOT NULL UNIQUE dedupe key, SHR-15)
    """
    rows = conn.execute(
        """
        SELECT platform,
               COUNT(*) AS total,
               SUM(CASE WHEN budget_pkr IS NOT NULL THEN 1 ELSE 0 END) AS n_price,
               SUM(CASE WHEN published_at IS NOT NULL AND published_at <> ''
                        THEN 1 ELSE 0 END) AS n_published,
               SUM(CASE WHEN (city_tag IS NOT NULL AND city_tag <> '')
                          OR (area_tag IS NOT NULL AND area_tag <> '')
                        THEN 1 ELSE 0 END) AS n_location,
               SUM(CASE WHEN (contact_published IS NOT NULL AND contact_published <> '')
                          OR (contact_type IS NOT NULL AND contact_type <> ''
                              AND contact_type <> 'none')
                        THEN 1 ELSE 0 END) AS n_contact,
               SUM(CASE WHEN url IS NOT NULL AND url <> ''
                        THEN 1 ELSE 0 END) AS n_url
          FROM post
         GROUP BY platform
         ORDER BY platform
        """
    ).fetchall()
    by_platform: dict[str, dict[str, Any]] = {}
    for platform, total, n_price, n_pub, n_loc, n_con, n_url in rows:
        by_platform[platform] = {
            "total_posts": total,
            "pct_price": _pct(n_price, total),
            "pct_published_at": _pct(n_pub, total),
            "pct_location": _pct(n_loc, total),
            "pct_contact": _pct(n_con, total),
            "pct_url": _pct(n_url, total),
        }
    return {"by_platform": by_platform,
            "total_posts": sum(v["total_posts"] for v in by_platform.values())}


def lead_completeness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Completeness percentages for ``lead`` rows.

    * ``scored`` — a matching row exists in ``lead_score``
    * ``hot`` — ``lead_score.hot_flag = 1``
    * ``multi_post`` — ``merged_post_ids`` JSON array has >= 2 entries
    """
    total = conn.execute("SELECT COUNT(*) FROM lead").fetchone()[0]
    scored = conn.execute("SELECT COUNT(*) FROM lead l "
                          "WHERE EXISTS (SELECT 1 FROM lead_score s "
                          "WHERE s.lead_id = l.lead_id)").fetchone()[0]
    hot = conn.execute("SELECT COUNT(*) FROM lead l "
                       "JOIN lead_score s ON s.lead_id = l.lead_id "
                       "WHERE s.hot_flag = 1").fetchone()[0]
    multi = sum(1 for (merged,) in conn.execute(
        "SELECT merged_post_ids FROM lead")
        if _merged_post_count(merged) >= 2)
    return {
        "total_leads": total,
        "pct_scored": _pct(scored, total),
        "pct_hot": _pct(hot, total),
        "pct_multi_post": _pct(multi, total),
    }


def duplicate_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    """Duplicate rate for the (platform, platform_post_id) key.

    ``post_id`` is ``platform:platform_post_id`` (the PK), so true key
    duplicates are impossible — this metric asserts that invariant and
    quantifies any violation. Also reports the share of posts whose
    (platform, platform_post_id) key group has more than one member.
    """
    total = conn.execute("SELECT COUNT(*) FROM post").fetchone()[0]
    # platform_post_id = post_id minus the leading "platform:" prefix.
    dup_posts = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT platform,
                   CASE WHEN instr(post_id, ':') > 0
                        THEN substr(post_id, instr(post_id, ':') + 1)
                        ELSE post_id END AS platform_post_id,
                   COUNT(*) AS c
              FROM post
             GROUP BY 1, 2
            HAVING c > 1
        )
        """
    ).fetchone()[0]
    dup_keys = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT platform,
                   CASE WHEN instr(post_id, ':') > 0
                        THEN substr(post_id, instr(post_id, ':') + 1)
                        ELSE post_id END AS platform_post_id,
                   COUNT(*) AS c
              FROM post
             GROUP BY 1, 2
            HAVING c > 1
        ) t
        """
    ).fetchone()[0]
    return {
        "total_posts": total,
        "duplicate_key_groups": dup_keys,
        "posts_in_duplicate_groups": dup_posts,
        "pct_posts_duplicated": _pct(dup_posts, total),
    }


def near_duplicate_estimate(conn: sqlite3.Connection,
                            sample_size: int = NEAR_DUP_SAMPLE_SIZE
                            ) -> dict[str, Any]:
    """Cross-platform title+price near-duplicate estimate (bounded sample).

    Draws a random sample of up to ``sample_size`` posts and fingerprints
    each as ``(first N chars of normalized text, budget_pkr)``. A sampled
    post counts as near-duplicated when its fingerprint appears on two or
    more distinct platforms. Reported as % of the sample. This deliberately
    measures what the (platform, platform_post_id) key dedupe cannot see:
    the same listing scraped from two different portals (e.g. the same
    G-13 house listed on both OLX and Zameen, which the cross-portal
    property-identity merge then resolves into one lead).
    """
    rows = conn.execute(
        """
        SELECT platform,
               substr(lower(text_normalized), 1, ?) AS title_fp,
               budget_pkr
          FROM post
         WHERE text_normalized IS NOT NULL AND text_normalized <> ''
         ORDER BY RANDOM()
         LIMIT ?
        """,
        (TITLE_FINGERPRINT_LEN, sample_size),
    ).fetchall()
    fingerprint_platforms: dict[tuple[str | None, int | None], set[str]] = {}
    for platform, title_fp, budget in rows:
        fingerprint_platforms.setdefault((title_fp, budget), set()).add(platform)
    near_duped = sum(
        1 for platform, title_fp, budget in rows
        if len(fingerprint_platforms[(title_fp, budget)]) >= 2
    )
    return {
        "sample_size": len(rows),
        "near_duplicate_posts": near_duped,
        "pct_near_duplicated": _pct(near_duped, len(rows)),
    }


def intent_distribution(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts per ``intent_primary`` on posts, % unclear, per-platform split."""
    counts = conn.execute(
        "SELECT intent_primary, COUNT(*) FROM post GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    counts = {k if k else "(null)": v for k, v in counts}
    total = sum(counts.values())
    unclear = counts.get("unclear", 0) + counts.get("(null)", 0)
    per_platform = {}
    for platform, intent, n in conn.execute(
            "SELECT platform, intent_primary, COUNT(*) FROM post "
            "GROUP BY 1, 2 ORDER BY 1, 3 DESC"):
        per_platform.setdefault(platform, {})[intent or "(null)"] = n
    return {
        "total_posts": total,
        "counts": counts,
        "pct_unclear": _pct(unclear, total),
        "by_platform": per_platform,
    }


def freshness(conn: sqlite3.Connection,
              now: datetime.datetime | None = None) -> dict[str, Any]:
    """Median post age per platform and staleness percentages.

    Age is ``now - published_at`` in days, for posts that carry a
    ``published_at``. Reports per-platform median age, the % of dated
    posts older than ``STALE_AFTER_DAYS`` days, and the % of all posts
    missing ``published_at`` (missing dates cannot be scored for
    recency, which is itself a data-quality signal).
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    rows = conn.execute(
        "SELECT platform, published_at FROM post"
    ).fetchall()
    by_platform: dict[str, list[float]] = {}
    missing = 0
    for platform, published_at in rows:
        ts = _parse_ts(published_at)
        if ts is None:
            missing += 1
            continue
        by_platform.setdefault(platform, []).append(
            (now - ts).total_seconds() / 86400.0)
    total = len(rows)
    dated = total - missing
    stale = sum(1 for ages in by_platform.values()
                for age in ages if age > STALE_AFTER_DAYS)
    medians = {p: _median(ages) for p, ages in sorted(by_platform.items())}
    return {
        "as_of": now.isoformat(),
        "total_posts": total,
        "dated_posts": dated,
        "pct_missing_published_at": _pct(missing, total),
        "median_age_days_by_platform": medians,
        "pct_older_than_7d": _pct(stale, dated),
    }


# ----------------------------------------------------------------------
# report assembly + rendering
# ----------------------------------------------------------------------
def compute_report(conn: sqlite3.Connection,
                   now: datetime.datetime | None = None) -> dict[str, Any]:
    """Compute the full data-quality report from an open connection.

    Read-only: issues SELECT queries only. ``now`` (UTC) anchors the
    freshness math so fixtures and tests stay deterministic.
    """
    posts = post_completeness(conn)
    return {
        "generated_at": utcnow_iso(),
        "posts": posts,
        "leads": lead_completeness(conn),
        "duplicates": duplicate_rate(conn),
        "near_duplicates": near_duplicate_estimate(conn),
        "intent": intent_distribution(conn),
        "freshness": freshness(conn, now=now),
    }


def _bar(pct: float | None, width: int = 20) -> str:
    """Tiny text bar for a percentage."""
    if pct is None:
        return "n/a"
    filled = int(round(pct / 100 * width))
    return "#" * filled + "-" * (width - filled)


def render(report: dict[str, Any]) -> str:
    """Render the report dict as a human-readable text summary."""
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("PAKLEAD ENGINE — DATA QUALITY REPORT")
    lines.append(f"generated: {report['generated_at']}")
    lines.append("=" * 64)

    # --- field completeness ---
    lines.append("")
    lines.append("1) FIELD COMPLETENESS (posts, per platform)")
    for platform, m in sorted(report["posts"]["by_platform"].items()):
        lines.append(f"  [{platform}] n={m['total_posts']}")
        for label, key in (("price        ", "pct_price"),
                           ("published_at ", "pct_published_at"),
                           ("location     ", "pct_location"),
                           ("contact      ", "pct_contact"),
                           ("url          ", "pct_url")):
            pct = m[key]
            lines.append(f"    {label}: {pct if pct is None else f'{pct:5.1f}%'} "
                         f"{_bar(pct)}")
    leads = report["leads"]
    lines.append("")
    lines.append(f"   LEADS n={leads['total_leads']}: "
                 f"scored {leads['pct_scored']}%, "
                 f"HOT {leads['pct_hot']}%, "
                 f">=2 merged posts {leads['pct_multi_post']}%")

    # --- duplicates ---
    dup = report["duplicates"]
    near = report["near_duplicates"]
    lines.append("")
    lines.append("2) DUPLICATE RATE")
    lines.append(f"   key (platform, platform_post_id) duplicate groups: "
                 f"{dup['duplicate_key_groups']}")
    lines.append(f"   posts in duplicate groups: "
                 f"{dup['posts_in_duplicate_groups']} "
                 f"({dup['pct_posts_duplicated']}%)")
    lines.append(f"   cross-platform title+price near-dup estimate "
                 f"(sample n={near['sample_size']}): "
                 f"{near['pct_near_duplicated']}%")

    # --- intent ---
    intent = report["intent"]
    lines.append("")
    lines.append(f"3) INTENT DISTRIBUTION (n={intent['total_posts']}, "
                 f"unclear {intent['pct_unclear']}%)")
    for name, count in sorted(intent["counts"].items(),
                              key=lambda kv: kv[1], reverse=True):
        lines.append(f"   {name:20s}: {count:5d} "
                     f"({_pct(count, intent['total_posts'])}%)")

    # --- freshness ---
    fresh = report["freshness"]
    lines.append("")
    lines.append("4) FRESHNESS")
    lines.append(f"   missing published_at: "
                 f"{fresh['pct_missing_published_at']}%")
    lines.append(f"   older than {STALE_AFTER_DAYS}d (of dated): "
                 f"{fresh['pct_older_than_7d']}%")
    for platform, med in sorted(fresh["median_age_days_by_platform"].items()):
        lines.append(f"   median age [{platform}]: "
                     f"{'n/a' if med is None else f'{med:.1f}d'}")

    lines.append("=" * 64)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def main(db_path: str | None = None) -> dict[str, Any]:
    """Open the DB read-only, compute + print the report, return the dict.

    ``db_path`` defaults to the engine's live database
    (``data/pak_leads_live.db``). The connection is opened in read-only
    URI mode: this function never writes to the database.
    """
    import pathlib
    if db_path is None:
        here = pathlib.Path(__file__).resolve().parent.parent
        db_path = str(here / "data" / "pak_leads_live.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = compute_report(conn)
    finally:
        conn.close()
    print(render(report))
    LOG.info("data-quality report computed for %s", db_path)
    return report


# ----------------------------------------------------------------------
# fixture self-test (temp DB only, exact-percentage assertions)
# ----------------------------------------------------------------------
def build_fixture(now: datetime.datetime) -> sqlite3.Connection:
    """Build a temp in-memory DB with known completeness for ``selftest``.

    Fixture layout (exact expectations live in :func:`selftest`):

    * 10 OLX posts — price on 7 (70%), published_at on 8 (80%),
      location on 5 (50%), contact on 6 (60%), URL on all (100%).
    * 2 Zameen posts — one is a title+price near-duplicate of an OLX
      post on a different platform.
    * Intents — 8 property_offered, 4 unclear (33.3%).
    * 4 leads — 3 scored (75%), 2 HOT (50%), 1 with >=2 merged posts (25%).
    * No (platform, platform_post_id) duplicates.
    """
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(
        __file__).resolve().parent.parent))
    from models import init_db

    conn = sqlite3.connect(":memory:")
    init_db(conn)

    def post(post_id, platform, *, price=None, days_ago=None,
             location=None, contact=None, intent="property_offered",
             text="5 marla brand new house available for sale. price rs "
                  "26000000. size 5 marla. brand new construction."):
        city, area = location if location else (None, None)
        published = (now - datetime.timedelta(days=days_ago)).isoformat() \
            if days_ago is not None else None
        conn.execute(
            """INSERT INTO post
               (post_id, platform, source_id, text_original, text_normalized,
                city_tag, area_tag, intent_primary, budget_pkr,
                contact_published, contact_type, url, published_at, seen_at)
               VALUES (?, ?, 'src-fixture', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, platform, text, text.lower(), city, area, intent,
             price, contact, "phone" if contact else "none",
             f"https://example.invalid/{post_id}", published,
             now.isoformat()))

    ages = [1, 2, 3, 10, 1, 2, 3, 10]
    for i in range(1, 11):
        post(f"olx:{i:04d}", "olx",
             price=10_000_000 + i if i <= 7 else None,
             days_ago=ages[i - 1] if i <= 8 else None,
             location=("Islamabad", "G-13") if i <= 5 else None,
             contact=f"0300000000{i}" if i <= 6 else None,
             intent="property_offered" if i <= 8 else "unclear")

    near_dup_text = ("5 marla brand new house available for sale. price rs "
                     "26000000. size 5 marla. brand new construction.")
    post("zameen:0001", "zameen", price=10000001, days_ago=None,
         location=None, contact=None, intent="unclear",
         text=near_dup_text)
    post("zameen:0002", "zameen", price=None, days_ago=None,
         location=None, contact=None, intent="unclear",
         text="commercial shop available on main boulevard for sale. "
              "corner unit. serious buyers only.")
    conn.commit()
    return conn


def selftest() -> dict[str, Any]:
    """Verify the report against the temp fixture with exact assertions.

    Returns the fixture report on success; raises AssertionError on any
    mismatch. Uses an in-memory DB — the live DB is never touched.
    """
    now = datetime.datetime(2026, 10, 15, 13, 0, 0,
                            tzinfo=datetime.timezone.utc)
    conn = build_fixture(now)

    # --- leads: 3/4 scored, 2/4 hot, 1/4 multi-post ---
    conn.executemany(
        "INSERT INTO lead (lead_id, merged_post_ids, intent, city, "
        "status, first_seen) VALUES (?, ?, 'property_offered', "
        "'Islamabad', ?, ?)",
        [("L1", '["olx:0001"]', "hot", now.isoformat()),
         ("L2", '["olx:0002"]', "new", now.isoformat()),
         ("L3", '["olx:0003", "olx:0004"]', "new", now.isoformat()),
         ("L4", '["olx:0005"]', "new", now.isoformat())])
    conn.executemany(
        "INSERT INTO lead_score (lead_id, computed_at, "
        "scoring_config_version, total, hot_flag) VALUES (?, ?, 'v1', ?, ?)",
        [("L1", now.isoformat(), 91, 1),
         ("L2", now.isoformat(), 55, 0),
         ("L3", now.isoformat(), 60, 1)])
    conn.commit()

    report = compute_report(conn, now=now)
    conn.close()

    olx = report["posts"]["by_platform"]["olx"]
    assert olx["total_posts"] == 10, olx
    assert olx["pct_price"] == 70.0, olx
    assert olx["pct_published_at"] == 80.0, olx
    assert olx["pct_location"] == 50.0, olx
    assert olx["pct_contact"] == 60.0, olx
    assert olx["pct_url"] == 100.0, olx

    leads = report["leads"]
    assert leads["total_leads"] == 4, leads
    assert leads["pct_scored"] == 75.0, leads
    assert leads["pct_hot"] == 50.0, leads
    assert leads["pct_multi_post"] == 25.0, leads

    dup = report["duplicates"]
    assert dup["duplicate_key_groups"] == 0, dup
    assert dup["pct_posts_duplicated"] == 0.0, dup

    near = report["near_duplicates"]
    # 2 of 12 sampled posts share (title_fp, price) across OLX/Zameen.
    assert near["sample_size"] == 12, near
    assert near["pct_near_duplicated"] == 16.7, near

    intent = report["intent"]
    assert intent["counts"]["property_offered"] == 8, intent
    assert intent["counts"]["unclear"] == 4, intent
    assert intent["pct_unclear"] == 33.3, intent

    fresh = report["freshness"]
    # Ages (days): [1,2,3,10,1,2,3,10] -> median 2.5; 2/8 > 7d; 4/12 missing.
    assert fresh["median_age_days_by_platform"]["olx"] == 2.5, fresh
    assert fresh["median_age_days_by_platform"].get("zameen") is None, fresh
    assert fresh["pct_older_than_7d"] == 25.0, fresh
    assert fresh["pct_missing_published_at"] == 33.3, fresh

    LOG.info("data-quality fixture selftest passed")
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    selftest()   # temp in-memory fixture, exact-percentage assertions
    main()       # live DB, read-only

