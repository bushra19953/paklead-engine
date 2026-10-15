"""
Graana detail-page enricher (access route: web_public).

Fetches individual Graana detail pages (/property/...-{id}/) and parses
their static __NEXT_DATA__ payload into a flat detail field set for HOT
leads. Companion to the city-level search profile in web_listings.py
(`_parse_graana`): search pages provide volume, this module provides
depth on a small, budgeted subset.

INTEGRATION CONTRACT (coordinator-owned wiring):
- Run ONLY on new HOT leads (score >= HOT threshold) that lack a
  "graana_detail" entry in their lead detail JSON — detail pages are
  ~110 KB each; the city search stays the volume route.
- Batch budget: max ~20 detail fetches per cycle (DETAIL_BUDGET).
  Detail fetches count against the same per-source politeness budget as
  the sweep cron.
- Merge: the coordinator writes the enriched dict returned by
  enrich() under the DETAIL_KEY ("graana_detail") in the lead's detail
  JSON; this module never writes to the database itself.
- Contact fields (agency/agents/listing phones, emails) are the seller's
  OWN published contact data from the static payload, stored as
  contact_published fields for triage only. NO automated outreach is
  ever triggered from enriched data (SHR-14).

DO-NOT-CAPTURE (explicit exclusions):
- Anything behind a click-to-reveal / interaction (nothing is clicked,
  no forms are submitted; only the static __NEXT_DATA__ JSON is parsed).
- Anything not in the static payload (no extra requests beyond the one
  GET of the detail page itself).
- Internal identifiers: agencyId/agentId/userId/systemUserId/riderId/
  propsureId, agent Cognito internals (sub, Username, custom:* etc.),
  agency backend fields (cloudinaryPublicId, createdAt/deletedAt/updatedAt).
- Only image URLs are counted (count stored), not downloaded or stored.

Block handling: 403 / 429 / CAPTCHA markers -> None + error_class, no
retries (SHR-1: a block is an answer, not an obstacle).

Verified live 2026-10-15 against the detail route documented in
~/workspace/paklead_ops/fleet/a12_graana_towns.md.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger("paklead.detail_graana")

UA = "PakLeadEngine/1.0 (+https://github.com/bushra19953/paklead-engine)"
DETAIL_KEY = "graana_detail"
DETAIL_BUDGET = 20            # max detail fetches per batch run
DETAIL_DELAY = 2.0            # minimum seconds between same-host fetches
CAPTCHA_RE = re.compile(r"captcha|challenge-platform|cfrclr|datadome",
                        re.I)

_next_data_re = re.compile(
    r'id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

#: Agent-contact keys we copy from agentDetails (Cognito user record).
#: Everything else in that record (sub, Username, custom:*, etc.) is
#: internal and excluded.
_AGENT_KEYS = ("name", "family_name", "phone_number",
               "phone_number_verified")

#: Room/feature group keys copied verbatim from the payload.
_FEATURE_GROUPS = ("primaryFeatures", "secondaryFeatures", "utilityFeatures",
                   "nearByFeatures", "communicationFeatures")


def _fetch(url: str, timeout: int = 30) -> tuple[int | None, str | None, str | None]:
    """Honest-UA GET of a single detail page.

    Returns (status, html, error_class). error_class is None on success.
    Blocks (403/429/CAPTCHA) are reported, never retried.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", None)
        if code in (403, 429):
            return code, None, f"http_{code}_blocked"
        return None, None, f"http_{code or 'unknown'}_fetch_failed"
    except Exception as e:  # network/DNS/timeout: report, don't raise
        return None, None, f"http_error:{type(e).__name__}"
    text = raw.decode("utf-8", errors="replace")
    # B33: challenge-shaped markers only — bare "captcha" matches the
    # benign STRAT_WITH_CAPTCHA JS flag Zameen ships on healthy pages.
    from collectors.retry import challenge_markers_found
    if challenge_markers_found(text):
        return status, None, "captcha_block"
    return status, text, None


def _extract_payload(text: str) -> dict | None:
    """Pull the detail record out of __NEXT_DATA__: props.pageProps.data.

    Returns None when the payload is absent or unparsable (route changed).
    """
    m = _next_data_re.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        detail = (data.get("props") or {}).get("pageProps") or {}
        detail = detail.get("data")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, dict) else None


def _txt(v) -> str:
    """String-ify a payload value; None/empty -> ""."""
    if v is None:
        return ""
    s = str(v).strip()
    return s


def extract(detail: dict) -> dict:
    """Flatten a Graana detail payload into the recommended field set.

    Pure function (no network) — takes the props.pageProps.data dict and
    returns the enrich field set. Contact fields are copied ONLY when the
    payload itself publishes them; internal ids are dropped.
    """
    if not isinstance(detail, dict):
        detail = {}

    agency = detail.get("agency") if isinstance(detail.get("agency"), dict) else {}
    agent = (detail.get("agentDetails")
             if isinstance(detail.get("agentDetails"), dict) else {})

    agency_contact = {}
    for k in ("name", "phone", "email", "website", "address"):
        v = _txt(agency.get(k))
        if v:
            agency_contact[k] = v
    agency_city = _txt((agency.get("city") or {}).get("name")) \
        if isinstance(agency.get("city"), dict) else ""
    if agency_city:
        agency_contact["city"] = agency_city

    agent_contact = {}
    for k in _AGENT_KEYS:
        v = agent.get(k)
        if k == "phone_number_verified":
            agent_contact[k] = bool(v)
        else:
            v = _txt(v)
            if v:
                agent_contact[k] = v

    images = detail.get("propertyImages")
    image_count = len(images) if isinstance(images, list) else 0

    features = {}
    for group in _FEATURE_GROUPS:
        g = detail.get(group)
        if isinstance(g, dict) and g:
            features[group] = g

    area = detail.get("area") if isinstance(detail.get("area"), dict) else {}
    city = detail.get("city") if isinstance(detail.get("city"), dict) else {}

    out = {
        "detail_url": "",            # filled in by enrich()
        "graana_id": _txt(detail.get("id")),
        "description": _txt(detail.get("description")),
        "agency_contact": agency_contact,   # name/phone/email/website/addr/city if published
        "agent_contact": agent_contact,     # name/family_name/phone(_verified) if published
        "listing_phone": _txt(detail.get("phone")),
        "built_year": _txt(detail.get("builtYear")),
        "condition": _txt(detail.get("condition")),
        "availability": _txt(detail.get("propertyAvailability")),
        "expires_at": _txt(detail.get("expiresAt")),
        "days_on_graana": detail.get("daysOnGraana"),
        "lat": detail.get("lat"),
        "lng": detail.get("lng"),
        "address": _txt(detail.get("address")),
        "area": _txt(area.get("name")),
        "city": _txt(city.get("name")),
        "bed": detail.get("bed"),
        "bath": detail.get("bath"),
        "features": features,
        "image_count": image_count,
        "status": _txt(detail.get("status")),
        "enriched_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    return out


#: Most recent error class from enrich(); None when the last call succeeded.
last_error: str | None = None


def enrich(listing: dict, timeout: int = 30) -> dict | None:
    """Enrich one Graana listing from its detail page.

    listing must carry "url" (the /property/...-{id}/ page) and
    "platform_post_id". Returns the field set (see extract()) with
    "detail_url" filled, or None on any failure/block; the reason is left
    in module attribute `last_error`. Never raises on HTTP problems.
    """
    global last_error
    last_error = None
    url = (listing or {}).get("url") or ""
    if not url:
        last_error = "missing_detail_url"
        log.warning("detail_graana: listing %s has no url.", listing.get("platform_post_id"))
        return None
    status, text, err = _fetch(url, timeout=timeout)
    if err:
        last_error = err
        log.warning("detail_graana: %s -> %s", url, err)
        return None
    if status != 200 or not text:
        last_error = "http_unexpected"
        log.warning("detail_graana: unexpected status %s: %s", status, url)
        return None
    detail = _extract_payload(text)
    if detail is None:
        last_error = "payload_missing"
        log.warning("detail_graana: no __NEXT_DATA__ detail payload: %s", url)
        return None
    out = extract(detail)
    out["detail_url"] = url
    if out["graana_id"]:
        pid = str(listing.get("platform_post_id") or "")
        if pid and pid != out["graana_id"]:
            log.warning("detail_graana: payload id %s != listing id %s.",
                        out["graana_id"], pid)
    return out


def run_batch(listings: list[dict], budget: int = DETAIL_BUDGET,
              delay: float = DETAIL_DELAY) -> dict:
    """Enrich up to `budget` listings, politely, with block handling.

    Contract: caller passes ONLY new HOT leads lacking a "graana_detail"
    entry (scope is the caller's job, documented in the module docstring).
    The budget cap is enforced here regardless: listings beyond `budget`
    are reported as "skipped_budget". Per-host delay is at least 2 s.
    On a block (403/429/CAPTCHA) the batch aborts early and reports the
    remaining listings as "skipped_block" — no retries.

    Returns {"enriched": [fieldsets], "skipped": {reason: count},
             "error_classes": {class: count}, "budget": budget,
             "fetched": n}.
    """
    global last_error
    delay = max(2.0, float(delay))
    budget = max(1, int(budget))
    out = {"enriched": [], "skipped": {}, "error_classes": {},
           "budget": budget, "fetched": 0}

    def skip(reason: str) -> None:
        out["skipped"][reason] = out["skipped"].get(reason, 0) + 1

    for i, listing in enumerate(listings or []):
        if out["fetched"] >= budget:
            skip("skipped_budget")
            continue
        if i > 0:
            time.sleep(delay)
        out["fetched"] += 1
        got = enrich(listing)
        err = last_error
        if got is None:
            cls = err or "unknown"
            out["error_classes"][cls] = out["error_classes"].get(cls, 0) + 1
            if cls in ("http_403_blocked", "http_429_blocked", "captcha_block"):
                # A block is an answer: abort the batch, report the rest.
                for _ in (listings or [])[i + 1:]:
                    skip("skipped_block")
                log.warning("detail_graana: block (%s) — batch aborted at %d/%d.",
                            cls, i + 1, len(listings or []))
                break
            continue
        out["enriched"].append(got)
    return out


def detail_key() -> str:
    """Detail-JSON key the coordinator merges enriched payloads under."""
    return DETAIL_KEY
