#!/usr/bin/env python3
"""
Concurrency tests for collectors/concurrent.py — runnable with the stdlib only:

    python tests/test_concurrent.py

All tests stub the HTTP layer (never touch the network) and use TEMP data
only — the live DB is never written (this module holds no DB code anyway).

Covers:
  1. run_sources_concurrently returns identical record sets to sequential
     WebListingsCollector calls on 3 small fixture sources.
  2. Per-host in-flight cap: racing same-host tasks never exceed 2.
  3. Per-host minimum delay between request starts is honored.
  4. A worker that raises produces a failed result without killing the batch.
  5. 403/429 from the HTTP layer surfaces as failed, with no retries.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors import web_listings as WL  # noqa: E402
from collectors import concurrent as CC  # noqa: E402
from collectors.web_listings import WebListingsCollector  # noqa: E402


# ----------------------------------------------------------------------
# Fixture pages (tiny but parseable by the real parsers).
def _olx_html() -> str:
    items = []
    for pid, name, price, locality in (
            ("111", "House for sale in G-13", 15000000, "G-13"),
            ("222", "Flat for sale in D-12", 8000000, "D-12")):
        items.append({
            "item": {
                "@type": "RealEstateListing",
                "url": f"https://www.olx.com.pk/ad/some-ad-ID{pid}.html",
                "name": name,
                "offers": {"price": price},
                "mainEntity": {
                    "floorSize": {"value": 5, "unitText": "Marla"},
                    "address": {"addressLocality": locality},
                },
            }
        })
    payload = {"@graph": [{"@type": "CollectionPage",
                           "mainEntity": {"@type": "ItemList",
                                          "itemListElement": items}}]}
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(payload) + "</script></head></html>")


def _zameen_html() -> str:
    cards = []
    for pid, title in (("123456", "5 Marla House for Sale in G-13"),
                       ("234567", "3 Marla House for Sale in D-12")):
        cards.append(
            '<li role="article">'
            f'<a href="/Property/some-house-{pid}-11-22.html">'
            f'<img title="{title}"></a>'
            '<span aria-label="Currency">PKR</span>'
            '<span aria-label="Price">1.5 Crore</span>'
            '<span aria-label="Location">G-13, Islamabad</span>'
            '<span aria-label="Listing creation date">Added 3 hours ago</span>'
            "</li>")
    return "<html><body><ul>" + "".join(cards) + "</ul></body></html>"


def _graana_html() -> str:
    props = [
        {"id": 101, "customTitle": "House in Bahria", "price": "25000000",
         "size": "10", "sizeUnit": "Marla", "bed": 5, "bath": 4,
         "area": {"name": "Bahria Town"}, "city": {"name": "Islamabad"},
         "createdAt": "2026-10-10T00:00:00Z", "name": "Dealer A"},
        {"id": 102, "customTitle": "Flat in F-7", "price": "12000000",
         "size": "3", "sizeUnit": "Marla", "bed": 3, "bath": 2,
         "area": {"name": "F-7"}, "city": {"name": "Islamabad"},
         "createdAt": "2026-10-11T00:00:00Z", "name": "Owner B"},
    ]
    payload = {"props": {"pageProps": {"properties": props}}}
    return ('<html><head><script id="__NEXT_DATA__" '
            'type="application/json">' + json.dumps(payload)
            + "</script></head></html>")


def _src(sid: str, platform: str, url: str, pages: int = 1) -> dict:
    return {"source_id": sid, "platform": platform,
            "access_route": "web_public",
            "boundary_note": f"url:{url};pages:{pages}"}


FIXTURES = [
    _src("c-olx", "olx", "https://www.olx.com.pk/fixture-1"),
    _src("c-zameen", "zameen", "https://www.zameen.com/fixture-2"),
    _src("c-graana", "graana", "https://www.graana.com/fixture-3"),
]
_FIXTURE_BODY = {
    "olx.com.pk": _olx_html(),
    "zameen.com": _zameen_html(),
    "graana.com": _graana_html(),
}


def _fixture_stub(url: str, timeout: int = 30):
    """Route stub: pick the fixture page by host."""
    host = url.split("/")[2]
    key = host.replace("www.", "")
    return 200, _FIXTURE_BODY[key], None


def _patch_fetch(fn):
    """Install a temporary _fetch stub; returns a restore() callable."""
    real = WL._fetch
    WL._fetch = fn

    def restore():
        WL._fetch = real
    return restore


def _norm(records: list[dict]) -> list[tuple]:
    return sorted((r["platform"], r["platform_post_id"], r["url"],
                   r["text_original"]) for r in records)


# ----------------------------------------------------------------------
def test_concurrent_matches_sequential():
    """Same record sets (and counts) via run_sources_concurrently as via
    three sequential WebListingsCollector.collect() calls."""
    CC.reset_gates()
    restore = _patch_fetch(_fixture_stub)
    try:
        seq = {}
        for src in FIXTURES:
            r = WebListingsCollector().collect(src, None, 30, {})
            seq[src["source_id"]] = r
        conc = CC.run_sources_concurrently(FIXTURES, budget_per_min=30,
                                           max_workers=3, host_delay=0.0)
    finally:
        restore()

    assert set(conc) == set(seq), f"missing sources: {set(seq) - set(conc)}"
    for sid in seq:
        a, b = seq[sid], conc[sid]
        assert a["status"] == b["status"] == "ok", f"{sid}: {b['status']}"
        assert a["fetched"] == b["fetched"] == 2, f"{sid}: {b['fetched']}"
        assert _norm(a["records"]) == _norm(b["records"]), \
            f"{sid}: records differ"
        # full dicts equal too (not just the key tuples)
        da = {r["platform_post_id"]: dict(r) for r in a["records"]}
        db = {r["platform_post_id"]: dict(r) for r in b["records"]}
        assert da == db, f"{sid}: record bodies differ"
    # _fetch patch must be restored afterwards.
    assert WL._fetch is not CC._polite_fetch
    assert CC._orig_fetch is None
    print("ok  concurrent_matches_sequential: 3 sources, 2 records each")


def test_per_host_inflight_cap():
    """4 racing workers on the SAME host must never exceed 2 in-flight
    requests (MAX_INFLIGHT_PER_HOST)."""
    CC.reset_gates()
    lock = threading.Lock()
    state = {"inflight": 0, "max": 0}

    def stub(url, timeout=30):
        with lock:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
        try:
            time.sleep(0.25)  # hold the slot so races actually overlap
            return _fixture_stub(url, timeout)
        finally:
            with lock:
                state["inflight"] -= 1

    restore = _patch_fetch(stub)
    try:
        sources = [_src(f"cap-{i}", "olx",
                        f"https://www.olx.com.pk/race-{i}")
                   for i in range(4)]
        results = CC.run_sources_concurrently(sources, budget_per_min=30,
                                              max_workers=4, host_delay=0.0)
    finally:
        restore()
    assert state["max"] <= CC.MAX_INFLIGHT_PER_HOST, \
        f"in-flight reached {state['max']} (> 2)"
    assert state["max"] == CC.MAX_INFLIGHT_PER_HOST, \
        f"expected contention to reach 2, got {state['max']}"
    assert all(r["status"] == "ok" for r in results.values())
    print(f"ok  per_host_inflight_cap: max in-flight = {state['max']} (cap 2)")


def test_per_host_min_delay():
    """Consecutive request starts on the same host are >= host_delay apart."""
    CC.reset_gates()
    lock = threading.Lock()
    starts: list[float] = []

    def stub(url, timeout=30):
        with lock:
            starts.append(time.monotonic())
        return _fixture_stub(url, timeout)

    restore = _patch_fetch(stub)
    host_delay = 0.25
    try:
        sources = [_src(f"delay-{i}", "olx",
                        f"https://www.olx.com.pk/d-{i}")
                   for i in range(3)]
        results = CC.run_sources_concurrently(sources, budget_per_min=30,
                                              max_workers=3,
                                              host_delay=host_delay)
    finally:
        restore()
    assert len(starts) == 3, f"expected 3 starts, got {len(starts)}"
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    for g in gaps:
        assert g >= host_delay - 0.03, \
            f"gap {g:.3f}s shorter than host_delay {host_delay}s"
    assert all(r["status"] == "ok" for r in results.values())
    print(f"ok  per_host_min_delay: gaps={[round(g, 3) for g in gaps]} "
          f"(>= {host_delay}s)")


def test_worker_exception_isolated():
    """A worker that raises yields a failed result for its own source only;
    the rest of the batch is unaffected."""
    CC.reset_gates()
    real_collect = CC.WebListingsCollector.collect

    def boom(self, source, since, budget_per_min, ctx):
        if source.get("source_id") == "boom-1":
            raise RuntimeError("simulated worker crash")
        return real_collect(self, source, since, budget_per_min, ctx)

    restore = _patch_fetch(_fixture_stub)
    CC.WebListingsCollector.collect = boom
    try:
        sources = [_src("boom-1", "olx", "https://www.olx.com.pk/b-1"),
                   _src("fine-1", "zameen", "https://www.zameen.com/b-2")]
        results = CC.run_sources_concurrently(sources, budget_per_min=30,
                                              max_workers=2, host_delay=0.0)
    finally:
        CC.WebListingsCollector.collect = real_collect
        restore()

    bad = results["boom-1"]
    assert bad["status"] == "failed"
    assert bad["error_class"] == "concurrent:RuntimeError", bad["error_class"]
    assert bad["records"] == []
    good = results["fine-1"]
    assert good["status"] == "ok" and good["fetched"] == 2
    print("ok  worker_exception_isolated: boom -> failed, sibling -> ok")


def test_403_429_no_retries():
    """A 403/429 from the HTTP layer fails the source with an http_*_blocked
    error class and exactly one fetch (no retries)."""
    CC.reset_gates()
    lock = threading.Lock()
    calls: dict[str, int] = {}

    def stub(url, timeout=30):
        with lock:
            calls[url] = calls.get(url, 0) + 1
        if "blk403" in url:
            return 403, None, None
        if "blk429" in url:
            return 429, None, None
        return _fixture_stub(url, timeout)

    restore = _patch_fetch(stub)
    try:
        sources = [_src("b403", "olx", "https://www.olx.com.pk/blk403"),
                   _src("b429", "zameen", "https://www.zameen.com/blk429"),
                   _src("ok1", "graana", "https://www.graana.com/fixture-3")]
        results = CC.run_sources_concurrently(sources, budget_per_min=30,
                                              max_workers=3, host_delay=0.0)
    finally:
        restore()

    assert results["b403"]["status"] == "failed"
    assert results["b403"]["error_class"] == "http_403_blocked", \
        results["b403"]["error_class"]
    assert results["b429"]["status"] == "failed"
    assert results["b429"]["error_class"] == "http_429_blocked", \
        results["b429"]["error_class"]
    assert results["ok1"]["status"] == "ok" and results["ok1"]["fetched"] == 2
    assert calls["https://www.olx.com.pk/blk403"] == 1, calls
    assert calls["https://www.zameen.com/blk429"] == 1, calls
    print("ok  403_429_no_retries: blocked -> failed, single fetch each")


if __name__ == "__main__":
    test_concurrent_matches_sequential()
    test_per_host_inflight_cap()
    test_per_host_min_delay()
    test_worker_exception_isolated()
    test_403_429_no_retries()
    print("\nALL CONCURRENT TESTS PASSED")
