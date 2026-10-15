"""
Transient-failure retry policy for PakLead web collectors (B33, 2026-10-15).

ONE consistent policy shared by ``web_listings._fetch`` and the three
detail enrichers (``detail_olx``, ``detail_zameen``, ``detail_graana``).
It replaces the collectors' current ad-hoc handling, which differs per
module:

  * ``web_listings._fetch``   — 5xx got one ``sleep(delay * 2)`` retry, then
    stop; 403/429 stopped the source with no retry.
  * ``detail_olx._fetch``     — 5xx got one ``sleep(MIN_DELAY_SECONDS * 2)``
    retry, then reported ``http_5xx``; 403/429 were blocks, no retry.
  * ``detail_zameen._fetch``  — NO 5xx retry at all (returned
    ``http_error_<code>``); 403/429/CAPTCHA/empty-body-200 were blocks,
    no retry, batch aborted.
  * ``detail_graana._fetch``  — NO 5xx retry at all; 403/429/CAPTCHA were
    blocks, no retry, batch aborted.

Unifying them matters because a 500 from Zameen during a cycle and a 500
from OLX during a cycle are the same event — a transient host wobble —
and deserve the same response: bounded backoff retries, then stop.

THIS MODULE NEVER FETCHES AND NEVER TOUCHES THE DATABASE. It is pure
policy: ``should_retry`` (the decision), ``backoff_delay`` (the timing),
``challenge_markers_found`` (the challenge detector). The coordinator
wires these into the collectors' ``_fetch`` functions — see
``INTEGRATION CONTRACT`` below.

SHR-1 boundary (hard): a block is an answer, not an obstacle. 403/429 and
a bot challenge are NEVER retried and NEVER evaded. A 404 is not
transient for our purposes (``web_listings`` treats a mid-run 404 as
break-and-keep pagination signal).
"""

from __future__ import annotations

import logging
import random
import re
import time

log = logging.getLogger("paklead.retry")

#: HTTP statuses that are genuinely transient: the host wobbled, it will
#: probably answer the same URL again soon. 500/502/503/504 only —
#: anything else with a 5xx is not in this set by design (documented
#: where it appears).
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

#: Statuses that are a BLOCK (stop, never retry): the host is telling us
#: to go away. 429 counts as a block, not a throttle signal — SHR-1 says
#: no rate-limit evasion of any kind.
BLOCK_STATUSES = frozenset({403, 429})

#: Total attempts per URL (initial try + retries). A 503 therefore retries
#: exactly twice, then stops (attempts 0, 1 retried; attempt 2 stops).
MAX_ATTEMPTS = 3

#: Base of the exponential backoff in seconds (see backoff_delay).
BACKOFF_BASE = 2.0

#: Cap of the EXPONENTIAL component of the delay (see backoff_delay).
MAX_BACKOFF = 60.0

#: Jitter window in seconds, added on top of the capped exponential.
#: Total worst-case delay for one retry is therefore
#: MAX_BACKOFF + JITTER_MAX = 61.0 seconds.
JITTER_MAX = 1.0

#: Challenge markers for ``challenge_markers_found``. Challenge-shaped
#: patterns ONLY — deliberately none of these is the bare word "captcha".
#: Zameen pages carry the JS config flag ``STRAT_WITH_CAPTCHA:true`` on
#: ordinary healthy result pages (A09), so a naive "captcha" substring
#: match would false-positive on every fetch. Same marker set as
#: ``web_listings._CHALLENGE_MARKERS`` so search and detail pages agree.
CHALLENGE_MARKERS = (
    "cf-challenge",
    "challenge-platform",
    "just a moment",
    "verify you are human",
    "are you a robot",
    "recaptcha",
    "hcaptcha",
    "perimeterx",
    "px-captcha",
    "datadome",
    "arkose",
    "captcha-delivery",
    "enter the characters you see",
    "security check",
)


def should_retry(status: int | None, attempt: int) -> bool:
    """Decide whether a failed fetch deserves another try.

    Retry ONLY on 500/502/503/504, or on a transport-level failure
    (``status`` is ``None``: connection timeout, DNS failure, connection
    reset — the network broke, not the host). NEVER on:

      * 403 / 429 — a block is an answer, not an obstacle (SHR-1);
      * 404 — not transient for our purposes (``web_listings`` treats a
        mid-run 404 as break-and-keep: stop pagination, keep records);
      * CAPTCHA markers — a challenge is a block (see
        ``challenge_markers_found``), never solved, never retried;
      * any other status.

    ``attempt`` is zero-based: 0 for the first attempt, 1 after the first
    retry. At most ``MAX_ATTEMPTS`` attempts happen in total, so a 503
    retries exactly twice then stops. Negative attempts are treated as 0
    (defensive: a caller that miscounts still gets a sane answer).
    """
    attempt = max(0, int(attempt))
    if attempt >= MAX_ATTEMPTS - 1:
        return False
    if status is None:
        # Transport failure (timeout, DNS, connection reset): retriable.
        return True
    if status in BLOCK_STATUSES:
        return False
    if status == 404:
        return False
    return status in RETRYABLE_STATUSES


def backoff_delay(attempt: int, base: float = BACKOFF_BASE) -> float:
    """Seconds to sleep before attempt ``attempt`` + 1.

    Exponential backoff (``base * 2**attempt``) plus a small uniform
    jitter in ``[0, JITTER_MAX]`` to avoid thundering-herd re-hits.

    THE CAP: the exponential component is capped at ``MAX_BACKOFF``
    (60.0s); the jitter is added AFTER the cap, so the total worst-case
    delay for one retry is ``MAX_BACKOFF + JITTER_MAX`` = 61.0 seconds.
    Growth example (base=2.0, before jitter): attempt 0 -> 2s,
    attempt 1 -> 4s, attempt 2 -> 8s, ... attempt 10+ -> 60s (capped).
    ``base <= 0`` is clamped to ``BACKOFF_BASE`` so a misconfigured
    caller cannot produce a zero/negative sleep.
    """
    attempt = max(0, int(attempt))
    base = float(base)
    if base <= 0:
        base = BACKOFF_BASE
    exponential = min(base * (2.0 ** attempt), MAX_BACKOFF)
    jitter = random.uniform(0.0, JITTER_MAX)
    return exponential + jitter


def challenge_markers_found(html: str) -> bool:
    """True when the page HTML carries a bot-challenge marker.

    Challenge-shaped patterns ONLY (``CHALLENGE_MARKERS``) — the bare
    word ``"captcha"`` is deliberately NOT a marker. Zameen ships the JS
    config flag ``STRAT_WITH_CAPTCHA:true`` on ordinary healthy result
    pages, so matching bare "captcha" would flag every Zameen page as
    challenged. (The flag ``STRAT_WITH_CAPTCHA:true`` alone — with no
    challenge-shaped marker — returns False: verified in tests.)

    This mirrors the behavior of ``web_listings._looks_challenged`` minus
    the content-hook check: that check needs the platform and the page's
    listing hooks, which the caller owns. Callers should treat a True
    here as a block (``error_class="web_challenge"``), not as an
    invitation to solve anything.
    """
    low = (html or "").lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


# ----------------------------------------------------------------------
# INTEGRATION CONTRACT (the coordinator wires the collectors; this module
# is policy only and never calls the code below itself).
# ----------------------------------------------------------------------
def example_fetch(url: str, timeout: int = 30) -> tuple:
    """HOW CALLERS WIRE THE POLICY — a 10-line reference wrapper.

    ``web_listings._fetch`` and the three detail enrichers keep their own
    honest-UA ``_raw_get`` — policy-free, returning ``(status, text,
    None)`` on HTTP responses and ``(None, None, err)`` on transport
    failures. The policy layer wraps it like this (10 lines)::

        def _fetch_with_policy(url, timeout=30):
            # policy-free _raw_get: (status, text, None) or (None, None, err)
            for attempt in range(MAX_ATTEMPTS):
                status, text, err = _raw_get(url, timeout)
                if not should_retry(status, attempt): break
                time.sleep(backoff_delay(attempt))
            if status in (403, 429): return status, None, f"http_{status}_blocked"
            if status == 404: return status, None, "http_404"  # break-and-keep
            if text and challenge_markers_found(text): return status, None, "web_challenge"
            return status, text, err

    Required error_class vocabulary (keeps monitors uniform):
      * ``http_<code>_blocked``  for 403 / 429 — the block classes.
      * ``http_404``             for 404 — NOT retried; for ``web_listings``
        this is the break-and-keep pagination signal (records fetched so
        far are returned with status ok and a note).
      * ``web_challenge``        for challenge markers — never solved,
        never retried.
      * ``http_5xx``             when the bounded retries are exhausted
        (or when a caller opts out of retries).
      * ``http_error:<ExcName>`` for transport failures that persist.

    Do NOT let a detail enricher use the bare-word ``"captcha"``
    substring (``detail_graana.CAPTCHA_RE`` currently does — the
    coordinator should replace it with ``challenge_markers_found`` when
    wiring the policy).
    """
    raise NotImplementedError(
        "Policy module only — the coordinator wires this pattern into "
        "web_listings._fetch and the three detail enrichers.")
