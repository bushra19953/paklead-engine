"""Zameen detail-page enricher (access route: web_public).

Fetches PUBLIC Zameen listing detail pages with a plain HTTP client and
parses only what the page itself serves: the embedded
``window['dataLayer']`` push object (the richest, most stable source —
price, agency, seller type, beds/baths, areas, title, reference id,
lat/long, package tier, location hierarchy) plus two optional DOM
sections — the full property description
(``div[aria-label="Property description"]``) and the Amenities section.

HARD BOUNDARIES (verified by Wave-A audit A11, 2026-10-15):
- Never capture phone numbers / contact details. Zameen masks them behind
  an interactive reveal by design; mining them is ToS-abusive (SHR-1).
  Zameen-lead contactability stays "unknown", never fabricated.
- No listing-creation timestamp exists on Zameen detail pages (dataLayer
  and DOM both checked). Store ``enriched_at`` honestly instead; do not
  infer listing age.
- Area values (``property_land_area`` / ``property_area``) are stored RAW
  with a unit-ambiguity note; never presented as converted facts.
- When the dataLayer and the DOM amenity counts disagree (observed on
  real pages), keep BOTH — neither is treated as authoritative over the
  other.
- Polite access: per-host delay >= 2s, batch budget capped at ~20 pages
  per run, block handling (403 / 429 / CAPTCHA markers / empty-body 200)
  -> error_class and NO retries. A block is an answer, not an obstacle.

INTEGRATION CONTRACT (same shape as the OLX detail enricher):
- The coordinator selects enrichment candidates: NEW or top-scored HOT
  leads from the ``zameen`` platform only, capped at ~20 per enrichment
  cycle.
- Each candidate is a dict with at least::
      {"platform_post_id": <zameen listing id>, "url": <detail page url>}
- ``enrich(listing) -> dict | None``. The returned dict carries ONLY
  fields actually observed on the page (missing fields are omitted, never
  fabricated). The coordinator merges it into the lead's detail JSON
  (``lead["detail"].update(...)``) without overwriting fields from a
  higher-authority source, and the dict always carries ``enriched_at``.
- This module never touches the database; the coordinator wires the merge.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import re
import time
import urllib.request

log = logging.getLogger("paklead.detail_zameen")

UA = "PakLeadEngine/1.0 (+https://github.com/bushra19953/paklead-engine)"

# --- budget / politeness ---------------------------------------------------
BATCH_MAX = 20          # never enrich more than ~20 pages per run
MIN_DELAY_S = 2.0       # per-host delay floor, seconds
REQUEST_TIMEOUT = 30

# Markers that mean "the host is serving a bot challenge" (SHR-1: no
# retries, stop). NOTE: a bare "captcha" substring is NOT a marker —
# Zameen's own config JSON carries "STRAT_WITH_CAPTCHA":true on healthy
# pages, so only challenge-shaped patterns count.
_BLOCK_PATTERNS = [
    re.compile(r"\bcf[-_]chl\b", re.I),      # cloudflare challenge path
    re.compile(r"\bg-recaptcha\b", re.I),    # real recaptcha widget markup
    re.compile(r"perimeterx", re.I),
    re.compile(r"\bpx-captcha\b", re.I),
    re.compile(r"verify you are human", re.I),
    re.compile(r"are you a robot", re.I),
    re.compile(r"unusual traffic", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"request blocked", re.I),
    re.compile(r"security check", re.I),
]

# --- regexes ---------------------------------------------------------------
_DATALAYER_RE = re.compile(
    r"""window\[['"]dataLayer['"]\]\s*=\s*window\[['"]dataLayer['"]\]\s*\|\|\s*\[\];\s*"""
    r"""window\[['"]dataLayer['"]\]\.push\(\{""",
    re.S)
_KV_RE = re.compile(r'"([a-zA-Z0-9_]+)"\s*:\s*("(?:[^"\\]|\\.)*"|[^",}\]]*)')
_TAG_RE = re.compile(r"<[^>]+>")
_BUILT_YEAR_RE = re.compile(r"Built in year\s*:?\s*(\d{4})", re.I)
_AMENITY_CATEGORIES = (
    "Main Features", "Rooms", "Business and Communication",
    "Community Features", "Healthcare Recreational",
    "Nearby Locations & Other Facilities", "Other Facilities",
    "Flooring", "Utilities", "Waste Disposal")
_AMENITY_CAT_RE = re.compile(
    r"<div[^>]*>\s*(" + "|".join(re.escape(c) for c in _AMENITY_CATEGORIES) +
    r")\s*</div>", re.I)
_AMENITY_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_AMENITY_SKIP = frozenset({"show more", "show less", "amenities"})


# --------------------------------------------------------------------------
def _iso_now() -> str:
    """Current UTC time as an ISO string (the honest ``enriched_at`` stamp)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _fetch(url: str, timeout: int = REQUEST_TIMEOUT
           ) -> tuple[int | None, str | None, str | None]:
    """Plain honest-UA GET of a detail page.

    Returns ``(status, html_text, error_class)``. A block is reported, not
    retried: 403/429, CAPTCHA markers in the body, and the empty-body-200
    rate-limit signature all come back as an error_class.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        code = e.code
        if code in (403, 429):
            return code, None, f"http_{code}_blocked"
        return code, None, f"http_error_{code}"
    except Exception as e:
        return None, None, f"http_error:{type(e).__name__}"
    text = body.decode("utf-8", errors="replace")
    if status in (403, 429):
        return status, None, f"http_{status}_blocked"
    if any(p.search(text) for p in _BLOCK_PATTERNS):
        return status, None, "http_captcha_blocked"
    if status == 200 and not text.strip():
        # Audit-observed rate-limit signature (200 with empty body).
        return status, None, "http_200_empty"
    if status != 200:
        return status, None, "http_unexpected"
    return status, text, None


# --------------------------------------------------------------------------
def _extract_datalayer(text: str) -> dict:
    """Pull the ``window['dataLayer'].push({...})`` object from page HTML.

    Uses balanced-brace extraction from the ``.push({`` marker, then
    ``json.loads``; falls back to scalar key/value regex scraping if the
    payload is not valid JSON. Returns a flat dict (values may be
    str/int/float/bool/list).
    """
    m = _DATALAYER_RE.search(text)
    if not m:
        return {}
    start = m.end() - 1  # the opening '{'
    depth, j = 0, start
    while j < len(text):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return {}
    payload = text[start:j + 1]
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            return data
        return {}
    except ValueError:
        pass
    # Fallback: scalar-only scrape (no types, no lists).
    out: dict = {}
    for key, raw in _KV_RE.findall(payload):
        val = raw.strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            out[key] = val[1:-1]
        else:
            out[key] = val
    return out


def _clean_text(raw: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _balanced_div(text: str, open_start: int) -> str | None:
    """Return inner content of the div starting at ``open_start``.

    ``open_start`` must point at a ``<div`` tag. Counts nested ``<div`` /
    ``</div>`` pairs until the matching close. Returns ``None`` if the
    tags are unbalanced.
    """
    gt = text.find(">", open_start)
    if gt < 0 or not text.startswith("<div", open_start):
        return None
    depth, j, inner_start = 1, gt + 1, gt + 1
    while j < len(text) and depth > 0:
        if text.startswith("<div", j):
            depth += 1
            j += 4
        elif text.startswith("</div>", j):
            depth -= 1
            j += 6
        else:
            j += 1
    if depth != 0:
        return None
    return text[inner_start:j - 6]


def _parse_description(text: str) -> str | None:
    """Full property description via the stable aria-label hook.

    Returns cleaned text, or ``None`` when the section is absent (degraded
    template — 1 in 5 pages in the audit).
    """
    marker = text.find('aria-label="Property description"')
    if marker < 0:
        return None
    div_m = re.search(r'<div[^>]*dir="auto"[^>]*>', text[marker:marker + 3000])
    if not div_m:
        return None
    open_start = marker + div_m.start()
    inner = _balanced_div(text, open_start)
    if not inner:
        return None
    cleaned = _clean_text(inner)
    return cleaned or None


def _parse_amenities(text: str) -> tuple[list[dict], int | None]:
    """Amenity triples from the Amenities section, if present.

    The section markup is hook-stable enough to parse directly: category
    headers are ``<div>Main Features</div>``-style label divs and items are
    ``<span>Built in year<!-- -->: 2026</span>`` spans in document order.
    A linear walk keeps (category, name, value) triples correctly grouped.
    Hit rate in the Wave-A audit: 3/5. Returns ``(amenities, built_year)``.
    """
    h = text.find("Amenities</h3>")
    if h < 0:
        h = text.find(">Amenities<")
    if h < 0:
        return [], None
    nxt = text.find("<h3", h + 10)
    slice_ = text[h:nxt] if nxt > 0 else text[h:h + 30_000]

    events: list[tuple[int, str, str]] = []
    for m in _AMENITY_CAT_RE.finditer(slice_):
        events.append((m.start(), "cat", m.group(1).strip()))
    for m in _AMENITY_SPAN_RE.finditer(slice_):
        content = _clean_text(_COMMENT_RE.sub("", m.group(1)))
        if content and content.lower() not in _AMENITY_SKIP:
            events.append((m.start(), "item", content))
    events.sort(key=lambda e: e[0])

    amenities: list[dict] = []
    seen: set[tuple] = set()
    category = "uncategorized"
    for _, kind, payload in events:
        if kind == "cat":
            category = payload
            continue
        if ":" in payload:
            name, _, value = payload.partition(":")
            name, value = name.strip(), value.strip() or None
        else:
            name, value = payload, None
        if name:
            key = (category, name, value or "")
            if key not in seen:
                seen.add(key)
                amenities.append({"category": category, "name": name,
                                  "value": value})

    built_year = None
    m = _BUILT_YEAR_RE.search(_COMMENT_RE.sub(" ", slice_))
    if m:
        try:
            built_year = int(m.group(1))
        except ValueError:
            built_year = None
    return amenities, built_year


# --------------------------------------------------------------------------
def _to_int(value) -> int | None:
    """Coerce a dataLayer numeric (int or numeric string) to int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        v = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(\.\d+)?", v):
            return int(float(v))
    return None


def _to_float(value) -> float | None:
    """Coerce a dataLayer numeric to float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", v):
            return float(v)
    return None


def _first_int(*values) -> int | None:
    """First coercible int, digging into one level of lists."""
    for v in values:
        if isinstance(v, list):
            for item in v:
                n = _to_int(item)
                if n is not None:
                    return n
        else:
            n = _to_int(v)
            if n is not None:
                return n
    return None


def _nonempty(value) -> str | None:
    """Strip a string; ``None`` when missing or blank."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _build_detail(dl: dict, description: str | None,
                  amenities: list[dict], built_year: int | None,
                  url: str, pid: str) -> dict:
    """Assemble the enriched field set from parsed pieces.

    Every field is included ONLY when actually observed on the page;
    missing fields are omitted, never fabricated.
    """
    detail: dict = {"platform_post_id": pid, "detail_url": url}

    price = _to_int(dl.get("property_price")) or _to_int(dl.get("price"))
    if price is not None:
        detail["price_pkr"] = price

    agency = _nonempty(dl.get("marketed_by"))
    if agency:
        detail["marketed_by"] = agency  # agency display name

    seller_type = _nonempty(dl.get("seller_type"))
    if seller_type:
        # Raw value only ("business" seen 5/5; "private" unverified).
        detail["seller_type"] = seller_type

    agent_id = _nonempty(dl.get("agent_id") or dl.get("agencyid") or dl.get("ag"))
    if agent_id:
        detail["agent_id"] = agent_id
    owner_id = _nonempty(dl.get("owner_user_id") or dl.get("seller_ids"))
    if owner_id:
        detail["owner_user_id"] = owner_id

    beds = _first_int(dl.get("property_beds"), dl.get("property_beds_list"))
    if beds is not None:
        detail["beds"] = beds
    baths = _first_int(dl.get("property_baths"), dl.get("property_baths_list"))
    if baths is not None:
        detail["baths"] = baths

    # Areas: RAW values only; units unlabelled (audit: land_area looks like
    # sqft, property_area looks like sqm — unverified). Never converted.
    land = _nonempty(dl.get("property_land_area"))
    if land:
        detail["land_area_raw"] = land
        detail["land_area_unit_note"] = "unit unlabelled on page; NOT converted"
    builtup = _nonempty(dl.get("property_area"))
    if builtup:
        detail["builtup_area_raw"] = builtup
        detail["builtup_area_unit_note"] = "unit unlabelled on page; NOT converted"

    title = _nonempty(dl.get("listing_title"))
    if title:
        detail["listing_title"] = title
    ref = _nonempty(dl.get("reference_id"))
    if ref:
        detail["reference_id"] = ref  # stable cross-run dedupe identity
    dynx = _nonempty(dl.get("dynx_itemid"))
    if dynx:
        detail["dynx_itemid"] = dynx

    lat = _to_float(dl.get("latitude"))
    lon = _to_float(dl.get("longitude"))
    if lat is not None and lon is not None:
        detail["latitude"] = lat
        detail["longitude"] = lon

    # Promo tier — useful scoring boost signal.
    for key in ("package_type", "package_hot", "package_superhot", "package_paid"):
        v = _nonempty(dl.get(key))
        if v:
            detail[key] = v

    # Location hierarchy: keep the raw loc_* keys that are present.
    loc = {k: _nonempty(v) for k, v in dl.items()
           if k.startswith("loc_") and _nonempty(v)}
    if loc:
        detail["location"] = loc

    completion = _nonempty(dl.get("completion_status"))
    if completion:
        detail["completion_status"] = completion
    purpose = _nonempty(dl.get("purpose"))
    if purpose:
        detail["purpose"] = purpose
    ptype = _nonempty(dl.get("property_type"))
    if ptype:
        detail["property_type"] = ptype

    # DOM-extracted, all optional.
    if description:
        detail["description"] = description
    if amenities:
        detail["amenities"] = amenities
    if built_year is not None:
        detail["built_year"] = built_year

    detail["enriched_at"] = _iso_now()
    return detail


# --------------------------------------------------------------------------
def _enrich_full(url: str, pid: str) -> tuple[dict | None, str | None]:
    """Fetch + parse a Zameen detail page.

    Returns ``(detail_dict, error_class)``. ``(None, error_class)`` on
    fetch blocks or unparsable pages; ``(None, None)`` should not happen —
    a page with no dataLayer at all gets ``http_no_datalayer``.
    """
    status, text, err = _fetch(url)
    if err or not text:
        return None, err or "http_unexpected"
    dl = _extract_datalayer(text)
    if not dl:
        log.warning("zameen detail %s: no dataLayer found", pid)
        return None, "http_no_datalayer"
    description = _parse_description(text)
    amenities, built_year = _parse_amenities(text)
    return _build_detail(dl, description, amenities, built_year, url, pid), None


def enrich(listing: dict) -> dict | None:
    """Enrich one Zameen listing from its public detail page.

    ``listing`` needs ``url`` (detail page) and ``platform_post_id``.
    Returns the enriched field set (see module docstring) or ``None`` when
    the page is blocked or unparseable — missing fields are omitted, never
    fabricated. No retries, no database writes.
    """
    url = (listing or {}).get("url") or ""
    pid = str((listing or {}).get("platform_post_id") or "")
    if not url or not pid:
        log.warning("detail_zameen: listing missing url or platform_post_id")
        return None
    detail, err = _enrich_full(url, pid)
    if err:
        log.warning("detail_zameen %s: %s", pid, err)
    return detail


def enrich_batch(listings: list[dict], max_items: int = BATCH_MAX,
                 delay_s: float = MIN_DELAY_S) -> dict:
    """Enrich up to ``max_items`` Zameen listings, capped at BATCH_MAX.

    Budget + politeness are module-enforced: the cap is
    ``min(max_items, BATCH_MAX)`` (20), and the per-host delay is
    ``max(delay_s, MIN_DELAY_S)`` (>= 2s) between consecutive fetches.

    A host-level block (403 / 429 / CAPTCHA markers / empty-body 200) stops
    the batch immediately — remaining listings are marked ``not_attempted``
    and no retries are made. Returns::

        {"attempted": n, "enriched": m, "stopped_on_block": bool,
         "results": [{"platform_post_id", "url", "detail"|None,
                      "error_class"|None}, ...]}

    ``detail`` is the enriched field set (or ``None`` with ``error_class``
    set when the page could not be enriched).
    """
    cap = max(0, min(int(max_items), BATCH_MAX))
    delay = max(float(delay_s), MIN_DELAY_S)
    results: list[dict] = []
    block: str | None = None
    attempted = 0
    for listing in (listings or [])[:cap]:
        if block is not None:
            break
        url = (listing or {}).get("url") or ""
        pid = str((listing or {}).get("platform_post_id") or "")
        if not url or not pid:
            results.append({"platform_post_id": pid, "url": url,
                            "detail": None, "error_class": "listing_incomplete"})
            continue
        if attempted > 0:
            time.sleep(delay)
        attempted += 1
        detail, err = _enrich_full(url, pid)
        if err in ("http_403_blocked", "http_429_blocked",
                   "http_captcha_blocked", "http_200_empty"):
            block = err
        results.append({"platform_post_id": pid, "url": url,
                        "detail": detail, "error_class": err})
    # Mark everything we never attempted (batch cap or block abort).
    for listing in (listings or [])[len(results):]:
        results.append({"platform_post_id": str((listing or {}).get("platform_post_id") or ""),
                        "url": (listing or {}).get("url") or "",
                        "detail": None, "error_class": "not_attempted"})
    enriched = sum(1 for r in results if r["detail"])
    return {"attempted": attempted, "enriched": enriched,
            "stopped_on_block": block is not None,
            "block_error": block, "results": results}
