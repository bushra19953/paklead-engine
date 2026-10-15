"""
Concurrent web-source fetching with per-host politeness (Wave-B B17).

Runs the fetch+parse phase of multiple ``web_public`` sources concurrently
with ``concurrent.futures.ThreadPoolExecutor``. Every HTTP request made by
the workers passes through a per-host politeness gate (``_polite_fetch``):

* at most 2 concurrent in-flight requests per host
  (olx.com.pk, zameen.com, graana.com each own a semaphore; any other host
  gets one lazily), and
* a minimum ``host_delay`` seconds (default 1.5) between request *starts*
  on the same host.

Politeness is non-negotiable: a 403/429 is reported as a failed source
(no retries, no evasion), exactly as ``WebListingsCollector`` already does
for the sequential path (SHR-1).

Thread-safety
-------------
* Each worker thread constructs its own ``WebListingsCollector`` instance
  (the class keeps no instance state — everything lives in locals).
* Records are returned per source as plain lists; the module holds no
  shared mutable state besides the per-host politeness gates, which are
  protected by locks/semaphores.
* This module NEVER touches the database. It returns
  ``{source_id: CollectResult}`` and the pipeline does all writes.

How the politeness gate works
-----------------------------
The runner installs a thin wrapper over ``collectors.web_listings._fetch``
for the duration of the run (installed and restored inside
``run_sources_concurrently``; no engine file is modified). The wrapper
acquires the host semaphore, then — holding the host lock — waits until
``host_delay`` has elapsed since the last request start on that host,
stamps the new start time, and only then delegates to the original
``_fetch``. This gives exact per-request guarantees even when one source
spans several paginated fetches across threads.

Integration contract (for the coordinator wiring into ``Pipeline``)
------------------------------------------------------------------
::

    from collectors.concurrent import run_sources_concurrently

    web_sources = [s for s in pipeline.get_sources()
                   if s["access_route"] == "web_public"
                   and s["platform"] in ("olx", "zameen", "graana")]
    results = run_sources_concurrently(
        web_sources,
        budget_per_min=30,          # passed through to each collect() call
        max_workers=6,
        host_delay=1.5,
        ctx={"settings": s, "phrase_bank": bank},  # optional; no DB handle
    )
    for src in web_sources:
        result = results[src["source_id"]]   # a CollectResult
        # then the SAME post-fetch handling as Pipeline._run_source:
        #   * _process_records(result["records"], src, run_stamp)
        #   * UPDATE collection_run with result["fetched"],
        #     result["rate_limit_events"], result["status"], result["error_class"]
        #   * update watermark from result["new_watermark"]

``results`` maps every input ``source_id`` to a ``CollectResult``
(``status`` ``"ok"``/``"failed"`` with ``records``, ``fetched``,
``rate_limit_events``, ``error_class``, ``new_watermark``). A worker that
raises is converted to a failed ``CollectResult`` — the runner itself never
raises for a single source's failure.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import web_listings
from .base import make_result
from .web_listings import WebListingsCollector

log = logging.getLogger("paklead.concurrent")

# Hosts known to carry their own per-host semaphore.
KNOWN_HOSTS = ("olx.com.pk", "zameen.com", "graana.com")

# Max simultaneous in-flight HTTP requests per host.
MAX_INFLIGHT_PER_HOST = 2


class _HostGate:
    """Per-host politeness state: an in-flight cap plus a start-time lock."""

    def __init__(self, max_inflight: int = MAX_INFLIGHT_PER_HOST):
        self.semaphore = threading.Semaphore(max_inflight)
        self.lock = threading.Lock()
        self.last_start = 0.0  # monotonic timestamp of last request start


_gates_lock = threading.Lock()
_gates: dict[str, _HostGate] = {}

# Active politeness settings while the _fetch patch is installed.
_orig_fetch = None
_host_delay = 1.5


def _host_key(hostname: str) -> str:
    """Normalize a hostname to its politeness domain.

    'www.olx.com.pk' -> 'olx.com.pk'; unknown hosts keep their full name
    (they get their own lazily-created gate).
    """
    host = (hostname or "").lower()
    for known in KNOWN_HOSTS:
        if host == known or host.endswith("." + known):
            return known
    return host or "unknown"


def _host_key_of_url(url: str) -> str:
    try:
        return _host_key(urllib.parse.urlparse(url).hostname or "")
    except Exception:
        return "unknown"


def _gate(key: str) -> _HostGate:
    """Return (creating lazily) the politeness gate for a host key."""
    gate = _gates.get(key)
    if gate is None:
        with _gates_lock:
            gate = _gates.get(key)
            if gate is None:
                gate = _HostGate()
                _gates[key] = gate
                log.debug("created politeness gate for host '%s'", key)
    return gate


def reset_gates() -> None:
    """Drop all lazily-created host gates (mainly for tests)."""
    with _gates_lock:
        _gates.clear()


def _polite_fetch(url: str, timeout: int = 30):
    """Per-host polite replacement for ``web_listings._fetch``.

    Acquires the host's in-flight semaphore, then — while holding the
    host lock — enforces ``_host_delay`` seconds since the previous
    request start on that host before delegating to the original fetch.
    """
    key = _host_key_of_url(url)
    gate = _gate(key)
    gate.semaphore.acquire()
    try:
        with gate.lock:
            now = time.monotonic()
            wait = gate.last_start + _host_delay - now
            if wait > 0:
                time.sleep(wait)
            gate.last_start = time.monotonic()
        return _orig_fetch(url, timeout=timeout)
    finally:
        gate.semaphore.release()


def _run_one(source: dict, budget_per_min: int, ctx: dict):
    """Fetch+parse one source in a worker thread. Never raises."""
    sid = source.get("source_id") or "<no-id>"
    try:
        adapter = WebListingsCollector()  # per-thread instance, no shared state
        result = adapter.collect(source, source.get("watermark"),
                                 budget_per_min, ctx)
        log.info("concurrent: source '%s' -> %s (%d records).",
                 sid, result["status"], len(result["records"]))
        return sid, result
    except Exception as e:  # never let one source kill the sweep
        log.warning("concurrent: source '%s' raised %s: %s",
                    sid, type(e).__name__, e)
        return sid, make_result(status="failed",
                                error_class=f"concurrent:{type(e).__name__}")


def run_sources_concurrently(sources: list[dict], budget_per_min: int,
                             max_workers: int = 6, host_delay: float = 1.5,
                             ctx: dict | None = None) -> dict:
    """Fetch+parse multiple ``web_public`` sources concurrently.

    Args:
        sources: source dicts (as from ``Pipeline.get_sources()``); each
            needs ``source_id``, ``platform`` in (olx, zameen, graana), and
            a ``boundary_note`` carrying ``url:<page>[;pages:<n>]``.
        budget_per_min: passed through to each ``collect()`` call
            (controls that source's own inter-page delay).
        max_workers: ThreadPoolExecutor size.
        host_delay: minimum seconds between request starts on the same
            host (default 1.5).
        ctx: optional context dict forwarded to ``collect()``. This module
            never reads ``ctx["conn"]`` or calls ``ctx["audit"]`` — the
            pipeline keeps all DB writes.

    Returns:
        dict mapping each input ``source_id`` to its ``CollectResult``
        (``records``, ``fetched``, ``rate_limit_events``, ``status``,
        ``error_class``, ``new_watermark``). A 403/429 surfaces as a failed
        result with ``error_class`` like ``http_403_blocked`` — that source
        simply stops, per SHR-1.
    """
    global _orig_fetch, _host_delay
    sources = list(sources or [])
    if not sources:
        return {}

    budget_per_min = int(budget_per_min) or 30
    ctx = dict(ctx or {})

    # Install the politeness wrapper for the whole run, restore afterwards.
    _host_delay = max(0.0, float(host_delay))
    _orig_fetch = web_listings._fetch
    web_listings._fetch = _polite_fetch
    log.info("concurrent: starting %d sources (workers=%d, host_delay=%.1fs).",
             len(sources), max_workers, _host_delay)
    try:
        results: dict = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_one, src, budget_per_min, ctx): src
                       for src in sources}
            for fut in as_completed(futures):
                sid, result = fut.result()
                results[sid] = result
        ok = sum(1 for r in results.values() if r["status"] == "ok")
        log.info("concurrent: done (%d/%d ok).", ok, len(results))
        return results
    finally:
        web_listings._fetch = _orig_fetch
        _orig_fetch = None
