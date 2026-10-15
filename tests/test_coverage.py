#!/usr/bin/env python3
"""Coverage tests (B38, Wave B) — town factory, buyer registry, yield monitor.

Runnable with the stdlib only (venv python preferred):

    ~/workspace/pak_lead_engine/.venv/bin/python tests/test_coverage.py

Covers:
  1. engine.town_source_factory — generate() determinism, url:…;pages:…
     grammar (OLX pages:1, Zameen pages:3), duplicate-free output, and
     validate() catching a bad source_id and an untraceable URL.
  2. engine.buyer_registry — phone merge (same phone → one buyer),
     name/handle never merges, anonymous demand signals, seller-lane
     exclusion, derive idempotency, match_buyers ranking + shapes,
     get_buyers_for_lead and buyer_summary.
  3. engine.yield_monitor — zero-yield alarm after 3 quiet ok runs,
     thin-market candidate (no alarm) for never-yielded sources, failed
     runs never alarming, new sources skipped.
  4. engine.retention — contract tests that SELF-SKIP while the module is
     unimplemented (no engine/retention.py exists as of 2026-10-15); they
     activate automatically once the owning worker lands the module.

All fixtures are synthetic (temp trees under /tmp, in-memory sqlite DBs).
The live DB is never touched. Conventions match tests/test_engine.py:
plain assert functions, "ok …" prints, __main__ runner.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import buyer_registry as BR  # noqa: E402
from engine import town_source_factory as TF  # noqa: E402
from engine import yield_monitor as YM  # noqa: E402
from models import init_db  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)
NOW_ISO = NOW.isoformat()


# ======================================================================
# 1. Town-source factory
# ======================================================================

def _factory_trees() -> tempfile.TemporaryDirectory:
    """Hand-written Wave-A-style tree fixture under /tmp (no DB, no web)."""
    td = tempfile.TemporaryDirectory(prefix="paklead-trees-")
    base = pathlib.Path(td.name)
    (base / "olx_lahore_towns.json").write_text(json.dumps([
        {"town_slug": "dha-defence",
         "url": "https://www.olx.com.pk/dha-defence_g5001007/houses_c1721"},
        {"town_slug": "gulberg-greens",
         "url": "https://www.olx.com.pk/gulberg-greens_g5002003/houses_c1721"},
    ]), encoding="utf-8")
    (base / "zameen_lahore_towns.json").write_text(json.dumps([
        {"url": "https://www.zameen.com/Houses_Property/Lahore_Dha_Defence-3188-1.html"},
        {"url": "https://www.zameen.com/Houses_Property/Lahore_Bahria_Town-3227-1.html"},
    ]), encoding="utf-8")
    (base / "olx_new_cities.json").write_text(json.dumps({
        "cities": [{"city": "Quetta",
                    "houses_url": "https://www.olx.com.pk/quetta_g4060659/houses_c1721"}],
    }), encoding="utf-8")
    (base / "zameen_new_cities.json").write_text(json.dumps({
        "verified_cities": [{"city": "Quetta",
                             "houses_url": "https://www.zameen.com/Houses_Property/Quetta-2-1.html"}],
    }), encoding="utf-8")
    scratch = base / "a05_scratch"
    scratch.mkdir()
    (scratch / "all_towns.json").write_text(json.dumps({
        "Quetta": {"city-housing-scheme_g1234": "City Housing Scheme"},
    }), encoding="utf-8")
    return td


def test_factory_generate_deterministic():
    td = _factory_trees()
    try:
        run1 = TF.generate(td.name, "Lahore", "olx")
        run2 = TF.generate(td.name, "Lahore", "olx")
        assert run1 == run2, "generate() must be deterministic"
        ids = [r["source_id"] for r in run1]
        assert ids == ["olx-town-lahore-dha-defence",
                       "olx-town-lahore-gulberg-greens"], ids
        zr = TF.generate(td.name, "Lahore", "zameen")
        assert [r["source_id"] for r in zr] == [
            "zameen-town-lahore-dha-defence",
            "zameen-town-lahore-bahria-town"], [r["source_id"] for r in zr]
    finally:
        td.cleanup()
    print("ok  factory: deterministic source_ids (olx + zameen)")


def test_factory_grammar_pages_and_dedup():
    td = _factory_trees()
    try:
        olx = TF.generate(td.name, "Lahore", "olx")
        zmn = TF.generate(td.name, "Lahore", "zameen")
        for r in olx:
            url, pages = TF._parse_boundary_note(r["boundary_note"])
            assert pages == TF.OLX_SWEEP_PAGES == 1, r["boundary_note"]
            assert TF.OLX_TOWN_URL_RE.match(url), f"OLX pattern fail: {url}"
        for r in zmn:
            url, pages = TF._parse_boundary_note(r["boundary_note"])
            assert pages == TF.ZAMEEN_SWEEP_PAGES == 3, r["boundary_note"]
            assert "?sort=date_desc" in url, f"missing sort suffix: {url}"
            assert TF.ZAMEEN_URL_RE.match(url), f"Zameen pattern fail: {url}"
        for rows in (olx, zmn):
            sids = [r["source_id"] for r in rows]
            assert len(set(sids)) == len(sids), "duplicate source_id"
            urls = [TF._parse_boundary_note(r["boundary_note"])[0] for r in rows]
            assert len(set(urls)) == len(urls), "duplicate URL"
        # Full validation against the trees must be clean.
        assert TF.validate(olx + zmn, trees_dir=td.name) == []
    finally:
        td.cleanup()
    print("ok  factory: url:…;pages:… grammar (OLX 1 / Zameen 3), dedup, validate clean")


def test_factory_backfill_override():
    td = _factory_trees()
    try:
        rows = TF.generate(td.name, "Lahore", "zameen", backfill_pages=10)
        for r in rows:
            _, pages = TF._parse_boundary_note(r["boundary_note"])
            assert pages == 10, r["boundary_note"]
        assert TF.validate(rows, trees_dir=td.name) == []
        try:
            TF.generate(td.name, "Lahore", "zameen", backfill_pages=0)
        except ValueError:
            pass
        else:
            raise AssertionError("backfill_pages=0 must raise ValueError")
    finally:
        td.cleanup()
    print("ok  factory: Zameen backfill_pages override + pages floor")


def test_factory_validate_bad_source_id():
    td = _factory_trees()
    try:
        rows = TF.generate(td.name, "Lahore", "olx")
        bad = [dict(r) for r in rows]
        bad[0]["source_id"] = "BAD ID!!"
        errs = TF.validate(bad, trees_dir=td.name)
        assert any("bad source_id" in e for e in errs), errs
    finally:
        td.cleanup()
    print("ok  factory: validate() catches a malformed source_id")


def test_factory_validate_untraceable_url():
    td = _factory_trees()
    try:
        rows = TF.generate(td.name, "Lahore", "olx")
        bad = [dict(r) for r in rows]
        # Passes the OLX URL pattern but traces to nothing in the trees.
        fake = "https://www.olx.com.pk/fake-town_g9999999/houses_c1721"
        bad[1]["boundary_note"] = f"url:{fake};pages:1"
        errs = TF.validate(bad, trees_dir=td.name)
        assert any("does not trace to any harvested tree entry" in e
                   for e in errs), errs
    finally:
        td.cleanup()
    print("ok  factory: validate() catches an untraceable URL")


def test_factory_quetta_thin_market_flag():
    td = _factory_trees()
    try:
        olx = TF.generate(td.name, "Quetta", "olx")
        zmn = TF.generate(td.name, "Quetta", "zameen")
        # City-level row + 1 town row for OLX; city-level only for Zameen.
        assert len(olx) == 2 and len(zmn) == 1, (len(olx), len(zmn))
        assert all(r.get("thin_market") is True for r in olx + zmn)
        lahore = TF.generate(td.name, "Lahore", "olx")
        assert all("thin_market" not in r for r in lahore)
        assert TF.validate(olx + zmn, trees_dir=td.name) == []
    finally:
        td.cleanup()
    print("ok  factory: thin_market=True iff city is Quetta")


# ======================================================================
# 2. Buyer registry
# ======================================================================

def _buyer_db() -> sqlite3.Connection:
    """In-memory DB with the 10 TRD models + a synthetic buyer-lane fixture."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    posts = [
        # wp1 + wp2: SAME buyer — different contact formats, one phone.
        ("fb:wp1", "facebook", "property_wanted", "Lahore", "DHA Defence",
         15_000_000, "0321 7654321", "buyer_hunt", "https://fb.com/p/1"),
        ("olx:wp2", "olx", "property_wanted", "Lahore", "DHA Defence",
         18_000_000, "+92 321 7654321", "", "https://olx.com/p/2"),
        # wp3 + wp4: same handle, DIFFERENT phones -> must stay separate.
        ("fb:wp3", "facebook", "property_wanted", "Karachi", "Clifton",
         9_000_000, "0300 1111111", "usama", "https://fb.com/p/3"),
        ("fb:wp4", "facebook", "property_wanted", "Karachi", "Clifton",
         9_500_000, "0300 2222222", "usama", "https://fb.com/p/4"),
        # wp5 + wp6: anonymous wanted posts -> one town demand signal.
        ("olx:wp5", "olx", "property_wanted", "Lahore", "Johar Town",
         20_000_000, "", "", "https://olx.com/p/5"),
        ("olx:wp6", "olx", "property_wanted", "Lahore", "Johar Town",
         25_000_000, "", "", "https://olx.com/p/6"),
        # so2: seller-lane post — the registry must never promote it.
        ("olx:so2", "olx", "property_offered", "Lahore", "DHA Defence",
         16_500_000, "0333 9999999", "agency_x", "https://olx.com/p/7"),
        # so3: seller post backing the lead used in get_buyers_for_lead.
        ("olx:so3", "olx", "property_offered", "Lahore", "DHA Defence",
         16_500_000, "0333 0000000", "agency_y", "https://olx.com/p/8"),
    ]
    for pid, plat, intent, city, area, budget, contact, handle, url in posts:
        conn.execute(
            "INSERT INTO post (post_id, platform, author_handle, text_original,"
            " city_tag, area_tag, intent_primary, budget_pkr,"
            " contact_published, url, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, plat, handle or None, f"{intent} {area} {city} PKR {budget}",
             city, area, intent, budget, contact or None, url, NOW_ISO),
        )
    conn.execute(
        "INSERT INTO lead (lead_id, merged_post_ids, intent, city, vertical,"
        " first_seen) VALUES (?,?,?,?,?,?)",
        ("lead-x", json.dumps(["olx:so3"]), "property_offered",
         "Lahore", "property", NOW_ISO),
    )
    conn.commit()
    return conn


def _by_key(buyers, key):
    hits = [b for b in buyers if b["merge_key"] == key]
    assert len(hits) == 1, f"expected exactly 1 buyer for {key}, got {len(hits)}"
    return hits[0]


def test_buyer_same_phone_merges():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    buyers = BR.list_buyers(conn)
    b = _by_key(buyers, "phone:923217654321")
    assert b["anonymous"] is False
    assert b["buyer_id"].startswith("buyer-")
    assert b["activity_count"] == 2
    assert b["budget_min_pkr"] == 15_000_000
    assert b["budget_max_pkr"] == 18_000_000
    towns = {(t["town"], t["city"]) for t in b["towns_of_interest"]}
    assert towns == {("DHA Defence", "Lahore")}
    ev = sum(1 for x in buyers if x["merge_key"] == "phone:923217654321")
    assert ev == 1
    print("ok  buyers: same normalized phone merges to one buyer (15M–18M, act 2)")


def test_buyer_handle_never_merges():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    buyers = BR.list_buyers(conn)
    b3 = _by_key(buyers, "phone:923001111111")
    b4 = _by_key(buyers, "phone:923002222222")
    assert b3["buyer_id"] != b4["buyer_id"], "different phones must not merge"
    for b in (b3, b4):
        handles = {i.get("handle") for i in b["identities"]}
        assert "usama" in handles, "handle recorded as evidence only"
    print("ok  buyers: same handle + different phones stays separate (SHR-9)")


def test_buyer_anonymous_demand_signal():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    buyers = BR.list_buyers(conn)
    d = _by_key(buyers, "demand:lahore|johar town")
    assert d["anonymous"] is True
    assert d["buyer_id"].startswith("demand-")
    assert d["activity_count"] == 2
    assert d["budget_min_pkr"] == 20_000_000
    assert d["budget_max_pkr"] == 25_000_000
    summary = BR.buyer_summary(conn, d["buyer_id"])
    assert len(summary["evidence_posts"]) == 2
    print("ok  buyers: anonymous wanted posts form one demand signal (20M–25M)")


def test_buyer_seller_lane_ignored():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    buyers = BR.list_buyers(conn)
    keys = {b["merge_key"] for b in buyers}
    assert "phone:923339999999" not in keys, "seller-lane phone promoted"
    # The pre-existing phone buyer must not have absorbed a seller post.
    b = _by_key(buyers, "phone:923217654321")
    assert b["activity_count"] == 2
    print("ok  buyers: seller-lane posts never enter the registry")


def test_buyer_derive_idempotent():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    n1 = len(BR.list_buyers(conn))
    snap = {b["buyer_id"]: b["activity_count"] for b in BR.list_buyers(conn)}
    BR.derive_buyers(conn)
    n2 = len(BR.list_buyers(conn))
    assert n1 == n2 == 4, (n1, n2)  # phone buyer + 2 Karachi + 1 demand
    snap2 = {b["buyer_id"]: b["activity_count"] for b in BR.list_buyers(conn)}
    assert snap == snap2, "re-run must not double-count activity"
    print("ok  buyers: derive_buyers is idempotent (4 buyers, activity stable)")


def test_match_buyers_ranking():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    seller = {"town": "DHA Defence", "city": "Lahore",
              "price_pkr": 16_000_000, "property_type": "property"}
    ranked = BR.match_buyers(conn, seller, limit=10)
    assert len(ranked) == 2, f"only Lahore buyers are candidates, got {len(ranked)}"
    top, second = ranked
    assert top["merge_key"] == "phone:923217654321"
    assert second["merge_key"] == "demand:lahore|johar town"
    assert top["match_score"] == 40 + 25 + 10 + 2  # town + budget + recency + activity
    assert top["match_score"] > second["match_score"]
    reasons = " | ".join(top["match_reasons"])
    assert "town match" in reasons and "budget compatible" in reasons
    assert "923217654321" in top["merge_key"]
    # Demand signal matches but never cites an identity.
    assert second["anonymous"] is True
    assert not any("phone" in r for r in second["match_reasons"])
    print("ok  buyers: town+budget-compatible buyer ranks first (77 > 47)")


_MATCH_KEYS = {"buyer_id", "anonymous", "merge_key", "towns_of_interest",
               "budget_min_pkr", "budget_max_pkr", "property_types",
               "activity_count", "last_seen", "source_platforms",
               "identities", "match_score", "match_reasons"}


def test_match_buyers_shape():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    ranked = BR.match_buyers(conn, {"town": "Clifton", "city": "Karachi",
                                    "price_pkr": 9_200_000})
    assert ranked, "Karachi buyers should match"
    for m in ranked:
        assert set(m.keys()) == _MATCH_KEYS, set(m.keys()) ^ _MATCH_KEYS
    scores = [m["match_score"] for m in ranked]
    assert scores == sorted(scores, reverse=True), "must be score-descending"
    print("ok  buyers: match_buyers returns the stable ranked shape")


def test_get_buyers_for_lead():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    ranked = BR.get_buyers_for_lead(conn, "lead-x")
    assert ranked, "lead-x has a usable seller post"
    assert ranked[0]["merge_key"] == "phone:923217654321"
    assert set(ranked[0].keys()) == _MATCH_KEYS
    assert BR.get_buyers_for_lead(conn, "lead-nope") == []
    print("ok  buyers: get_buyers_for_lead ranks for a lead; [] for unknown lead")


def test_buyer_summary_shape():
    conn = _buyer_db()
    BR.derive_buyers(conn)
    buyers = BR.list_buyers(conn)
    s1 = BR.buyer_summary(conn, buyers[0]["buyer_id"])
    s2 = BR.buyer_summary(conn, buyers[1]["buyer_id"])
    expect = {"buyer_id", "identities", "merge_key", "towns_of_interest",
              "budget_min_pkr", "budget_max_pkr", "property_types",
              "activity_count", "first_seen", "last_seen",
              "source_platforms", "anonymous", "evidence_posts"}
    assert set(s1.keys()) == expect and set(s2.keys()) == expect
    assert isinstance(s1["evidence_posts"], list)
    assert BR.buyer_summary(conn, "buyer-nope") is None
    print("ok  buyers: buyer_summary stable shape + evidence; None when unknown")


# ======================================================================
# 3. Yield monitor
# ======================================================================

def _yield_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    YM.ensure_yield_table(conn)
    return conn


def _run(conn, sid, fetched, status="ok", error_class=None):
    return YM.record_run(conn, source_id=sid, fetched=fetched, stored=fetched,
                         status=status, error_class=error_class,
                         ran_at=YM.utcnow_iso())


def test_yield_zero_yield_alarm():
    conn = _yield_db()
    _run(conn, "s-alarm", 20)          # productive history
    for _ in range(3):
        _run(conn, "s-alarm", 0)      # 3 consecutive quiet ok runs
    alarms = YM.check_alarms(conn)
    hit = [a for a in alarms if a["source_id"] == "s-alarm"]
    assert len(hit) == 1, alarms
    a = hit[0]
    assert a["alarm_type"] == "zero_yield_alarm"
    assert len(a["streak"]) == 3
    assert all(r["fetched"] == 0 and r["status"] == "ok" for r in a["streak"])
    assert a["last_run_at"] and a["message"]
    print("ok  yield: 3 zero-yield ok runs -> zero_yield_alarm")


def test_yield_thin_market_candidate_no_alarm():
    conn = _yield_db()
    for _ in range(3):
        _run(conn, "s-thin", 0)       # never yielded anything, ever
    alarms = YM.check_alarms(conn)
    hit = [a for a in alarms if a["source_id"] == "s-thin"]
    assert len(hit) == 1, alarms
    assert hit[0]["alarm_type"] == "thin_market_candidate"
    assert not any(a["alarm_type"] == "zero_yield_alarm" for a in alarms)
    print("ok  yield: never-yielded source -> thin_market_candidate, no alarm")


def test_yield_failed_runs_never_alarm():
    conn = _yield_db()
    _run(conn, "s-fail", 20)          # productive history
    for _ in range(3):
        _run(conn, "s-fail", 0, status="failed", error_class="TimeoutError")
    alarms = YM.check_alarms(conn)
    assert not [a for a in alarms if a["source_id"] == "s-fail"], alarms
    # A lone zero ok run after failures must not alarm either.
    _run(conn, "s-fail", 0)
    assert not [a for a in YM.check_alarms(conn) if a["source_id"] == "s-fail"]
    print("ok  yield: failed runs never trigger a zero-yield alarm")


def test_yield_new_source_skipped():
    conn = _yield_db()
    for _ in range(2):
        _run(conn, "s-new", 0)        # fewer than lookback_runs runs
    assert YM.check_alarms(conn) == []
    # record_run returns a usable run id.
    rid = _run(conn, "s-new", 5)
    assert isinstance(rid, str) and rid.startswith("yield-")
    print("ok  yield: <3 runs -> too new to judge; record_run returns run_id")


def test_yield_summary_shape():
    conn = _yield_db()
    _run(conn, "s-a", 10)
    _run(conn, "s-b", 0)
    s = YM.summary(conn)
    assert "generated_at" in s and isinstance(s["sources"], list)
    expect = {"source_id", "last_fetched", "last_stored", "last_status",
              "last_run_at", "fetched_7d", "stored_7d", "runs_7d"}
    for src in s["sources"]:
        assert set(src.keys()) == expect, set(src.keys()) ^ expect
    by_id = {x["source_id"]: x for x in s["sources"]}
    assert by_id["s-a"]["last_fetched"] == 10 and by_id["s-b"]["runs_7d"] == 1
    print("ok  yield: summary() returns the documented shape")


# ======================================================================
# 4. Retention contract (self-skipping until engine/retention.py lands)
# ======================================================================

_RETENTION_ABSENT = "SKIP retention: engine/retention.py not implemented yet"


def _load_retention():
    """Lazy import so the rest of the suite runs without the module."""
    try:
        from engine import retention as R  # noqa: E402
        return R
    except ImportError:
        return None


def _retention_db() -> sqlite3.Connection:
    """Temp DB with old (>180d) and fresh rows across dependent tables."""
    from engine import buyer_registry as _BR  # local: only needed here
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    _BR.ensure_buyer_schema(conn)
    YM.ensure_yield_table(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS price_snapshots"
                 " (snapshot_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
    old = (NOW - datetime.timedelta(days=200)).isoformat()
    fresh = (NOW - datetime.timedelta(days=10)).isoformat()
    # Exempt rows: a source config row and a buyer (derived demand).
    conn.execute(
        "INSERT INTO source (source_id, platform, access_route, display_name,"
        " created_at) VALUES (?,?,?,?,?)",
        ("src-keep", "olx", "web_public", "keep me", fresh),
    )
    conn.execute(
        "INSERT INTO buyer (buyer_id, merge_key, identities, towns_of_interest,"
        " first_seen, last_seen, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("buyer-keep", "phone:923001234567", "[]", "[]", old, old, old, old),
    )
    # Posts: one stale, one fresh.
    for pid, seen in (("web:old1", old), ("web:new1", fresh)):
        conn.execute(
            "INSERT INTO post (post_id, platform, text_original, url, seen_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "olx", "listing", f"https://x/{pid}", seen),
        )
    # lead-old: stale AND fully stale -> purgeable (with its score).
    conn.execute(
        "INSERT INTO lead (lead_id, merged_post_ids, intent, city, first_seen)"
        " VALUES (?,?,?,?,?)",
        ("lead-old", json.dumps(["web:old1"]), "property_offered",
         "Lahore", old),
    )
    conn.execute(
        "INSERT INTO lead_score (lead_id, computed_at, scoring_config_version)"
        " VALUES (?,?,?)",
        ("lead-old", old, "v1"),
    )
    # lead-mixed: stale but still references a FRESH post -> kept.
    conn.execute(
        "INSERT INTO lead (lead_id, merged_post_ids, intent, city, first_seen)"
        " VALUES (?,?,?,?,?)",
        ("lead-mixed", json.dumps(["web:old1", "web:new1"]),
         "property_offered", "Lahore", old),
    )
    conn.execute(
        "INSERT INTO lead_score (lead_id, computed_at, scoring_config_version)"
        " VALUES (?,?,?)",
        ("lead-mixed", old, "v1"),
    )
    # lead-fresh: recent -> kept.
    conn.execute(
        "INSERT INTO lead (lead_id, merged_post_ids, intent, city, first_seen)"
        " VALUES (?,?,?,?,?)",
        ("lead-fresh", json.dumps(["web:new1"]), "property_offered",
         "Lahore", fresh),
    )
    conn.execute(
        "INSERT INTO lead_score (lead_id, computed_at, scoring_config_version)"
        " VALUES (?,?,?)",
        ("lead-fresh", fresh, "v1"),
    )
    # Comments: one on the stale post, one on the fresh post.
    conn.execute(
        "INSERT INTO comment (comment_id, post_id, text, seen_at)"
        " VALUES (?,?,?,?)",
        ("c-old", "web:old1", "nice", old),
    )
    conn.execute(
        "INSERT INTO comment (comment_id, post_id, text, seen_at)"
        " VALUES (?,?,?,?)",
        ("c-new", "web:new1", "also nice", fresh),
    )
    # Sibling snapshot tables: one stale row each, one fresh each.
    conn.execute("INSERT INTO price_snapshots VALUES (?,?)", ("snap-old", old))
    conn.execute("INSERT INTO price_snapshots VALUES (?,?)", ("snap-new", fresh))
    YM.record_run(conn, "s-old", 5, 5, "ok", ran_at=old)
    YM.record_run(conn, "s-new", 5, 5, "ok", ran_at=fresh)
    conn.commit()
    return conn


def _counts(conn):
    tables = ["source", "buyer", "post", "comment", "lead", "lead_score",
              "price_snapshots", "source_yield"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables}


def test_retention_dry_run_deletes_nothing():
    R = _load_retention()
    if R is None:
        print(_RETENTION_ABSENT + " (dry_run contract pending)")
        return
    conn = _retention_db()
    before = _counts(conn)
    # retention_report is read-only and agrees with the purge plan.
    rep = R.retention_report(conn, days=180)
    assert rep["days"] == 180
    assert rep["tables"]["post"]["count_old"] == 1
    assert rep["tables"]["post"]["count_total"] == 2
    # purge(dry_run=True) plans without touching anything.
    plan = R.purge(conn, days=180, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["would_delete"]["post"]["count"] == 1
    assert plan["would_delete"]["post"]["sample_ids"] == ["web:old1"]
    assert plan["would_delete"]["lead"]["count"] == 1
    assert "source" in plan["protected_tables_kept"]
    assert "buyer" in plan["protected_tables_kept"]
    assert _counts(conn) == before, "dry_run must not delete anything"
    print("ok  retention: report + dry_run plan without deleting anything")


def test_retention_purge_old_rows_dependency_order():
    R = _load_retention()
    if R is None:
        print(_RETENTION_ABSENT + " (purge contract pending)")
        return
    conn = _retention_db()
    res = R.purge(conn, days=180, dry_run=False, confirm=True)
    assert res["dry_run"] is False
    deleted = res["deleted"]
    # Only the fully-stale chain is deleted, children before parents.
    assert deleted["lead_score"] == 1, deleted
    assert deleted["lead"] == 1
    assert deleted["comment"] == 1
    assert deleted["price_snapshots"] == 1
    assert deleted["post"] == 1
    assert deleted["source_yield"] == 1
    after = _counts(conn)
    assert after["post"] == 1 and after["lead"] == 2  # lead-mixed + lead-fresh
    assert after["lead_score"] == 2 and after["comment"] == 1
    assert after["price_snapshots"] == 1 and after["source_yield"] == 1
    posts = {r[0] for r in conn.execute("SELECT post_id FROM post")}
    assert posts == {"web:new1"}, posts
    leads = {r[0] for r in conn.execute("SELECT lead_id FROM lead")}
    assert leads == {"lead-mixed", "lead-fresh"}, leads
    # Protected tables survive wholesale.
    assert after["source"] == 1, "source rows are exempt"
    assert after["buyer"] == 1, "buyer (derived demand) is exempt"
    print("ok  retention: real purge deletes only >180d rows, exempt tables survive")


def test_retention_purge_refuses_without_confirm():
    R = _load_retention()
    if R is None:
        print(_RETENTION_ABSENT + " (confirm-guard contract pending)")
        return
    conn = _retention_db()
    before = _counts(conn)
    try:
        R.purge(conn, days=180, dry_run=False)  # confirm defaults to False
    except R.RetentionSafetyError:
        pass
    else:
        raise AssertionError("purge(dry_run=False) without confirm=True"
                             " must raise RetentionSafetyError")
    assert _counts(conn) == before, "refused purge must delete nothing"
    print("ok  retention: purge without confirm=True raises and deletes nothing")


if __name__ == "__main__":
    test_factory_generate_deterministic()
    test_factory_grammar_pages_and_dedup()
    test_factory_backfill_override()
    test_factory_validate_bad_source_id()
    test_factory_validate_untraceable_url()
    test_factory_quetta_thin_market_flag()
    test_buyer_same_phone_merges()
    test_buyer_handle_never_merges()
    test_buyer_anonymous_demand_signal()
    test_buyer_seller_lane_ignored()
    test_buyer_derive_idempotent()
    test_match_buyers_ranking()
    test_match_buyers_shape()
    test_get_buyers_for_lead()
    test_buyer_summary_shape()
    test_yield_zero_yield_alarm()
    test_yield_thin_market_candidate_no_alarm()
    test_yield_failed_runs_never_alarm()
    test_yield_new_source_skipped()
    test_yield_summary_shape()
    test_retention_dry_run_deletes_nothing()
    test_retention_purge_old_rows_dependency_order()
    test_retention_purge_refuses_without_confirm()
    print("\nALL TESTS PASSED")
