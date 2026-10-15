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
from collectors import web_listings as WL  # noqa: E402

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


# ----------------------------------------------------------------------
# web_public collector (web_listings) — B25 hardening tests. All fixtures
# are inline; no network.

def _zcard(pid):
    return (f'<li role="article">'
            f'<a href="/Property/house-for-sale-in-g-13-{pid}-678-9.html" '
            f'title="3 Marla House for sale in G-13">x</a>'
            f'<span aria-label="Currency">PKR</span>'
            f'<span aria-label="Price">1.5 Crore</span>'
            f'<span aria-label="Location">G-13, Islamabad</span></li>')


def test_web_challenge_detection():
    # A09: Zameen ships the JS flag STRAT_WITH_CAPTCHA:true on ordinary
    # result pages — a bare-"captcha" substring match would false-positive.
    ok_page = ("<html><body>var cfg={STRAT_WITH_CAPTCHA:true};<ul>"
               + _zcard("12345") + "</ul></body></html>")
    assert WL._looks_challenged(ok_page, "zameen") is False
    # Real challenge: markers present AND no listing content.
    chal = ("<html><body><div class='cf-challenge'>"
            "Please verify you are human</div></body></html>")
    assert WL._looks_challenged(chal, "zameen") is True
    # No marker at all -> never a challenge.
    assert WL._looks_challenged("<html><body>hello</body></html>", "olx") is False
    # The engine never substring-matches the bare word "captcha".
    assert all(m != "captcha" for m in WL._CHALLENGE_MARKERS)
    print("ok  web: challenge detection (no bare-'captcha' match)")


def _olx_search_html(anchor=True):
    import json
    item = {"@type": "RealEstateListing",
            "name": "House for Sale in Buch Villas",
            "url": "https://www.olx.com.pk/ad/house-for-sale-in-buch-villas-ID1119884225.html",
            "mainEntity": {"@type": "House", "numberOfBedrooms": "6",
                           "numberOfBathroomsTotal": "6",
                           "floorSize": {"@type": "QuantitativeValue",
                                         "value": "9", "unitText": "Marla"},
                           "address": {"@type": "PostalAddress",
                                       "streetAddress": "Buch Executive Villas",
                                       "addressLocality": "Multan"}},
            "offers": {"@type": "Offer", "price": 42000000, "priceCurrency": "PKR"}}
    ld = {"@graph": [{"@type": "CollectionPage",
                      "mainEntity": {"@type": "ItemList", "numberOfItems": 1,
                                     "itemListElement": [
                                         {"@type": "ListItem", "position": 1,
                                          "item": item}]}}]}
    body = ('<script id="search-schema" type="application/ld+json">'
            + json.dumps(ld) + '</script>')
    if anchor:
        body += ('<a href="/item/house-for-sale-in-buch-villas-iid-1119884225">'
                 'ad</a>')
    return "<html><head>" + body + "</head><body></body></html>"


def test_olx_search_schema_wrapper():
    # A05: new-city OLX pages ship LD in <script id="search-schema"> with
    # @graph[0].mainEntity as the ItemList — the existing _main_itemlists
    # walks @graph and must find it. The DOM card href gives the live URL.
    src = {"platform": "olx",
           "boundary_note": "url:https://www.olx.com.pk/multan_g4060615/houses_c1721",
           "source_id": "t"}
    recs = WL.WebListingsCollector()._parse_page(
        "olx", _olx_search_html(anchor=True), src)
    assert len(recs) == 1, f"expected 1, got {len(recs)}"
    r = recs[0]
    assert r["platform"] == "olx"
    assert r["platform_post_id"] == "1119884225"
    assert r["url"] == ("https://www.olx.com.pk/item/"
                        "house-for-sale-in-buch-villas-iid-1119884225"), r["url"]
    assert "Price Rs 42000000" in r["text_original"]
    assert "Size 9 Marla" in r["text_original"]
    assert r["access_route_used"] == "web_public"
    print("ok  web: <script id='search-schema'> @graph wrapper parsed")


def test_olx_url_rewrite_fallback():
    # No DOM card href: /ad/<slug>-ID<digits>.html rewrites to the live
    # /item/<slug>-iid-<digits> scheme (verified live-200 on 2 samples).
    src = {"platform": "olx",
           "boundary_note": "url:https://www.olx.com.pk/multan_g4060615/houses_c1721",
           "source_id": "t"}
    recs = WL.WebListingsCollector()._parse_page(
        "olx", _olx_search_html(anchor=False), src)
    assert len(recs) == 1
    assert recs[0]["url"] == ("https://www.olx.com.pk/item/"
                              "house-for-sale-in-buch-villas-iid-1119884225")
    assert "/ad/" not in recs[0]["url"], "stale /ad/ URL must not be emitted"
    print("ok  web: OLX /ad/ -> /item/ rewrite fallback")


def test_olx_motors_record():
    # A07: OLX vehicle category pages carry Product/Car JSON-LD items.
    import json
    item = {"@type": ["Product", "Car"],
            "name": "Toyota Corolla 2019 for sale",
            "url": "https://www.olx.com.pk/ad/toyota-corolla-2019-ID1120348047.html",
            "vehicleModelDate": "2019",
            "mileageFromOdometer": {"@type": "QuantitativeValue",
                                    "value": 190000, "unitCode": "KMT"},
            "fuelType": "Diesel",
            "offers": {"@type": "Offer", "price": 3000000, "priceCurrency": "PKR",
                       "availableAtOrFrom": {
                           "@type": "Place", "name": "E-11/4",
                           "address": {"streetAddress": "E-11/4",
                                       "addressLocality": "Islamabad"}}}}
    ld = {"@graph": [{"@type": "CollectionPage",
                      "mainEntity": {"@type": "ItemList", "numberOfItems": 1,
                                     "itemListElement": [{"@type": "ListItem",
                                                          "position": 1,
                                                          "item": item}]}}]}
    html = ('<html><body><script type="application/ld+json">'
            + json.dumps(ld) + '</script>'
            '<a href="/item/toyota-corolla-2019-iid-1120348047">ad</a></body></html>')
    src = {"platform": "olx",
           "boundary_note": "url:https://www.olx.com.pk/islamabad_g4060615/cars_c84",
           "source_id": "t"}
    recs = WL.WebListingsCollector()._parse_page("olx", html, src)
    assert len(recs) == 1, f"expected 1, got {len(recs)}"
    r = recs[0]
    assert r["platform"] == "olx"
    assert r["platform_post_id"] == "1120348047"
    assert "Price Rs 3000000" in r["text_original"]
    assert "Model year 2019" in r["text_original"]
    assert "190000 km" in r["text_original"]
    assert "Diesel" in r["text_original"]
    assert "E-11/4, Islamabad" in r["text_original"]
    assert "-iid-1120348047" in r["url"]
    print("ok  web: OLX motors (Product/Car) record parses")


def test_pakwheels_record():
    # A08: per-listing Product blocks; id from the URL slug tail; posted
    # date / seller type / location stay missing.
    import json

    def prod(pid, title, price):
        return {"@type": ["Product"],
                "name": title,
                "url": f"https://www.pakwheels.com/used-cars/kia-picanto-{pid}",
                "modelDate": "2023",
                "brand": {"@type": "Brand", "name": "KIA"},
                "fuelType": "Petrol",
                "vehicleTransmission": "Automatic",
                "vehicleEngine": {"engineDisplacement": "1000cc"},
                "mileageFromOdometer": "61,000 km",
                "offers": {"@type": "Offer", "price": price, "priceCurrency": "PKR",
                           "url": f"https://www.pakwheels.com/used-cars/kia-picanto-{pid}"}}

    blocks = "".join(
        '<script type="application/ld+json">' + json.dumps(prod(pid, t, p)) + '</script>'
        for pid, t, p in (("12058426", "KIA Picanto 2023 for sale in Islamabad", 3260000),
                          ("12130130", "Honda Civic 2020 for sale in Islamabad", 5950000)))
    ilist = ('<script type="application/ld+json">' +
             json.dumps({"@type": "ItemList", "numberOfItems": 2}) + '</script>')
    html = ("<html><body>" + ilist + blocks
            + '<div class="classified-listing"></div></body></html>')
    src = {"platform": "pakwheels",
           "boundary_note": "url:https://www.pakwheels.com/used-cars/islamabad/24856",
           "source_id": "t"}
    recs = WL.WebListingsCollector()._parse_page("pakwheels", html, src)
    assert len(recs) == 2, f"expected 2, got {len(recs)}"
    r = recs[0]
    assert r["platform"] == "pakwheels"
    assert r["platform_post_id"] == "12058426"
    assert r["published_at"] is None  # absent on search pages: stays missing
    assert r["author_handle"] == ""
    assert "Price Rs 3260000" in r["text_original"]
    assert "Model year 2023" in r["text_original"]
    assert "KIA" in r["text_original"] and "Automatic" in r["text_original"]
    assert "1000cc" in r["text_original"] and "61,000 km" in r["text_original"]
    assert r["access_route_used"] == "web_public"
    print("ok  web: PakWheels Product blocks parse")


def test_web_404_break_and_keep():
    # A09 bonus flag: a mid-run 404 (Zameen's end-of-listings signal) must
    # stop pagination, KEEP already-fetched records, and report ok — not
    # failed like the old code did.
    page1 = "<html><body><ul>" + _zcard("12345") + _zcard("12346") + "</ul></body></html>"
    calls = []

    def fake_fetch(url, timeout=30):
        calls.append(url)
        if "page=2" in url:
            return (404, None, None)
        return (200, page1, None)

    old = WL._fetch
    WL._fetch = fake_fetch
    try:
        res = WL.WebListingsCollector().collect(
            {"platform": "zameen",
             "boundary_note": "url:https://www.zameen.com/x-1.html?sort=date_desc;pages:5",
             "source_id": "t"},
            None, 30, {})
    finally:
        WL._fetch = old
    assert res["status"] == "ok", res
    assert res["fetched"] == 2, res
    assert len(res["records"]) == 2
    assert "404" in res.get("note", ""), res
    assert len(calls) == 2, f"should stop at the 404, fetched {len(calls)} pages"
    print("ok  web: mid-run 404 keeps records, stops, reports ok")


# ----------------------------------------------------------------------
# B22: Zameen detail-page enricher — offline parser + batch-budget tests.
# Synthetic fixtures only; no network calls anywhere below.
def _zameen_fixture(full=True):
    import json as _j
    dl = ('window[\'dataLayer\'] = window[\'dataLayer\'] || [];'
          'window[\'dataLayer\'].push({"property_price":41500000,'
          '"price":41500000,"marketed_by":"Blue Rock Investments",'
          '"seller_type":"business","agent_id":"201948",'
          '"owner_user_id":"1002280390","property_beds":"3",'
          '"property_baths_list":["4"],"property_land_area":"1125",'
          '"property_area":"104.52","listing_title":"5 Marla House",'
          '"reference_id":"ID54284067","dynx_itemid":"54284067",'
          '"latitude":"33.527944","longitude":"73.161392",'
          '"package_type":"superhot","package_hot":"0",'
          '"package_superhot":"1","package_paid":"0",'
          '"loc_city_name":"Islamabad","loc_name":"DHA Defence",'
          '"loc_neighbourhood_name":"DHA Defence Phase 5",'
          '"completion_status":"completed","purpose":"Buy",'
          '"property_type":";Houses_Property;"});')
    if not full:
        return f"<html><head><script>{dl}</script></head><body></body></html>"
    return (
        f"<html><head><script>{dl}</script></head><body>"
        '<div aria-label="Property description"><div dir="auto">'
        "Lovely 5 marla house near the park.</div></div>"
        "<h3>Amenities</h3><div>"
        "<div>Main Features</div>"
        "<span>Built in year<!-- -->: 2026</span>"
        "<span>Parking Spaces<!-- -->: 2</span>"
        "<div>Rooms</div>"
        "<span>Bedrooms<!-- -->: 3</span>"
        "</div></body></html>")


def test_detail_zameen_parsers():
    import json as _j
    from collectors import detail_zameen as dz

    full = _zameen_fixture(full=True)
    dl = dz._extract_datalayer(full)
    assert dl["property_price"] == 41500000
    assert dl["marketed_by"] == "Blue Rock Investments"

    d = dz._build_detail(dl, dz._parse_description(full),
                         *dz._parse_amenities(full),
                         "https://www.zameen.com/Property/x-1.html", "1")
    assert d["price_pkr"] == 41500000
    assert d["marketed_by"] == "Blue Rock Investments"
    assert d["seller_type"] == "business"
    assert d["agent_id"] == "201948" and d["owner_user_id"] == "1002280390"
    assert d["beds"] == 3 and d["baths"] == 4
    assert d["land_area_raw"] == "1125" and "unit" in d["land_area_unit_note"]
    assert d["builtup_area_raw"] == "104.52"  # raw, never converted
    assert d["reference_id"] == "ID54284067"
    assert d["latitude"] == 33.527944 and d["longitude"] == 73.161392
    assert d["package_type"] == "superhot"
    assert d["location"]["loc_neighbourhood_name"] == "DHA Defence Phase 5"
    assert d["purpose"] == "Buy" and d["property_type"] == ";Houses_Property;"
    assert d["description"] == "Lovely 5 marla house near the park."
    assert any(a == {"category": "Main Features", "name": "Built in year",
                     "value": "2026"} for a in d["amenities"])
    assert d["built_year"] == 2026
    assert d["enriched_at"]
    # No contact/phone material is ever produced.
    assert not any(k for k in d if "phone" in k or "tel" in k or "contact" in k)

    # Degraded page: dataLayer only -> dataLayer-only result, no fabrication.
    deg = _zameen_fixture(full=False)
    dl2 = dz._extract_datalayer(deg)
    assert dl2["property_price"] == 41500000
    d2 = dz._build_detail(dl2, dz._parse_description(deg),
                          *dz._parse_amenities(deg),
                          "https://www.zameen.com/Property/x-2.html", "2")
    assert d2["price_pkr"] == 41500000
    assert "description" not in d2 and "amenities" not in d2
    assert dz._parse_description(deg) is None

    # CAPTCHA markers: healthy config strings must NOT trip, real
    # challenge markup must.
    assert not any(p.search('{"STRAT_WITH_CAPTCHA":true}')
                   for p in dz._BLOCK_PATTERNS)
    assert any(p.search("<div>Verify you are human</div>")
               for p in dz._BLOCK_PATTERNS)

    # Batch: budget cap, delay floor, block aborts, no retries.
    calls = []

    def _fake_enrich(url, pid):
        calls.append(pid)
        return {"platform_post_id": pid}, None

    real = dz._enrich_full
    dz._enrich_full = _fake_enrich
    try:
        five = [{"platform_post_id": f"p{i}", "url": f"https://u/{i}"}
                for i in range(5)]
        b = dz.enrich_batch(five, max_items=3, delay_s=0.0)
        assert b["attempted"] == 3 and len(calls) == 3
        assert all(r["error_class"] == "not_attempted" for r in b["results"][3:])

        dz._enrich_full = lambda url, pid: (None, "http_429_blocked")
        b2 = dz.enrich_batch(five, max_items=5, delay_s=0.0)
        assert b2["stopped_on_block"] and b2["attempted"] == 1
        assert b2["block_error"] == "http_429_blocked"
        assert all(r["error_class"] == "not_attempted"
                   for r in b2["results"][1:])
    finally:
        dz._enrich_full = real
    print("ok  detail_zameen: parsers, degraded page, batch cap, block abort")


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
    test_web_challenge_detection()
    test_olx_search_schema_wrapper()
    test_olx_url_rewrite_fallback()
    test_olx_motors_record()
    test_pakwheels_record()
    test_web_404_break_and_keep()
    test_detail_zameen_parsers()
    print("\nALL TESTS PASSED")
