#!/usr/bin/env python3
"""
Detail-enricher tests (Wave-B B36) — runnable with the stdlib only:

    python tests/test_enrichment.py

Covers collectors/detail_olx.py, detail_zameen.py, detail_graana.py:

- OLX: resolve_detail_url unit tests; enrich() field-set extraction from a
  fixture payload; phone captured ONLY when isPhoneNumberVisible=true;
  captcha/block handling with no retries
- Zameen: enrich() dataLayer extraction; degraded dataLayer-only fixture
  (missing fields absent, never fabricated)
- Graana: enrich() __NEXT_DATA__ props.pageProps.data parse; internal
  Cognito/backend identifiers excluded
- Budget caps: budget=2 over 5 listings -> exactly 2 enriched, 3 skipped
- Block paths: stubbed 403/429 -> error_class with exactly 1 fetch

All network access is stubbed; nothing touches the live DB.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors import detail_olx as do_olx  # noqa: E402
from collectors import detail_zameen as dz  # noqa: E402
from collectors import detail_graana as dg  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures (offline HTML snippets; no live network anywhere below)
# ----------------------------------------------------------------------
OLX_URL = "https://www.olx.com.pk/ad/spacious-10-marla-house-ID12345678.html"


def _olx_html(phone_visible: bool = True) -> str:
    dl = {
        "page_type": "offerdetail",
        "ad_id": "12345678",
        "seller_id": "998877",
        "seller_type": "private",
        "is_agent": "no",
        "seller_verified": "yes",
        "number_of_photos": 8,
        "package_type": "featured",
        "price": "15500000",
        "currency_unit": "Rs.",
        "rooms": "4",
        "bathrooms": "3",
        "ft": "10",
        "ft_unit": "Marla",
        "furnished": "yes",
        "video": "yes",
        "panorama": "no",
        "ownership_type": "owned",
        "loc_id": "402",
        "loc_name": "DHA Defence",
        "loc_breadcrumb": "Islamabad / DHA Defence",
        "loc_1_id": "1",
        "loc_1_name": "Islamabad",
        "loc_2_id": "401",
        "loc_2_name": "Defence",
    }
    sp = {"loading": False, "error": None, "data": {
        "name": "Ahmed Khan",
        "isPhoneNumberVisible": phone_visible,
        "phoneNumber": "03001234567",
        "createdAt": "2021-06-15T00:00:00Z",
        "activeAdCount": 3,
    }}
    return (
        "<html><head><script>"
        "window['dataLayer'] = window['dataLayer'] || [];"
        "window['dataLayer'].push(" + json.dumps(dl) + ");"
        'window.state = {adManagement: {"sellerProfile":' + json.dumps(sp) + "}};"
        "</script></head><body>"
        '<section aria-label="Description"><h2>Description</h2>'
        "<p>Spacious 10 marla house with marble flooring, double unit, "
        "near main boulevard.</p>"
        "</section>"
        "<!-- Related ads -->"
        "<div>Posted by Ahmed Khan</div>"
        "</body></html>")


def _zameen_html(full: bool = True) -> str:
    dl = {
        "property_price": 41500000,
        "price": 41500000,
        "marketed_by": "Blue Rock Investments",
        "seller_type": "business",
        "agent_id": "201948",
        "owner_user_id": "1002280390",
        "property_beds": "3",
        "property_baths_list": ["4"],
        "property_land_area": "1125",
        "property_area": "104.52",
        "listing_title": "5 Marla House for Sale in DHA Defence",
        "reference_id": "ID54284067",
        "dynx_itemid": "54284067",
        "latitude": "33.527944",
        "longitude": "73.161392",
        "package_type": "superhot",
        "loc_city_name": "Islamabad",
        "loc_name": "DHA Defence",
        "loc_neighbourhood_name": "DHA Defence Phase 5",
        "completion_status": "completed",
        "purpose": "Buy",
        "property_type": ";Houses_Property;",
    }
    script = ("window['dataLayer'] = window['dataLayer'] || [];"
              "window['dataLayer'].push(" + json.dumps(dl) + ");")
    if not full:
        return ("<html><head><script>" + script +
                "</script></head><body></body></html>")
    return (
        "<html><head><script>" + script + "</script></head><body>"
        '<div aria-label="Property description"><div dir="auto">'
        "Lovely 5 marla house near the park, demand 4.15 crore."
        "</div></div>"
        "<h3>Amenities</h3><div>"
        "<div>Main Features</div>"
        "<span>Built in year<!-- -->: 2026</span>"
        "<span>Parking Spaces<!-- -->: 2</span>"
        "</div></body></html>")


def _graana_html() -> str:
    data = {
        "id": "789012",
        "description": "Modern 5 marla corner house with solid construction.",
        "phone": "03019876543",
        "builtYear": "2020",
        "condition": "New",
        "propertyAvailability": "Available",
        "expiresAt": "2026-12-31",
        "daysOnGraana": 15,
        "lat": 33.6844,
        "lng": 73.0479,
        "address": "Street 12, DHA Phase 2",
        "area": {"name": "DHA Phase 2"},
        "city": {"name": "Islamabad"},
        "bed": 5,
        "bath": 6,
        "primaryFeatures": {"Bedrooms": "5", "Bathrooms": "6"},
        "secondaryFeatures": {"Flooring": "Marble"},
        "utilityFeatures": {},
        "nearByFeatures": None,
        "communicationFeatures": {"Intercom": "Yes"},
        "propertyImages": ["https://img1", "https://img2", "https://img3"],
        "status": "active",
        "agency": {
            "name": "Star Properties",
            "phone": "03001112233",
            "email": "info@starproperties.pk",
            "website": "https://starproperties.pk",
            "address": "Plaza 1, Main Boulevard",
            "city": {"name": "Islamabad"},
        },
        "agentDetails": {
            "name": "Ali Raza",
            "family_name": "Raza",
            "phone_number": "03225556666",
            "phone_number_verified": True,
            "sub": "cognito-sub-uuid-1234",
            "Username": "ali.raza.88",
            "custom:role": "agent",
            "email": "ali.raza.88@agents.pk",
        },
        "agencyId": "internal-agency-1",
        "agentId": "internal-agent-2",
        "userId": "internal-user-3",
        "systemUserId": "internal-sys-4",
        "riderId": "internal-rider-5",
        "propsureId": "internal-propsure-6",
        "cloudinaryPublicId": "backend-only-cloud-7",
        "createdAt": "2020-01-01T00:00:00Z",
        "updatedAt": "2021-06-01T00:00:00Z",
        "deletedAt": None,
    }
    payload = {"props": {"pageProps": {"data": data}}}
    return ('<html><head><script id="__NEXT_DATA__" '
            'type="application/json">' + json.dumps(payload) +
            "</script></head><body></body></html>")


def _five_listings(prefix: str, url_fn):
    return [{"platform_post_id": f"{prefix}{i}", "url": url_fn(i)}
            for i in range(5)]


# ----------------------------------------------------------------------
# 1. OLX: URL resolution
# ----------------------------------------------------------------------
def test_olx_resolve_detail_url():
    # Stale /ad/ scheme rewrites to the live /item/ scheme.
    url, rewritten = do_olx.resolve_detail_url(OLX_URL)
    assert url == ("https://www.olx.com.pk/item/"
                   "spacious-10-marla-house-iid-12345678"), url
    assert rewritten is True
    # Live /item/ scheme passes through untouched.
    url2, rewritten2 = do_olx.resolve_detail_url(url)
    assert url2 == url and rewritten2 is False
    # Anything else: (None, False) — never fetched.
    assert do_olx.resolve_detail_url("https://www.olx.com.pk/search/x") == (None, False)
    assert do_olx.resolve_detail_url("") == (None, False)
    assert do_olx.resolve_detail_url(None) == (None, False)
    # http scheme and bare host also match.
    url3, rewritten3 = do_olx.resolve_detail_url(
        "http://olx.com.pk/ad/a-ID1.html")
    assert url3 == "https://www.olx.com.pk/item/a-iid-1" and rewritten3 is True
    print("ok  olx: resolve_detail_url /ad/ -> /item/ rewrite, passthrough, rejects")


# ----------------------------------------------------------------------
# 2. OLX: enrich() field set + phone gating
# ----------------------------------------------------------------------
def _with_olx_fetch(html_or_block):
    """Monkeypatch do_olx._fetch to return a canned response."""
    calls = []
    real = do_olx._fetch

    def fake(url, timeout=30):
        calls.append(url)
        if isinstance(html_or_block, tuple):
            return html_or_block
        return 200, html_or_block, None

    do_olx._fetch = fake
    return real, calls


def test_olx_enrich_field_set():
    real, calls = _with_olx_fetch(_olx_html(phone_visible=True))
    try:
        d = do_olx.enrich({"url": OLX_URL, "platform_post_id": "12345678"})
    finally:
        do_olx._fetch = real
    assert d is not None, "enrich returned None on a healthy fixture"
    assert d["platform"] == "olx"
    assert d["platform_post_id"] == "12345678"
    assert d["ad_id"] == "12345678"
    assert d["seller_type"] == "private"
    assert d["is_agent"] is False
    assert d["seller_verified"] is True
    assert d["seller_name"] == "Ahmed Khan"
    assert d["member_since"] == "2021-06-15T00:00:00Z"
    assert d["active_ad_count"] == 3
    assert d["number_of_photos"] == 8
    assert d["package_type"] == "featured"
    assert d["is_featured"] is True
    assert d["price"] == "15500000"
    assert d["currency"] == "Rs."
    assert d["rooms"] == "4" and d["bathrooms"] == "3"
    assert d["size_value"] == "10" and d["size_unit"] == "Marla"
    assert d["furnished"] is True
    assert d["has_video"] is True
    assert d["has_panorama"] is False
    assert d["ownership_type"] == "owned"
    assert d["loc"]["loc_name"] == "DHA Defence"
    assert d["loc"]["loc_breadcrumb"] == "Islamabad / DHA Defence"
    assert d["loc"]["loc_1_name"] == "Islamabad"
    assert "Spacious 10 marla house" in d["description"]
    assert "Related ads" not in d["description"]
    assert d["url_resolved"] == ("https://www.olx.com.pk/item/"
                                 "spacious-10-marla-house-iid-12345678")
    assert d["url_rewritten_from_ad_scheme"] is True
    assert d["source"] == "olx_detail"
    assert d["enriched_at"]
    # Phone present ONLY because isPhoneNumberVisible=true.
    assert d["phone"] == "03001234567"
    assert d["phone_visible"] is True
    assert len(calls) == 1
    print("ok  olx: enrich() extracts the documented field set; phone kept when visible")


def test_olx_phone_gating():
    real, _ = _with_olx_fetch(_olx_html(phone_visible=False))
    try:
        d = do_olx.enrich({"url": OLX_URL, "platform_post_id": "12345678"})
    finally:
        do_olx._fetch = real
    assert d is not None
    assert "phone" not in d, "phone must be absent when not visible"
    assert "phone_visible" not in d
    assert d["seller_name"] == "Ahmed Khan"  # name still captured
    # Bad URL -> None (never fetched).
    assert do_olx.enrich({"url": "https://example.com/nope",
                          "platform_post_id": "x"}) is None
    print("ok  olx: phone absent when isPhoneNumberVisible!=true; bad URL -> None")


# ----------------------------------------------------------------------
# 3. OLX: block paths (403/429/captcha -> error_class, exactly 1 fetch)
# ----------------------------------------------------------------------
def test_olx_block_paths():
    listing = {"url": OLX_URL, "platform_post_id": "12345678"}

    # Stub at the _fetch level: block reported, no retry.
    for code in (403, 429):
        calls = []
        real = do_olx._fetch

        def fake(url, timeout=30, _code=code):
            calls.append(url)
            return _code, None, f"http_{_code}_blocked"

        do_olx._fetch = fake
        try:
            d = do_olx.enrich(listing)
            assert d is None
            assert len(calls) == 1, f"http_{code}: expected 1 fetch, got {len(calls)}"
            r = do_olx.enrich_leads([listing], budget=1, delay_seconds=0,
                                    fetcher=lambda u, timeout=30:
                                    (code, None, f"http_{code}_blocked"))
            assert r["failed"] == {"12345678": f"http_{code}_blocked"}, r
        finally:
            do_olx._fetch = real

    # CAPTCHA markers in a 200 body -> captcha_challenge via the real path.
    d2, err2 = do_olx._enrich_with_error(
        listing, 30,
        lambda url, timeout=30: (200, "<html><body>perimeterx challenge</body></html>", None))
    assert d2 is None and err2 == "captcha_challenge", err2

    # Real _fetch converts a raised HTTPError(403) to http_403_blocked
    # with exactly one urlopen call (no retry).
    calls3 = []
    real_open = urllib.request.urlopen

    def fake_open(req, timeout=30):
        calls3.append(getattr(req, "full_url", None))
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    urllib.request.urlopen = fake_open
    try:
        status, text, err = do_olx._fetch(
            "https://www.olx.com.pk/item/x-iid-1")
    finally:
        urllib.request.urlopen = real_open
    assert err == "http_403_blocked" and status == 403 and text is None
    assert len(calls3) == 1, f"expected exactly 1 urlopen, got {len(calls3)}"
    print("ok  olx: 403/429/captcha -> error_class with exactly 1 fetch (no retries)")


# ----------------------------------------------------------------------
# 4. Zameen: enrich() dataLayer extraction + degraded fixture
# ----------------------------------------------------------------------
def _with_zameen_fetch(status_text_err):
    calls = []
    real = dz._fetch

    def fake(url, timeout=30):
        calls.append(url)
        return status_text_err

    dz._fetch = fake
    return real, calls


def test_zameen_enrich_datalayer():
    real, calls = _with_zameen_fetch((200, _zameen_html(full=True), None))
    try:
        d = dz.enrich({"url": "https://www.zameen.com/Property/x-1.html",
                       "platform_post_id": "1"})
    finally:
        dz._fetch = real
    assert d is not None
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
    assert d["description"] == "Lovely 5 marla house near the park, demand 4.15 crore."
    assert any(a == {"category": "Main Features", "name": "Built in year",
                     "value": "2026"} for a in d["amenities"])
    assert d["built_year"] == 2026
    assert d["enriched_at"]
    # No contact/phone material is ever produced (SHR-1).
    assert not any("phone" in k or "tel" in k or "contact" in k for k in d)
    assert len(calls) == 1
    print("ok  zameen: enrich() extracts dataLayer fields; no phone material")


def test_zameen_degraded_datalayer_only():
    real, _ = _with_zameen_fetch((200, _zameen_html(full=False), None))
    try:
        d = dz.enrich({"url": "https://www.zameen.com/Property/x-2.html",
                       "platform_post_id": "2"})
    finally:
        dz._fetch = real
    assert d is not None, "dataLayer-only page must still enrich"
    assert d["price_pkr"] == 41500000
    # Missing sections are ABSENT, never fabricated.
    assert "description" not in d
    assert "amenities" not in d
    assert "built_year" not in d
    # No dataLayer at all -> None.
    real2, _ = _with_zameen_fetch((200, "<html><body>no payload</body></html>", None))
    try:
        assert dz.enrich({"url": "https://www.zameen.com/Property/x-3.html",
                          "platform_post_id": "3"}) is None
    finally:
        dz._fetch = real2
    print("ok  zameen: degraded dataLayer-only page omits missing fields, no fabrication")


def test_zameen_block_paths():
    for code in (403, 429):
        real, calls = _with_zameen_fetch((code, None, f"http_{code}_blocked"))
        try:
            got = dz.enrich({"url": "https://www.zameen.com/Property/x-9.html",
                             "platform_post_id": "9"})
            assert got is None
            assert len(calls) == 1, f"http_{code}: expected 1 fetch, got {len(calls)}"
            calls.clear()
            five = _five_listings("z", lambda i: f"https://www.zameen.com/Property/x-{i}.html")
            b = dz.enrich_batch(five, max_items=5, delay_s=0)
            assert b["stopped_on_block"] and b["attempted"] == 1, b
            assert len(calls) == 1, f"block must abort batch: {len(calls)} fetches"
            assert all(r["error_class"] == "not_attempted" for r in b["results"][1:])
        finally:
            dz._fetch = real
    print("ok  zameen: 403/429 -> enrich None; batch aborts with exactly 1 fetch")


# ----------------------------------------------------------------------
# 5. Graana: __NEXT_DATA__ parse + internal-field exclusion
# ----------------------------------------------------------------------
def _with_graana_fetch(status_text_err):
    calls = []
    real = dg._fetch

    def fake(url, timeout=30):
        calls.append(url)
        return status_text_err

    dg._fetch = fake
    return real, calls


def test_graana_enrich_next_data():
    real, calls = _with_graana_fetch((200, _graana_html(), None))
    url = "https://www.graana.com/property/islamabad/dha-phase-2-789012/"
    try:
        d = dg.enrich({"url": url, "platform_post_id": "789012"})
    finally:
        dg._fetch = real
    assert d is not None
    assert d["graana_id"] == "789012"
    assert d["detail_url"] == url
    assert d["description"] == "Modern 5 marla corner house with solid construction."
    assert d["listing_phone"] == "03019876543"
    assert d["built_year"] == "2020"
    assert d["condition"] == "New"
    assert d["availability"] == "Available"
    assert d["expires_at"] == "2026-12-31"
    assert d["days_on_graana"] == 15
    assert d["lat"] == 33.6844 and d["lng"] == 73.0479
    assert d["address"] == "Street 12, DHA Phase 2"
    assert d["area"] == "DHA Phase 2" and d["city"] == "Islamabad"
    assert d["bed"] == 5 and d["bath"] == 6
    assert d["features"]["primaryFeatures"] == {"Bedrooms": "5", "Bathrooms": "6"}
    assert "utilityFeatures" not in d["features"]  # empty group dropped
    assert d["image_count"] == 3
    assert d["agency_contact"]["name"] == "Star Properties"
    assert d["agency_contact"]["city"] == "Islamabad"
    # Only the audited agent keys are copied.
    assert d["agent_contact"] == {
        "name": "Ali Raza", "family_name": "Raza",
        "phone_number": "03225556666", "phone_number_verified": True}
    assert d["enriched_at"]
    # Internal Cognito/backend identifiers are excluded.
    blob = json.dumps(d)
    for secret in ("cognito-sub-uuid-1234", "ali.raza.88", "internal-agency-1",
                   "internal-agent-2", "internal-user-3", "internal-sys-4",
                   "internal-rider-5", "internal-propsure-6",
                   "backend-only-cloud-7", "custom:role", "agents.pk"):
        assert secret not in blob, f"internal value leaked: {secret}"
    assert len(calls) == 1
    # No __NEXT_DATA__ -> None (payload_missing).
    real2, _ = _with_graana_fetch((200, "<html><body>shell</body></html>", None))
    try:
        assert dg.enrich({"url": url, "platform_post_id": "x"}) is None
        assert dg.last_error == "payload_missing"
        assert dg.enrich({"platform_post_id": "x"}) is None  # no url
        assert dg.last_error == "missing_detail_url"
    finally:
        dg._fetch = real2
    print("ok  graana: __NEXT_DATA__ parsed; internal Cognito/backend fields excluded")


def test_graana_block_paths():
    for code in (403, 429):
        real, calls = _with_graana_fetch((code, None, f"http_{code}_blocked"))
        url = "https://www.graana.com/property/islamabad/x-1/"
        try:
            assert dg.enrich({"url": url, "platform_post_id": "1"}) is None
            assert dg.last_error == f"http_{code}_blocked"
            assert len(calls) == 1, f"http_{code}: expected 1 fetch, got {len(calls)}"
            calls.clear()
            five = _five_listings("g", lambda i: f"https://www.graana.com/property/islamabad/x-{i}/")
            r = dg.run_batch(five, budget=5, delay=0)
            assert r["fetched"] == 1, r
            assert r["skipped"].get("skipped_block") == 4, r
            assert r["error_classes"] == {f"http_{code}_blocked": 1}, r
        finally:
            dg._fetch = real
    print("ok  graana: 403/429 -> None + error_class; batch aborts with exactly 1 fetch")


# ----------------------------------------------------------------------
# 6. Budget caps: budget=2 over 5 listings -> exactly 2 enriched, 3 skipped
# ----------------------------------------------------------------------
def test_budget_caps():
    real_sleep = time.sleep
    time.sleep = lambda s: None  # politeness sleeps not needed offline
    try:
        # OLX: enrich_leads
        stub = lambda url, timeout=30: (200, _olx_html(phone_visible=True), None)
        five_o = _five_listings("o", lambda i:
                                f"https://www.olx.com.pk/ad/slug-ID1000{i}.html")
        r = do_olx.enrich_leads(five_o, budget=2, delay_seconds=0, fetcher=stub)
        assert r["fetched"] == 2, r
        assert len(r["results"]) == 2, r
        assert len(r["skipped"]) == 3, r
        assert r["budget"] == 2

        # Zameen: enrich_batch
        real_z, _ = _with_zameen_fetch((200, _zameen_html(full=True), None))
        try:
            five_z = _five_listings("z", lambda i:
                                    f"https://www.zameen.com/Property/x-{i}.html")
            b = dz.enrich_batch(five_z, max_items=2, delay_s=0)
        finally:
            dz._fetch = real_z
        assert b["attempted"] == 2, b
        assert b["enriched"] == 2, b
        assert len(b["results"]) == 5, b
        assert all(r["error_class"] == "not_attempted" for r in b["results"][2:])

        # Graana: run_batch
        real_g, _ = _with_graana_fetch((200, _graana_html(), None))
        try:
            five_g = _five_listings("g", lambda i:
                                    f"https://www.graana.com/property/islamabad/x-{i}/")
            g = dg.run_batch(five_g, budget=2, delay=0)
        finally:
            dg._fetch = real_g
        assert g["fetched"] == 2, g
        assert len(g["enriched"]) == 2, g
        assert g["skipped"].get("skipped_budget") == 3, g
        assert g["budget"] == 2
    finally:
        time.sleep = real_sleep
    print("ok  budget: 2 of 5 enriched and 3 skipped across olx/zameen/graana")


if __name__ == "__main__":
    test_olx_resolve_detail_url()
    test_olx_enrich_field_set()
    test_olx_phone_gating()
    test_olx_block_paths()
    test_zameen_enrich_datalayer()
    test_zameen_degraded_datalayer_only()
    test_zameen_block_paths()
    test_graana_enrich_next_data()
    test_graana_block_paths()
    test_budget_caps()
    print("\nALL TESTS PASSED")
