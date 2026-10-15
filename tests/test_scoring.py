#!/usr/bin/env python3
"""
Scoring-pipeline tests (Wave-B B37) — runnable with the stdlib only:

    python tests/test_scoring.py

Covers the full scoring pipeline:
  1. rental-cap rule (engine/scorer.py, B26)
  2. batch parity: score_leads_batch == score_lead per lead (engine/scoring_batch.py)
  3. delta scoring: re-score only changed leads, others byte-identical
  4. price history snapshots + drop detector (engine/price_history.py)
  5. comparator price-envelope / area / intent / tenure gates (engine/comparator.py)

Conventions match tests/test_engine.py: plain assert functions, inline
synthetic fixtures, no network, DB fixtures only on TEMP files under /tmp
(the live DB is never touched).
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_scoring_config  # noqa: E402
from engine.comparator import Comparator  # noqa: E402
from engine.price_history import (  # noqa: E402
    first_seen_price,
    price_drop,
    snapshot,
)
from engine.scorer import score_lead  # noqa: E402
from engine.scoring_batch import delta_scoring, score_leads_batch  # noqa: E402

BANK_CFG = load_scoring_config()
NOW = datetime.datetime(2026, 10, 15, 13, 45, 0, tzinfo=datetime.timezone.utc)
HOT = BANK_CFG["hot_threshold"]          # 70
CAP = BANK_CFG.get("rental_score_cap") or 64  # config predates the key -> fallback


def _base_hot():
    """The PRD §8 AC-M4-1 hot fixture (scores exactly 93, no post_texts)."""
    return {
        "intent": "seeking_service", "category_weight": 8,
        "first_seen": NOW - datetime.timedelta(hours=2),
        "budget_pkr": 5000, "budget_phrase_matched": False,
        "urgency_count": 1, "platform_count": 1, "contact_type": "phone",
        "price_inquiry": False, "serious_buyer": False,
    }


# ----------------------------------------------------------------------
# 1. Rental-cap rule (B26)
def test_rental_cap_pure_rental():
    # Pure rental phrasing on an otherwise-hot fixture -> capped at 64, NOT
    # hot. (urgency_count=0, no published contact: nothing that would lift
    # the cap via the urgency override.)
    lead = _base_hot()
    lead["urgency_count"] = 0
    lead["contact_type"] = "none"
    lead["post_texts"] = [
        "2 bed flat for rent in DHA Lahore, monthly rent 85000, kiraye par available"
    ]
    uncapped = score_lead({k: v for k, v in lead.items() if k != "post_texts"},
                          BANK_CFG, NOW)
    assert uncapped["total"] > CAP, \
        f"fixture must score above the cap without the rule, got {uncapped['total']}"
    s = score_lead(lead, BANK_CFG, NOW)
    assert s["total"] == CAP, f"expected cap {CAP}, got {s['total']}"
    assert s["total"] < HOT
    assert s["hot_flag"] is False, f"capped lead must not be HOT: {s}"
    assert s["hot_trigger"] is None
    assert s["rental_cap"]["applied"] is True
    assert s["rental_cap"]["pre_cap_total"] == uncapped["total"]
    assert s["rental_cap"]["overridden"] is False
    print("ok  rental cap: pure-rental fixture capped to 64, not HOT")


def test_rental_cap_sale_unaffected():
    # Sale phrasing -> the scorer runs exactly as before the cap existed:
    # byte-identical score dict to the same fixture without post_texts.
    with_texts = _base_hot()
    with_texts["post_texts"] = [
        "5 marla house for sale in G-13 Islamabad, demand 2.9 crore"
    ]
    expected = score_lead(_base_hot(), BANK_CFG, NOW)
    s = score_lead(with_texts, BANK_CFG, NOW)
    assert s == expected, "sale fixture must be byte-identical to pre-cap expectation"
    assert s["total"] == 93 and s["hot_flag"] is True
    assert "rental_cap" not in s
    print("ok  rental cap: sale fixture byte-identical to pre-cap expectation")


def test_rental_cap_urgent_phone_override():
    # Urgency marker + published phone = motivated lessor -> override keeps HOT.
    lead = _base_hot()
    lead["post_texts"] = [
        "URGENT: 2 bed flat for rent in DHA Lahore, monthly rent 85000. "
        "Call now 03215550123, foran rabta karein"
    ]
    s = score_lead(lead, BANK_CFG, NOW)
    assert s["total"] == 93, f"override must keep the uncapped total, got {s['total']}"
    assert s["hot_flag"] is True and s["hot_trigger"] is not None, \
        f"override must keep HOT: {s}"
    assert s["rental_cap"]["applied"] is False
    assert s["rental_cap"]["overridden"] is True
    assert s["rental_cap"]["reason"] == "urgency_with_published_contact"
    print("ok  rental cap: urgent+rental+phone override keeps HOT")


def test_rental_cap_urgent_no_phone_stays_capped():
    # Urgency WITHOUT a published contact is noise, not a hot lead.
    lead = _base_hot()
    lead["contact_type"] = "inbox"
    lead["post_texts"] = [
        "urgent: 2 bed flat for rent in DHA Lahore, monthly rent 85000"
    ]
    s = score_lead(lead, BANK_CFG, NOW)
    assert s["total"] == CAP, f"expected cap {CAP}, got {s['total']}"
    assert s["hot_flag"] is False
    assert s["rental_cap"]["applied"] is True
    assert s["rental_cap"]["overridden"] is False
    print("ok  rental cap: urgent rental without phone stays capped")


def test_rental_cap_mixed_sale_or_rent_unaffected():
    # Both rental AND sale markers -> ambiguous, left to the normal scorer.
    lead = _base_hot()
    lead["post_texts"] = [
        "House available for rent or sale in B-17, rent par bhi milega, "
        "sale k liye demand 2.5 crore"
    ]
    expected = score_lead(_base_hot(), BANK_CFG, NOW)
    s = score_lead(lead, BANK_CFG, NOW)
    assert s == expected, "mixed sale-or-rent fixture must score uncapped"
    assert "rental_cap" not in s
    print("ok  rental cap: mixed sale-or-rent fixture unaffected")


# ----------------------------------------------------------------------
# 2. Batch parity
def _synthetic_leads(n=20):
    intents = ["seeking_service", "property_offered", "hiring",
               "seeking_product", "complaint_pain", "job_seeking"]
    contacts = ["phone", "whatsapp", "open_thread", "inbox", "none"]
    leads = []
    for i in range(n):
        lead = {
            "lead_id": f"par-{i:02d}",
            "intent": intents[i % len(intents)],
            "category_weight": 3 + (i % 8),
            "first_seen": (NOW - datetime.timedelta(hours=i * 7)).isoformat(),
            "budget_pkr": 100000 * i if i % 3 == 0 else None,
            "budget_phrase_matched": i % 3 == 1,
            "urgency_count": i % 4,
            "platform_count": 1 + (i % 3),
            "contact_type": contacts[i % len(contacts)],
            "price_inquiry": i % 5 == 0,
            "serious_buyer": i % 6 == 0,
            "replacement_demand": i % 6 == 4,
            # Neutral sale-side texts: never rental markers, so the rental
            # cap stays out of both implementations equally.
            "post_texts": ["5 marla house for sale, demand 2.5 crore, "
                           "call 03215550123"],
        }
        if lead["intent"] == "hiring":
            lead["stated_deadline"] = (NOW + datetime.timedelta(days=3)).isoformat()
        leads.append(lead)
    return leads


def test_batch_parity():
    # 20 synthetic leads: batch results == individual score_lead for every lead.
    leads = _synthetic_leads(20)
    # cleared_complaint exercised per-lead via the mapping form.
    flags = {f"par-{i:02d}": (i % 2 == 0) for i in range(20)}
    batch = score_leads_batch(leads, BANK_CFG, now=NOW, cleared_complaint=flags)
    assert len(batch) == len(leads) == 20, "batch output must be 1:1 aligned"
    for lead in leads:
        individual = score_lead(lead, BANK_CFG, NOW,
                                cleared_complaint=flags[lead["lead_id"]])
        got = batch[leads.index(lead)]
        assert got == individual, (
            f"parity failed for {lead['lead_id']}:\n batch={got}\n indiv={individual}")
    print("ok  batch parity: 20/20 score_leads_batch == score_lead")


def test_batch_parity_scalar_flag():
    # Scalar True/False cleared_complaint also matches per-lead calls.
    leads = _synthetic_leads(20)
    for flag in (True, False):
        batch = score_leads_batch(leads, BANK_CFG, now=NOW, cleared_complaint=flag)
        for lead, got in zip(leads, batch):
            assert got == score_lead(lead, BANK_CFG, NOW, cleared_complaint=flag), \
                f"parity failed for {lead['lead_id']} with flag={flag}"
    print("ok  batch parity: scalar cleared_complaint True/False both match")


# ----------------------------------------------------------------------
# 3. Delta scoring
def test_delta_scoring():
    leads = _synthetic_leads(20)
    scored = {l["lead_id"]: score_lead(l, BANK_CFG, NOW) for l in leads}
    by_id = {l["lead_id"]: dict(l) for l in leads}

    # Change 2 leads: contact upgrade (inbox -> phone) on par-03,
    # urgency + budget on par-17 (both move the totals).
    changed = ["par-03", "par-17"]
    by_id["par-03"]["contact_type"] = "phone"
    by_id["par-17"]["urgency_count"] = 2
    by_id["par-17"]["budget_pkr"] = 2_500_000

    merged = delta_scoring(scored, by_id, changed, BANK_CFG, now=NOW)
    assert set(merged) == set(scored), "merged mapping must keep all lead_ids"

    # Untouched leads: byte-identical to their previous score records.
    for lid, prev in scored.items():
        if lid not in changed:
            assert merged[lid] == prev, f"{lid} should be byte-identical"
            assert merged[lid] is prev, f"{lid} should be the same score object"

    # Changed leads: exactly what a fresh score_lead gives.
    for lid in changed:
        expected = score_lead(by_id[lid], BANK_CFG, NOW)
        assert merged[lid] == expected, f"{lid} re-score mismatch"
    for lid in changed:
        assert merged[lid]["total"] != scored[lid]["total"], \
            f"changed lead {lid} must actually move"
    print("ok  delta scoring: 2 re-scored, 18 byte-identical")


def test_delta_scoring_skips_unknown():
    # Unknown / stale changed_ids are skipped, never an exception.
    leads = _synthetic_leads(5)
    scored = {l["lead_id"]: score_lead(l, BANK_CFG, NOW) for l in leads}
    by_id = {l["lead_id"]: l for l in leads}
    merged = delta_scoring(scored, by_id, ["par-99", "par-00"], BANK_CFG, now=NOW)
    assert set(merged) == set(scored)
    assert merged["par-00"] == score_lead(by_id["par-00"], BANK_CFG, NOW)
    print("ok  delta scoring: unknown changed_ids skipped cleanly")


# ----------------------------------------------------------------------
# 4. Price history (TEMP db only — never the live DB)
def _tmp_conn():
    fd, path = tempfile.mkstemp(prefix="b37_price_", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # price_drop reads rows by column name
    return conn, path


def test_price_history_snapshots():
    conn, path = _tmp_conn()
    try:
        t0 = NOW - datetime.timedelta(days=3)
        assert snapshot(conn, "olx:111", 100_000_000, t0) is True
        # Missing price is data absence, not a zero-price drop.
        assert snapshot(conn, "olx:112", None, t0) is False
        assert first_seen_price(conn, "olx:111") == 100_000_000
        assert first_seen_price(conn, "olx:112") is None
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM price_snapshots").fetchone()["n"]
        assert rows == 1, f"only the priced snapshot should persist, got {rows}"
    finally:
        conn.close()
        os.unlink(path)
    print("ok  price history: snapshot records; missing prices skipped")


def test_price_drop_fires():
    # 100M -> 85M is a 15% drop -> fires (>=10% threshold).
    conn, path = _tmp_conn()
    try:
        t0 = NOW - datetime.timedelta(days=10)
        t1 = NOW - datetime.timedelta(days=1)
        snapshot(conn, "olx:200", 100_000_000, t0)
        snapshot(conn, "olx:200", 85_000_000, t1)
        ev = price_drop(conn, "olx:200", drop_pct=10.0, window_days=30)
        assert ev is not None, "15% drop should fire"
        assert ev["first_price"] == 100_000_000
        assert ev["last_price"] == 85_000_000
        assert ev["drop_pct"] == 15.0, f"got {ev['drop_pct']}"
        assert ev["first_seen"] <= ev["last_seen"]
        # Single snapshot: no history to compare -> None.
        snapshot(conn, "olx:201", 50_000_000, t1)
        assert price_drop(conn, "olx:201") is None
    finally:
        conn.close()
        os.unlink(path)
    print("ok  price history: 100M->85M fires at 15% drop")


def test_price_drop_recovery_does_not_refire():
    # Drop then recover: latest price is no longer the window minimum.
    conn, path = _tmp_conn()
    try:
        t0 = NOW - datetime.timedelta(days=10)
        t1 = NOW - datetime.timedelta(days=5)
        t2 = NOW - datetime.timedelta(days=1)
        snapshot(conn, "olx:300", 100_000_000, t0)
        snapshot(conn, "olx:300", 85_000_000, t1)
        mid = price_drop(conn, "olx:300")
        assert mid is not None and mid["drop_pct"] == 15.0
        snapshot(conn, "olx:300", 100_000_000, t2)
        assert price_drop(conn, "olx:300") is None, \
            "recovered price must not re-fire"
    finally:
        conn.close()
        os.unlink(path)
    print("ok  price history: drop-then-recover does not re-fire")


def test_price_drop_small_drop_silent():
    # 100M -> 95M is a 5% dip -> below the 10% threshold -> silent.
    conn, path = _tmp_conn()
    try:
        snapshot(conn, "olx:400", 100_000_000, NOW - datetime.timedelta(days=5))
        snapshot(conn, "olx:400", 95_000_000, NOW - datetime.timedelta(days=1))
        assert price_drop(conn, "olx:400", drop_pct=10.0) is None
    finally:
        conn.close()
        os.unlink(path)
    print("ok  price history: 5% dip stays silent under the 10% threshold")


# ----------------------------------------------------------------------
# 5. Comparator gates
def _post(pid, text, price=None, intent="selling", area=None, phone=None,
          hours_ago=1):
    seen = NOW - datetime.timedelta(hours=hours_ago)
    return {
        "post_id": f"olx:{pid}",
        "platform_post_id": pid,
        "contact_published": phone,
        "text_original": text,
        "text_normalized": text.lower(),
        "published_at": seen.isoformat(),
        "seen_at": seen.isoformat(),
        "city_tag": "Islamabad",
        "intent_primary": intent,
        "budget_pkr": price,
        "area_tag": area,
    }


def _lead(lid, posts, intent="selling"):
    return {
        "lead_id": lid,
        "city": "Islamabad",
        "intent": intent,
        "merged_posts": posts,
        "contact_published": None,
        "first_seen": NOW.isoformat(),
    }


def _title(price_cr):
    # No size token here: without marla/kanal/sqft the post reads as family
    # "other" (20% envelope), which is the lane where chain-walking can be
    # demonstrated step by step.
    return (f"House for sale in G-13 Islamabad, demand {price_cr} crore, "
            f"park facing unit near market")


def test_comparator_chain_walk_envelope():
    # Family "other" (no size token -> no property-content detection) uses
    # the 20% envelope. Posts at 2.90 / 3.30 Cr merge (12.1% spread), but a
    # 3.90 Cr post must NOT chain-walk onto the lead: pairwise vs 3.30 it
    # would pass (15.4% <= 20%), yet the lead-wide spread is 25.6% > 20%.
    comp = Comparator(BANK_CFG)
    p1 = _post("a1", _title("2.90"), price=29_000_000)
    p2 = _post("a2", _title("3.30"), price=33_000_000, hours_ago=2)
    lead = _lead("chain-1", [p1])
    match, _ = comp.find_cluster(p2, [lead])
    assert match is not None, "2.90 + 3.30 must merge (12.1% <= 20%)"
    lead["merged_posts"].append(p2)

    p3 = _post("a3", _title("3.90"), price=39_000_000, hours_ago=3)
    match, expl = comp.find_cluster(p3, [lead])
    assert match is None, f"3.90 must NOT chain-walk onto the lead: {expl}"

    # And the literal 2.9 -> 4.65 Cr jump never merges.
    p4 = _post("a4", _title("4.65"), price=46_500_000, hours_ago=4)
    match, _ = comp.find_cluster(p4, [lead])
    assert match is None, "4.65 Cr must not merge onto a 2.90-3.30 lead"
    print("ok  comparator: chain-walk price envelope vetoes 2.9->4.65 Cr")


def test_comparator_different_area_stays_separate():
    # Same price, same city, near-identical template text — but explicitly
    # different areas veto the merge (property identity tier).
    comp = Comparator(BANK_CFG)
    t1 = ("5 Marla House for sale in G-13, 5 marla, demand 2.50 crore, "
          "park facing, call 03215550123")
    t2 = ("5 Marla House for sale in DHA Defence, 5 marla, demand 2.50 crore, "
          "park facing, call 03215550123")
    p1 = _post("b1", t1, price=25_000_000, area="G-13")
    p2 = _post("b2", t2, price=25_000_000, area="DHA Defence")
    lead = _lead("area-1", [p1], intent="property_offered")
    lead["intent"] = "selling"
    p1["intent_primary"] = "selling"
    p2["intent_primary"] = "selling"
    match, expl = comp.find_cluster(p2, [lead])
    assert match is None, f"different explicit areas must not merge: {expl}"
    print("ok  comparator: different-area same-price posts stay separate")


def test_comparator_same_phone_cross_intent_merges():
    # Tier 2 phone hard key is intent-agnostic: the same published number
    # in-window merges across intent labels (B28 tweak 4).
    comp = Comparator(BANK_CFG)
    p1 = _post("c1", "need electrician dha phase 5 lahore, call 03331122334",
               intent="seeking_service", phone="03331122334")
    lead = _lead("phone-1", [p1], intent="seeking_service")
    lead["city"] = "Islamabad"
    p1["city_tag"] = "Islamabad"
    p2 = _post("c2", "electrician available lahore, contact 03331122334",
               intent="freelance_offering", phone="03331122334", hours_ago=3)
    p2["city_tag"] = "Islamabad"
    match, expl = comp.find_cluster(p2, [lead])
    assert match is not None and match["lead_id"] == "phone-1", \
        f"same-phone cross-intent posts must merge: {expl}"
    assert "03331122334" in expl
    print("ok  comparator: same-phone cross-intent posts merge")


def test_comparator_rent_vs_sale_vetoed():
    # Same price, same area, same city, near-identical titles — but one
    # side is rent and the other is sale: vetoed. Control pair (sale/sale)
    # merges, proving the veto is what blocks the rent side.
    comp = Comparator(BANK_CFG)
    # Titles are near-identical except the tenure word, so the title
    # Jaccard clears 0.75 and the ONLY thing stopping the merge is the
    # rent-vs-sale veto.
    shared = ("with attached bathrooms, drawing dining, tv lounge, mumty, "
              "solid land, near market and mosque, ideal investment")
    rent_t = (f"5 Marla House available for rent in G-13 Islamabad, 5 marla, "
              f"monthly kiraya 90000, park facing, brand new, {shared}")
    sale_t = (f"5 Marla House available for sale in G-13 Islamabad, 5 marla, "
              f"demand 2.50 crore, park facing, brand new, {shared}")
    rent_p = _post("d1", rent_t, price=25_000_000, area="G-13",
                   intent="property_offered")
    sale_p = _post("d2", sale_t, price=25_000_000, area="G-13",
                   intent="property_offered")
    lead = _lead("tenure-1", [sale_p], intent="property_offered")
    match, expl = comp.find_cluster(rent_p, [lead])
    assert match is None, f"rent-vs-sale pair must be vetoed: {expl}"

    # Control: another sale post with the same template DOES merge.
    sale_p2 = _post("d3", sale_t, price=25_000_000, area="G-13",
                    intent="property_offered")
    match, _ = comp.find_cluster(sale_p2, [lead])
    assert match is not None and match["lead_id"] == "tenure-1", \
        "sale/sale control pair must merge"
    print("ok  comparator: rent-vs-sale pair vetoed, sale/sale merges")


# ----------------------------------------------------------------------
if __name__ == "__main__":
    test_rental_cap_pure_rental()
    test_rental_cap_sale_unaffected()
    test_rental_cap_urgent_phone_override()
    test_rental_cap_urgent_no_phone_stays_capped()
    test_rental_cap_mixed_sale_or_rent_unaffected()
    test_batch_parity()
    test_batch_parity_scalar_flag()
    test_delta_scoring()
    test_delta_scoring_skips_unknown()
    test_price_history_snapshots()
    test_price_drop_fires()
    test_price_drop_recovery_does_not_refire()
    test_price_drop_small_drop_silent()
    test_comparator_chain_walk_envelope()
    test_comparator_different_area_stays_separate()
    test_comparator_same_phone_cross_intent_merges()
    test_comparator_rent_vs_sale_vetoed()
    print("\nALL TESTS PASSED")
