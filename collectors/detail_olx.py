"""
OLX detail-page enricher (access route: web_public).

Fetches ONE OLX detail page per listing and returns a small, audited field
set drawn from the page's own embedded payloads:

  * ``window['dataLayer']`` offerdetail payload  -> seller/ad/location facts
  * ``window.state`` -> ``adManagement.sellerProfile.data`` -> seller facts
  * server-rendered DOM -> full description text (``aria-label="Description"``)

Derived from the Wave-A detail-page audit
(``~/workspace/paklead_ops/fleet/a10_olx_detail_audit.md``): the search-page
JSON-LD ``/ad/<slug>-ID<digits>.html`` URLs are STALE (HTTP 404); the live
scheme is ``/item/<slug>-iid-<digits>`` (DOM card hrefs). This module
resolves the live URL per listing before fetching.

HARD BOUNDARIES (SHR-1, SHR-14):
  * Page GET only. Never click the "Show phone" button and never call any
    phone-reveal / chat endpoint.
  * Never fetch ``search.olxpk.net``, any search-backend host, or use any
    embedded credential — the page source is parsed with a narrow regex
    that only extracts the ``sellerProfile`` object, so ``window.state``
    is never read wholesale.
  * The phone number is captured ONLY when ``isPhoneNumberVisible`` is
    explicitly ``true`` (the seller opted into visibility; no interaction
    is performed). Otherwise the field stays absent.
  * Ad creation/posted timestamps do not exist on the page and are never
    inferred. Price history does not exist on olx.com.pk.

POLITENESS:
  * Honest ``User-Agent`` (``PakLeadEngine/1.0``), no cookies, no login.
  * Per-host delay is enforced at a floor of 2.0 seconds between fetches.
  * A 403, 429, or CAPTCHA challenge is treated as a block: the listing
    fails with an ``error_class`` and there are NO retries.

INTEGRATION CONTRACT (the coordinator wires the caller; this module never
calls the pipeline or the DB):
  * Call ``enrich_leads`` ONLY for new HOT leads (score >= 70) or leads
    missing contactability, at most ~20 enrichments per cycle.
  * Each returned field set merges into the lead's detail JSON store
    (destination column/key chosen by the coordinator).
  * ``enrich`` is pure fetch+parse: it never writes to any database.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import re
import time
import urllib.request

log = logging.getLogger("paklead.detail_olx")

UA = "PakLeadEngine/1.0 (+https://github.com/bushra19953/paklead-engine)"
HOST = "www.olx.com.pk"

MIN_DELAY_SECONDS = 2.0
DEFAULT_BUDGET_PER_CYCLE = 20

# Stale search-page scheme vs live DOM-card scheme (audit §0).
_AD_URL_RE = re.compile(r"^https?://(?:www\.)?olx\.com\.pk/ad/(.+?)-ID(\d+)\.html$")
_ITEM_URL_RE = re.compile(r"^https?://(?:www\.)?olx\.com\.pk/item/(.+?)-iid-(\d+)$")

# dataLayer offerdetail payload and the sellerProfile state object.
_DATALAYER_PUSH_RE = re.compile(r"window\['dataLayer'\]\.push\(")
_SELLER_PROFILE_RE = re.compile(r'"sellerProfile"\s*:\s*\{"')
_POSTED_BY_RE = re.compile(r"Posted by\s+([^\n<]{1,80})", re.I)

# Markers of a bot challenge in the returned HTML. On a hit we stop with
# error_class="captcha_challenge" — never solve, never retry.
CAPTCHA_MARKERS = (
    "g-recaptcha", "hcaptcha", "data-sitekey",
    "verify you are a human", "please confirm you are a human",
    "unusual traffic", "perimeterx", "px-captcha",
)


# --------------------------------------------------------------------------
# URL resolution
# --------------------------------------------------------------------------
def resolve_detail_url(url: str) -> tuple[str | None, bool]:
    """Resolve a stored OLX URL to the live detail-page URL.

    The search collector stores ``/ad/<slug>-ID<digits>.html`` URLs from
    search-page JSON-LD; those are dead (HTTP 404). The live scheme is
    ``/item/<slug>-iid-<digits>``. Rewrites the dead form to the live form
    and returns ``(resolved_url, was_rewritten)``. Returns ``(None, False)``
    for anything that is neither scheme (never fetch those).
    """
    url = (url or "").strip()
    m = _AD_URL_RE.match(url)
    if m:
        slug, digits = m.group(1), m.group(2)
        return f"https://www.olx.com.pk/item/{slug}-iid-{digits}", True
    if _ITEM_URL_RE.match(url):
        return url, False
    return None, False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _fetch(url: str, timeout: int = 30) -> tuple[int | None, str | None, str | None]:
    """Plain honest-UA GET. Returns (status, text, error_class).

    A 403/429/CAPTCHA marker is a block, not an obstacle: no retries.
    A 5xx gets ONE backoff retry, then stops.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        if e.code in (403, 429):
            return e.code, None, f"http_{e.code}_blocked"
        if 500 <= e.code < 600:
            time.sleep(MIN_DELAY_SECONDS * 2)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp2:
                    status2 = resp2.status
                    ctype2 = resp2.headers.get("Content-Type", "")
                    raw2 = resp2.read()
            except Exception as e2:
                return None, None, f"http_error:{type(e2).__name__}"
            if status2 == 200:
                return 200, _decode(raw2, ctype2), None
            return status2, None, "http_5xx"
        return e.code, None, f"http_{e.code}"
    except Exception as e:
        return None, None, f"http_error:{type(e).__name__}"
    if status in (403, 429):
        return status, None, f"http_{status}_blocked"
    if status == 404:
        return status, None, "http_404"
    if status != 200:
        return status, None, "http_unexpected"
    return status, _decode(raw, ctype), None


def _decode(raw: bytes, ctype: str) -> str:
    """Decode a response body using the declared charset."""
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype or "")
    if m:
        charset = m.group(1)
    return raw.decode(charset, errors="replace")


def _looks_like_captcha(text: str) -> bool:
    """True if the page text carries a bot-challenge marker."""
    low = text.lower()
    return any(marker in low for marker in CAPTCHA_MARKERS)


# --------------------------------------------------------------------------
# Embedded-payload extraction
# --------------------------------------------------------------------------
def _brace_json(text: str, open_idx: int) -> str | None:
    """Return the balanced ``{...}`` substring starting at ``open_idx``.

    String-aware (quotes/escapes) so nested objects and literal braces in
    strings do not break the balance count.
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
    return None


def _extract_data_layer(text: str) -> dict | None:
    """The dataLayer push carrying ``page_type=offerdetail`` (5/5 pages).

    Every push candidate is brace-matched and JSON-parsed individually; the
    rest of the page is never JSON-parsed.
    """
    for m in _DATALAYER_PUSH_RE.finditer(text):
        open_idx = text.find("{", m.end())
        if open_idx < 0:
            continue
        chunk = _brace_json(text, open_idx)
        if not chunk:
            continue
        try:
            payload = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(payload, dict) and (
                payload.get("page_type") == "offerdetail" or "seller_id" in payload):
            return payload
    return None


def _extract_seller_profile(text: str) -> dict | None:
    """``window.state.adManagement.sellerProfile.data`` (5/5 pages).

    Only the ``sellerProfile`` object is extracted — ``window.state`` as a
    whole is multi-MB and embeds a search-backend credential, which we
    deliberately never read, record, or use. The brace-matched object IS
    the sellerProfile node ({"loading", ..., "data"}), so its ``data``
    member is returned directly.
    """
    m = _SELLER_PROFILE_RE.search(text)
    if not m:
        return None
    open_idx = text.find("{", m.start())
    if open_idx < 0:
        return None
    chunk = _brace_json(text, open_idx)
    if not chunk:
        return None
    try:
        node = json.loads(chunk)
    except ValueError:
        return None
    if isinstance(node, dict) and isinstance(node.get("data"), dict):
        return node["data"]
    return None


def _extract_description(text: str) -> str | None:
    """Full ad description from the server-rendered DOM (5/5).

    Anchors on ``aria-label="Description"`` and takes the visible text up
    to "Related ads" — robust against the container tag changing (it has
    been both <section> and <div>). Does NOT use
    ``<meta name="description">`` (truncated ~160 chars).
    """
    hook = text.find('aria-label="Description"')
    if hook < 0:
        return None
    # Start after the opening tag's closing bracket so no attribute text
    # (e.g. `aria-label="Description"`) leaks into the stripped output.
    tag_end = text.find(">", hook)
    if tag_end < 0:
        return None
    start = tag_end + 1
    end = text.find("Related ads", start)
    window = text[start:end] if end > 0 else text[start:start + 12000]
    body = re.sub(r"<script.*?</script>", " ", window, flags=re.I | re.S)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    # Drop the "Description" heading word the container itself renders.
    body = re.sub(r"^Description\s+", "", body, flags=re.I)
    return body or None


def _posted_by(text: str) -> str | None:
    """Seller display name from the DOM fallback block."""
    m = _POSTED_BY_RE.search(text)
    return m.group(1).strip() if m else None


def _yesno(value) -> bool | None:
    """OLX "yes"/"no" flag strings -> bool."""
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------
def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _enrich_with_error(listing: dict, timeout: int,
                       fetcher) -> tuple[dict | None, str | None]:
    """Fetch + parse ONE OLX detail page. Returns (detail_dict, error_class).

    error_class is None on success. Block-class failures (403/429/CAPTCHA)
    and all other failures return ``(None, error_class)`` with no retries
    for blocks. Explicitly NOT captured: the "Show phone" button, any
    search-backend host/credential, ad creation timestamps, price history.
    """
    url, rewritten = resolve_detail_url(listing.get("url") or "")
    if not url:
        return None, "bad_url"
    status, text, err = fetcher(url, timeout=timeout)
    if err is not None:
        return None, err
    if status is None or text is None:
        return None, "fetch_failed"
    if _looks_like_captcha(text):
        log.warning("olx detail blocked by captcha challenge: %s", url)
        return None, "captcha_challenge"

    dl = _extract_data_layer(text) or {}
    sp = _extract_seller_profile(text) or {}
    description = _extract_description(text)

    seller_name = ((sp.get("name") or "").strip() or _posted_by(text))
    phone_visible = sp.get("isPhoneNumberVisible") is True
    package_type = dl.get("package_type")  # "featured" or None (= organic)

    detail = {
        "platform": "olx",
        "platform_post_id": listing.get("platform_post_id"),
        "ad_id": dl.get("ad_id"),
        "url_resolved": url,
        "url_rewritten_from_ad_scheme": rewritten,
        "seller_type": dl.get("seller_type"),
        "is_agent": _yesno(dl.get("is_agent")),
        "seller_verified": _yesno(dl.get("seller_verified")),
        "seller_name": seller_name,
        "member_since": sp.get("createdAt"),  # seller account creation (ISO)
        "active_ad_count": sp.get("activeAdCount"),
        "number_of_photos": dl.get("number_of_photos"),
        "package_type": package_type,
        "is_featured": package_type == "featured",
        "price": dl.get("price"),
        "currency": dl.get("currency_unit"),
        "rooms": dl.get("rooms"),
        "bathrooms": dl.get("bathrooms"),
        "size_value": dl.get("ft"),
        "size_unit": dl.get("ft_unit"),
        "furnished": _yesno(dl.get("furnished")),
        "has_video": _yesno(dl.get("video")),
        "has_panorama": _yesno(dl.get("panorama")),
        "ownership_type": dl.get("ownership_type"),
        "loc": {
            "loc_id": dl.get("loc_id"),
            "loc_name": dl.get("loc_name"),
            "loc_breadcrumb": dl.get("loc_breadcrumb"),
            "loc_1_id": dl.get("loc_1_id"),
            "loc_1_name": dl.get("loc_1_name"),
            "loc_2_id": dl.get("loc_2_id"),
            "loc_2_name": dl.get("loc_2_name"),
            "loc_3_id": dl.get("loc_3_id"),
            "loc_3_name": dl.get("loc_3_name"),
            "loc_4_id": dl.get("loc_4_id"),
            "loc_4_name": dl.get("loc_4_name"),
        },
        "description": description,
        "enriched_at": _iso_now(),
        "source": "olx_detail",
    }
    # Phone ONLY when the seller explicitly made it visible. The "Show
    # phone" button is never clicked; this is the plain-text value the
    # seller opted to publish on the page.
    if phone_visible:
        detail["phone"] = sp.get("phoneNumber")
        detail["phone_visible"] = True
    return detail, None


def enrich(listing: dict, delay_seconds: float = 2.5,
           timeout: int = 30) -> dict | None:
    """Fetch ONE OLX detail page and return the audited field set, or None.

    ``listing`` must carry ``url`` and ``platform_post_id``. ``delay_seconds``
    is the politeness pause the CALLER applies between calls (floored at
    2.0s inside ``enrich_leads``); a single call does not sleep. Returns the
    field dict on success, None on any failure (block, 404, parse failure).
    Pure fetch+parse — never touches the DB.
    """
    detail, _err = _enrich_with_error(listing, timeout, _fetch)
    return detail


def enrich_leads(listings: list[dict], budget: int = DEFAULT_BUDGET_PER_CYCLE,
                 delay_seconds: float = 2.5, timeout: int = 30,
                 fetcher=None) -> dict:
    """Enrich up to ``budget`` listings, politely and budget-capped.

    Sleeps ``delay_seconds`` (floored at 2.0s) between page fetches; stops
    hard at ``budget`` pages. Returns::

        {"results": {platform_post_id: detail_dict, ...},
         "failed":  {platform_post_id: error_class, ...},
         "skipped": [platform_post_id, ...],   # beyond the budget
         "fetched": <pages fetched>, "budget": <budget>}

    ``fetcher`` is an injectable ``(url, timeout) -> (status, text, err)``
    for tests; production uses the honest-UA ``_fetch``.
    """
    budget = max(0, int(budget))
    delay = max(MIN_DELAY_SECONDS, float(delay_seconds or 0))
    fetch = fetcher or _fetch
    results: dict[str, dict] = {}
    failed: dict[str, str] = {}
    skipped: list[str] = []
    fetched = 0
    for idx, listing in enumerate(listings):
        pid = str(listing.get("platform_post_id") or "")
        if idx >= budget:
            skipped.append(pid)
            continue
        if idx > 0:
            time.sleep(delay)
        detail, err = _enrich_with_error(listing, timeout, fetch)
        fetched += 1
        if detail is not None:
            results[pid] = detail
        else:
            failed[pid] = err or "unknown"
    return {"results": results, "failed": failed, "skipped": skipped,
            "fetched": fetched, "budget": budget}
