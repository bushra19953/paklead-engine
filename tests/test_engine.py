#!/usr/bin/env python3
"""
Engine tests — runnable with the stdlib only:

    python tests/test_engine.py

Covers: classifier (10 sample posts), budget parser, scorer (PRD §8
worked examples reproduced exactly), complaint hot trigger, phone-key
dedupe, loophole ratio/underserved flags, scam quarantine, and the
banned-symbols static check (TRD NFR-PK-8).
"""

from __future__ import annotations

import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_phrase_bank, load_scoring_config  # noqa: E402
from engine import classifier as C  # noqa: E402
from engine import loophole as L  # noqa: E402
from engine.comparator import Comparator  # noqa: E402
from engine.scorer import score_lead  # noqa: E402

BANK = load_phrase_bank()
CFG = load_scoring_config()
NOW = datetime.datetime(2026, 10, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)
SAMPLES = (ROOT / "sample_data" / "sample_posts.json")


def _posts():
    import json
    return {p["platform_post_id"]: p["text_original"]
            for p in json.loads(SAMPLES.read_text(encoding="utf-8"))["posts"]}


# ----------------------------------------------------------------------
def test_classifier_sample_posts():
    posts = _posts()
    expected = {
        "fb-1001": "seeking_service", "rd-2001": "seeking_product",
        "fb-1002": "complaint_pain", "im-3001": "property_wanted",
        "rd-2003": "seeking_service", "fb-1005": "hiring",
        "im-3002": "freelance_seeking", "fb-1006": "property_offered",
        "fb-1007": "selling", "fb-1008": "job_seeking",
        "rd-2004": "seeking_service",
    }
    for pid, want in expected.items():
        got = C.classify_post(posts[pid], BANK)["intent_primary"]
        assert got == want, f"{pid}: expected {want}, got {got}"
    # cities
    assert C.classify_post(posts["fb-1001"], BANK)["city_tag"] == "Lahore"
    assert C.classify_post(posts["rd-2003"], BANK)["city_tag"] == "Karachi"
    assert C.classify_post(posts["rd-2004"], BANK)["city_tag"] == "Islamabad"
    # languages
    assert C.classify_post(posts["rd-2003"], BANK)["language_mode"] == "urdu"
    assert C.classify_post(posts["fb-1001"], BANK)["language_mode"] == "roman_urdu"
    assert C.classify_post(posts["rd-2004"], BANK)["language_mode"] == "en"
    # phone normalization
    assert C.classify_post(posts["fb-1001"], BANK)["contact_published"] == "03217654321"
    # serious-buyer cultural marker
    assert C.classify_post(posts["fb-1007"], BANK)["serious_buyer"] is True
    print("ok  classifier: 11 sample posts categorized correctly")


def test_budget_parser():
    assert C.extract_budget("1.5 lac", BANK)["budget_pkr"] == 150000
    assert C.extract_budget("50k", BANK)["budget_pkr"] == 50000
    assert C.extract_budget("demand 2.5 crore", BANK)["budget_pkr"] == 25000000
    assert C.extract_budget("Rs 45,000", BANK)["budget_pkr"] == 45000
    assert C.extract_budget("budget 30k", BANK)["budget_pkr"] == 30000
    assert C.extract_budget("no money mentioned", BANK)["budget_pkr"] is None
    print("ok  budget parser: lac/k/crore/Rs normalization")


def test_scorer_prd_worked_examples():
    # PRD §8 AC-M4-1: hot example → exactly 93
    hot = {
        "intent": "seeking_service", "category_weight": 8,
        "first_seen": NOW - datetime.timedelta(hours=2),
        "budget_pkr": 5000, "budget_phrase_matched": False,
        "urgency_count": 1, "platform_count": 1, "contact_type": "phone",
        "price_inquiry": False, "serious_buyer": False,
    }
    s1 = score_lead(hot, CFG, NOW)
    assert s1["total"] == 93, f"hot example: expected 93, got {s1['total']} {s1}"
    assert s1["hot_flag"] is True
    # PRD §8 AC-M4-2: warm example → exactly 47
    warm = {
        "intent": "seeking_product", "category_weight": 7,
        "first_seen": NOW - datetime.timedelta(days=5),
        "budget_pkr": None, "budget_phrase_matched": False,
        "urgency_count": 0, "platform_count": 1, "contact_type": "none",
        "price_inquiry": False, "serious_buyer": False,
    }
    s2 = score_lead(warm, CFG, NOW)
    assert s2["total"] == 47, f"warm example: expected 47, got {s2['total']} {s2}"
    assert s2["hot_flag"] is False
    assert s1["total"] > s2["total"]
    print("ok  scorer: PRD worked examples 93 (hot) > 47 (warm), exact")


def test_complaint_hot_trigger():
    feats = {
        "intent": "complaint_pain", "category_weight": 5,
        "first_seen": NOW - datetime.timedelta(hours=1),
        "budget_pkr": None, "budget_phrase_matched": False,
        "urgency_count": 1, "platform_count": 1, "contact_type": "none",
        "replacement_demand": True,
    }
    s = score_lead(feats, CFG, NOW, cleared_complaint=True)
    assert s["hot_flag"] is True
    assert s["hot_trigger"] == "complaint_with_replacement_urgency"
    print("ok  scorer: complaint-with-replacement-urgency trigger fires")


def test_phone_key_dedupe():
    comp = Comparator(CFG)
    lead = {"lead_id": "lead-1", "city": "Lahore", "intent": "seeking_service",
            "merged_posts": [{
                "post_id": "reddit:rd-2002", "contact_published": "03331122334",
                "text_normalized": "need electrician dha phase 5 lahore",
                "published_at": NOW.isoformat(), "seen_at": NOW.isoformat(),
                "city_tag": "Lahore", "intent_primary": "seeking_service"}]}
    post = {"post_id": "facebook:fb-1004", "contact_published": "03331122334",
            "text_normalized": "electrician chahiye dha phase 5 lahore",
            "published_at": (NOW + datetime.timedelta(hours=2)).isoformat(),
            "seen_at": (NOW + datetime.timedelta(hours=2)).isoformat(),
            "city_tag": "Lahore", "intent_primary": "seeking_service"}
    match, expl = comp.find_cluster(post, [lead])
    assert match is not None and match["lead_id"] == "lead-1"
    assert "published number 03331122334" in expl
    # city mismatch veto (PRD AC-M5-2)
    post2 = dict(post, city_tag="Karachi", contact_published=None,
                 text_normalized="electrician chahiye gulshan karachi")
    match2, _ = comp.find_cluster(post2, [lead])
    assert match2 is None
    print("ok  comparator: phone hard-key merge + city veto")


def test_loophole_engine():
    leads = [{"lead_id": f"s{i}", "intent": "seeking_service", "city": "Multan",
              "vertical": "services", "status": "new"} for i in range(12)]
    leads += [{"lead_id": f"p{i}", "intent": "freelance_offering", "city": "Multan",
               "vertical": "services", "status": "new"} for i in range(2)]
    posts = [{"post_id": f"s{i}", "intent_primary": "seeking_service",
              "city_tag": "Multan", "vertical": "services",
              "text_original": "ac repair wala chahiye multan",
              "budget_pkr": None} for i in range(12)]
    reps = L.detect_loopholes(leads, posts, [], CFG, NOW)
    gaps = [r for r in reps if r["report_type"] == "ratio_gap"
            and r["city"] == "Multan"]
    assert gaps and gaps[0]["ratio"] == 6.0, f"expected 6.0 ratio, got {gaps}"
    # underserved city: 14 seekers, 0 provider replies
    posts2 = [{"post_id": f"u{i}", "intent_primary": "seeking_service",
               "city_tag": "Sialkot", "vertical": "services",
               "text_original": "plumber chahiye sialkot",
               "budget_pkr": None} for i in range(14)]
    leads2 = [{"lead_id": f"u{i}", "intent": "seeking_service", "city": "Sialkot",
               "vertical": "services", "status": "new"} for i in range(14)]
    reps2 = L.detect_loopholes(leads2, posts2, [], CFG, NOW)
    assert any(r["report_type"] == "underserved_city" and r["city"] == "Sialkot"
               for r in reps2), "underserved-city flag missing"
    print("ok  loophole: ratio 6.0 flagged; underserved city flagged")


def test_quarantine_paths():
    posts = _posts()
    assert C.is_scam_suspect(posts["fb-1003"]) is True
    assert C.is_scam_suspect(posts["fb-1001"]) is False
    cls = C.classify_post(posts["fb-1002"], BANK)
    assert cls["intent_primary"] == "complaint_pain"
    print("ok  quarantine: scam suspect + complaint routing")


def test_banned_symbols_absent():
    """TRD NFR-PK-8: no stealth/cookie/outreach capability in collectors."""
    banned = ["stealth", "cookie_jar", "undetected_chromedriver", "send_dm",
              "post_message", "comment_post", "identity_rotat"]
    bad = []
    for path in (ROOT / "collectors").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for sym in banned:
            if sym in src:
                bad.append(f"{path.name}:{sym}")
    assert not bad, f"banned symbols in collectors: {bad}"
    print("ok  static check: no stealth/cookie/outreach symbols in collectors")


def test_db_schema():
    import sqlite3
    from models import init_db
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("source", "post", "comment", "author_profile", "lead",
              "lead_score", "loophole_report", "collection_run",
              "suppression_entry", "audit_entry"):
        assert t in tables, f"missing table {t}"
    print("ok  schema: all 10 TRD models present")


if __name__ == "__main__":
    test_classifier_sample_posts()
    test_budget_parser()
    test_scorer_prd_worked_examples()
    test_complaint_hot_trigger()
    test_phone_key_dedupe()
    test_loophole_engine()
    test_quarantine_paths()
    test_banned_symbols_absent()
    test_db_schema()
    print("\nALL TESTS PASSED")
