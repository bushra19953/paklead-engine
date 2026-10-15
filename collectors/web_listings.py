"""
Public-page web collector (access route: web_public).

Reads PUBLIC listing/search pages with a plain HTTP client and parses only
the structured data those pages themselves serve (JSON-LD, embedded data
layers, server-rendered cards). Read-only, no login, no cookies, no
evasion tooling (SHR-1).

HARD BOUNDARIES (verified 2026-10-15):
- The collector fetches ONLY the page URLs configured on the source row.
  It never calls a site's internal search-backend host and never uses any
  embedded credential — even if the page source exposes one.
- OLX Pakistan (olx.com.pk) has NO price-history / previous-price field
  (that signal exists on some other OLX countries only) — no price-drop
  expectations anywhere in this collector.
- No CAPTCHA handling, no proxy rotation, no rate-limit evasion: a 403 or
  429 stops the source with a failed result.

Source config (source.boundary_note):
    url:<search-page-url>[;pages:<max pages, default 1>]
OLX paginates with ?page=N / &page=N (Islamabad houses: ~460 pages).
Zameen search URLs use the same ?page=N/&page=N pattern; Zameen server-404s
past its last page (A09: G-13 floor = page 23) — a mid-run 404 ends
pagination but keeps the records already fetched (status ok, note set).
PakWheels city pages use ?page=N.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import re
import time
import urllib.request

from .base import CollectorAdapter, RawRecord, make_result

log = logging.getLogger("paklead.web_listings")

UA = "PakLeadEngine/1.0 (+https://github.com/bushra19953/paklead-engine)"

_ld_re = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
_href_prop_re = re.compile(r'href="(/Property/[^"]+\.html)"')
_price_pkr_re = re.compile(r"PKR\s?[\d.,]+\s?(Crore|Lakh|Lac|Arab|Thousand)?", re.I)
_rel_time_re = re.compile(
    r"(?:added\s+)?(\d+)\s+(minute|hour|day|week)s?\s+ago", re.I)
_did_re = re.compile(r"ID(\d+)")
_iid_re = re.compile(r"iid-(\d+)")
_pid_re = re.compile(r"-(\d+)-\d+-\d+\.html")
# Live OLX scheme (A10): DOM card hrefs are /item/<slug>-iid-<digits>.
_iid_href_re = re.compile(
    r'href="(?:https://www\.olx\.com\.pk)?(/item/[^"]*?-iid-(\d+))"')
_ad_url_re = re.compile(r"(https?://www\.olx\.com\.pk)/ad/([^/?#]*)-ID(\d+)\.html")
# PakWheels: listing id is the trailing slug segment.
_pw_id_re = re.compile(r"-(\d{6,})/?$")
_pw_city_re = re.compile(r"/used-cars/([a-z][a-z-]*)/")

# Challenge markers (A09 2026-10-15). A challenge page is declared ONLY when
# a marker is present AND the page carries no listing content: Zameen ships
# the JS flag "STRAT_WITH_CAPTCHA:true" on ordinary result pages, so a naive
# "captcha" substring match would false-positive on every fetch. Deliberately,
# none of these markers is the bare word "captcha".
_CHALLENGE_MARKERS = (
    "cf-challenge", "challenge-platform", "just a moment",
    "verify you are human", "are you a robot", "recaptcha", "hcaptcha",
    "perimeterx", "px-captcha", "datadome", "arkose", "captcha-delivery",
    "enter the characters you see", "security check",
)
# Per-platform listing-content hooks: if any is present, the page is real
# results, not a challenge, regardless of marker strings.
_CONTENT_HOOKS = {
    "olx": ("application/ld+json", "itemlistelement"),
    "zameen": ('<li role="article"',),
    "graana": ("__next_data__",),
    "pakwheels": ("classified-listing", "application/ld+json"),
}


def _looks_challenged(text: str, platform: str) -> bool:
    """True only when a challenge marker is present AND the page carries no
    listing content for this platform. Never a bare-"captcha" substring
    match (see _CHALLENGE_MARKERS)."""
    low = (text or "").lower()
    if not any(m in low for m in _CHALLENGE_MARKERS):
        return False
    hooks = _CONTENT_HOOKS.get(platform, ("application/ld+json",))
    return not any(h in low for h in hooks)


def _fetch(url: str, timeout: int = 30) -> tuple[int | None, str | None, str | None]:
    """Plain honest-UA GET. Returns (status, text, error_class)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, None, None
    except Exception as e:
        return None, None, f"http_error:{type(e).__name__}"
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    return status, raw.decode(charset, errors="replace"), None


def _page_url(base: str, n: int) -> str:
    if n <= 1:
        return base
    return f"{base}&page={n}" if "?" in base else f"{base}?page={n}"


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _rel_to_iso(text: str) -> str | None:
    """'Added 3 hours ago' -> ISO. Only what the page itself states."""
    m = _rel_time_re.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}[unit]
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=n * mult)
    return ts.isoformat(timespec="seconds")


class WebListingsCollector(CollectorAdapter):
    platform = "web"
    access_route = "web_public"

    def collect(self, source: dict, since: str | None, budget_per_min: int,
                ctx: dict) -> CollectResult:
        platform = (source.get("platform") or "").strip()
        note = source.get("boundary_note") or ""
        base = ""
        pages = 1
        for part in note.split(";"):
            part = part.strip()
            if part.startswith("url:"):
                base = part[len("url:"):].strip()
            elif part.startswith("pages:"):
                try:
                    pages = max(1, int(part[len("pages:"):]))
                except ValueError:
                    pages = 1
        if not base:
            log.warning("web_public source '%s' has no url: in boundary_note.",
                        source.get("source_id"))
            return make_result(status="failed", error_class="web_config_missing")

        delay = max(2.0, 60.0 / (int(budget_per_min) or 30))
        records: list[RawRecord] = []
        rate_events = 0
        stop_note = ""
        for n in range(1, pages + 1):
            url = _page_url(base, n)
            status, text, err = _fetch(url)
            if status is None:
                log.warning("web fetch failed (%s): %s", err, url)
                return make_result(status="failed", error_class=err or "http_error")
            if status in (403, 429):
                # A block is an answer, not an obstacle (SHR-1): no retries.
                log.warning("web source blocked (%s): %s", status, url)
                return make_result(records, fetched=len(records),
                                   rate_limit_events=rate_events,
                                   status="failed",
                                   error_class=f"http_{status}_blocked")
            if status and status >= 500:
                # B33: consistent transient-5xx policy (collectors/retry.py):
                # one backoff retry on 500/502/503/504, then stop. 403/429
                # above already returned without retrying (SHR-1).
                from collectors import retry as _retry
                if _retry.should_retry(status, 0):
                    time.sleep(_retry.backoff_delay(0, base=delay))
                    status, text, err = _fetch(url)
                if status != 200:
                    log.warning("web source 5xx persisted: %s", url)
                    return make_result(records, fetched=len(records),
                                       status="failed", error_class="http_5xx")
            if status == 404:
                # End-of-listings signal (Zameen server-404s past its last
                # page; A09 2026-10-15): behave like the empty-page case —
                # stop pagination, keep everything fetched so far.
                log.info("web_public: %s page %d -> 404 (end of listings); "
                         "keeping %d records.", platform, n, len(records))
                stop_note = (f"pagination stopped at page {n}: HTTP 404 "
                             f"(end of listings)")
                break
            if status != 200 or not text:
                log.warning("web unexpected status %s: %s", status, url)
                return make_result(status="failed", error_class="http_unexpected")
            if _looks_challenged(text, platform):
                log.warning("web challenge page on %s: %s", platform, url)
                return make_result(records, fetched=len(records),
                                   rate_limit_events=rate_events,
                                   status="failed", error_class="web_challenge")
            got = self._parse_page(platform, text, source)
            if not got:
                # OLX embeds the full listing JSON-LD on page 1 only; later
                # pages carry no structured listings in the public HTML
                # (their cards load from the search backend, which we never
                # call) — stop instead of fetching empty pages.
                log.info("web_public: page %d of %s yielded nothing — stopping.",
                         n, platform)
                break
            records.extend(got)
            if n < pages:
                time.sleep(delay)
        result = make_result(records, fetched=len(records),
                             rate_limit_events=rate_events, status="ok")
        if stop_note:
            result["note"] = stop_note
        return result

    # ------------------------------------------------------------------
    def _parse_page(self, platform: str, text: str, source: dict) -> list[RawRecord]:
        if platform == "olx":
            return self._parse_olx(text, source)
        if platform == "zameen":
            return self._parse_zameen(text)
        if platform == "graana":
            return self._parse_graana(text, source)
        if platform == "pakwheels":
            return self._parse_pakwheels(text, source)
        log.warning("web_public: no profile for platform '%s'", platform)
        return []

    # ------------------------------------------------------------------
    @staticmethod
    def _main_itemlists(data: dict) -> list[dict]:
        """The search page's ld+json is a CollectionPage inside @graph: its
        mainEntity is the results ItemList. hasPart lists are facet filters
        (e.g. "How Many Bedrooms?") and are deliberately ignored."""
        found: list[dict] = []

        def from_graph(entries):
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("@type") == "CollectionPage":
                    me = entry.get("mainEntity")
                    if isinstance(me, dict) and me.get("@type") == "ItemList":
                        found.append(me)

        if isinstance(data, dict):
            g = data.get("@graph")
            if isinstance(g, list):
                from_graph(g)
            elif isinstance(data.get("@type"), str) and "CollectionPage" in data.get("@type", ""):
                from_graph([data])
        return found

    def _parse_olx(self, text: str, source: dict) -> list[RawRecord]:
        """OLX search page. Two item profiles share the CollectionPage shell:
        RealEstateListing (property) and Product/Car (motors, A07). Listing
        URLs use the live /item/<slug>-iid-<digits> scheme (A10): the
        search JSON-LD /ad/<slug>-ID<digits>.html URLs are stale (5/5 404)."""
        records: list[RawRecord] = []
        seen_ids: set[str] = set()
        # DOM card hrefs keyed by listing id: the live URL for each ad.
        anchor_map: dict[str, str] = {}
        for href, iid in _iid_href_re.findall(text):
            anchor_map.setdefault(iid, href)
        fallback = ""
        for part in (source.get("boundary_note") or "").split(";"):
            part = part.strip()
            if part.startswith("url:"):
                fallback = part[len("url:"):].strip()
        for m in _ld_re.finditer(text):
            payload = m.group(1).strip()
            try:
                data = json.loads(payload)
            except Exception:
                continue
            for ilist in self._main_itemlists(data):
                for el in ilist.get("itemListElement", []) or []:
                    it = (el or {}).get("item", el) if isinstance(el, dict) else None
                    if not isinstance(it, dict):
                        continue
                    types = it.get("@type")
                    tset = set(types) if isinstance(types, list) else {types}
                    rec = None
                    if "RealEstateListing" in tset:
                        rec = self._olx_record(it, anchor_map, fallback)
                    elif tset & {"Product", "Car"}:
                        rec = self._olx_motors_record(it, anchor_map, fallback)
                    if rec and rec["platform_post_id"] not in seen_ids:
                        seen_ids.add(rec["platform_post_id"])
                        records.append(rec)
        # Completeness cross-check: live DOM card ids vs parsed ids. (The old
        # /ad/ regex was circular — it only matched the JSON-LD itself.)
        if anchor_map:
            missing = set(anchor_map) - seen_ids
            if missing:
                log.warning("olx: extracted %d/%d card links (%d missing).",
                            len(seen_ids & set(anchor_map)), len(anchor_map),
                            len(missing))
        return records

    @staticmethod
    def _olx_item_url(it_url: str, anchor_map: dict, fallback: str) -> str:
        """Emit the live OLX URL scheme: prefer the DOM card href for the
        same listing id; else rewrite /ad/<slug>-ID<digits>.html to
        /item/<slug>-iid-<digits> (verified live-200 on 2 samples 2026-10-15);
        else the search-page URL — never a dead link."""
        md = _did_re.search(it_url or "")
        if md:
            href = anchor_map.get(md.group(1))
            if href:
                return "https://www.olx.com.pk" + href
        m = _ad_url_re.match(it_url or "")
        if m:
            return f"{m.group(1)}/item/{m.group(2)}-iid-{m.group(3)}"
        return fallback or it_url or ""

    @staticmethod
    def _olx_pid(it_url: str) -> str | None:
        """Listing id from either URL scheme: ID<digits> (/ad/) or
        iid-<digits> (/item/)."""
        for rx in (_did_re, _iid_re):
            m = rx.search(it_url or "")
            if m:
                return m.group(1)
        return None

    def _olx_record(self, it: dict, anchor_map: dict, fallback: str) -> RawRecord | None:
        url = it.get("url") or ""
        pid = self._olx_pid(url)
        if not url or not pid:
            return None
        name = (it.get("name") or "").strip()
        offers = it.get("offers") or {}
        price = offers.get("price")
        # Detail fields live one level down in mainEntity (House object).
        detail = it.get("mainEntity") if isinstance(it.get("mainEntity"), dict) else {}
        size = detail.get("floorSize") or {}
        addr = detail.get("address") or {}
        parts = [name] if name else []
        if price is not None:
            parts.append(f"Price Rs {int(float(price))}")
        if size.get("value"):
            parts.append(f"Size {size['value']} {size.get('unitText', '')}".strip())
        beds, baths = detail.get("numberOfBedrooms"), detail.get("numberOfBathroomsTotal")
        if beds or baths:
            bb = []
            if beds:
                bb.append(f"{beds} Beds")
            if baths:
                bb.append(f"{baths} Baths")
            parts.append(", ".join(bb))
        locality = ", ".join(x for x in
                             (addr.get("streetAddress"), addr.get("addressLocality"))
                             if x)
        if locality:
            parts.append(locality.strip())
        # No published_at and no price history on olx.com.pk — stay missing.
        return RawRecord(platform="olx", platform_post_id=pid,
                         source_id=None, author_handle="",
                         text_original=". ".join(p for p in parts if p),
                         url=self._olx_item_url(url, anchor_map, fallback),
                         published_at=None,
                         access_route_used="web_public", comments=[])

    def _olx_motors_record(self, it: dict, anchor_map: dict,
                           fallback: str) -> RawRecord | None:
        """Motors profile (A07 2026-10-15): JSON-LD Product/Car items on OLX
        vehicle category pages. Platform stays "olx"; the record is a
        vehicle (year, km, fuel) rather than a property."""
        url = it.get("url") or ""
        pid = self._olx_pid(url)
        if not url or not pid:
            return None
        name = (it.get("name") or "").strip()
        offers = it.get("offers") or {}
        price = offers.get("price")
        year = it.get("vehicleModelDate")
        odo = it.get("mileageFromOdometer")
        km = odo.get("value") if isinstance(odo, dict) else odo
        fuel = (it.get("fuelType") or "").strip()
        loc_src = offers.get("availableAtOrFrom") or {}
        loc_name = (loc_src.get("name") or "").strip() if isinstance(loc_src, dict) else ""
        addr = loc_src.get("address") if isinstance(loc_src, dict) else None
        city = (addr.get("addressLocality") or "").strip() if isinstance(addr, dict) else ""
        parts = [name] if name else []
        if price is not None:
            parts.append(f"Price Rs {int(float(price))}")
        if year:
            parts.append(f"Model year {year}")
        if km:
            parts.append(f"{km} km")
        if fuel:
            parts.append(fuel)
        locality = ", ".join(x for x in (loc_name, city) if x)
        if locality:
            parts.append(locality.strip())
        # No posted date on OLX vehicle items — stays missing.
        return RawRecord(platform="olx", platform_post_id=pid,
                         source_id=None, author_handle="",
                         text_original=". ".join(p for p in parts if p),
                         url=self._olx_item_url(url, anchor_map, fallback),
                         published_at=None,
                         access_route_used="web_public", comments=[])

    # ------------------------------------------------------------------
    def _parse_zameen(self, text: str) -> list[RawRecord]:
        """Cards are <li role="article"> blocks. Hooks are semantic
        aria-labels (stable) rather than hashed class names (not)."""
        records: list[RawRecord] = []
        seen: set[str] = set()
        for card in text.split('<li role="article"')[1:]:
            href_m = _href_prop_re.search(card)
            if not href_m:
                continue
            href = href_m.group(1)
            pm = _pid_re.search(href)
            pid = pm.group(1) if pm else None
            if not pid or pid in seen:
                continue
            title_m = re.search(r'title="([^"]{8,200})"', card[:3000])
            title = html.unescape(title_m.group(1)).strip() if title_m else ""
            cur_m = re.search(r'aria-label="Currency">([^<]{1,10})<', card)
            prc_m = re.search(r'aria-label="Price">([^<]{1,40})<', card)
            price_text = (" ".join(x for x in
                                   ((cur_m.group(1).strip() if cur_m else ""),
                                    (prc_m.group(1).strip() if prc_m else ""))
                                   if x))
            loc_m = re.search(r'aria-label="Location">([^<]{1,80})<', card)
            locality = html.unescape(loc_m.group(1)).strip() if loc_m else ""
            added_m = re.search(r'aria-label="Listing creation date"[^>]*>([^<]{1,60})<',
                                card)
            parts = [title]
            if price_text:
                parts.append(f"Price {price_text}")
            if locality:
                parts.append(locality)
            if not title and not price_text:
                continue  # don't emit empty records
            seen.add(pid)
            records.append(RawRecord(
                platform="zameen", platform_post_id=pid, source_id=None,
                author_handle="",
                text_original=". ".join(p for p in parts if p),
                url="https://www.zameen.com" + href,
                published_at=_rel_to_iso(added_m.group(1) if added_m else ""),
                access_route_used="web_public", comments=[]))
        return records

    # ------------------------------------------------------------------
    def _parse_graana(self, text: str, source: dict) -> list[RawRecord]:
        """Graana is a Next.js app: the search page's __NEXT_DATA__ JSON
        carries props.pageProps.properties — full listing records (id,
        price, size, beds/baths, area/city names, createdAt, seller name).
        Detail links are the cards' own /property/...-{id}/ hrefs, mapped
        by id from the same HTML; never reconstructed. Verified live
        2026-10-15 (Islamabad houses: 30 records/page)."""
        m = re.search(r'id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                      text, re.S)
        if not m:
            log.warning("graana: no __NEXT_DATA__ payload — profile may have changed.")
            return []
        try:
            data = json.loads(m.group(1))
            props = (data.get("props") or {}).get("pageProps") or {}
            items = props.get("properties") or []
        except (ValueError, AttributeError):
            log.warning("graana: __NEXT_DATA__ unparsable.")
            return []
        hrefs = {pid_: href_ for href_, pid_ in
                 re.findall(r'href="(/property/[^"]*?-(\d+)/)"', text)}
        base = ""
        for part in (source.get("boundary_note") or "").split(";"):
            part = part.strip()
            if part.startswith("url:"):
                base = part[len("url:"):].strip()
        records: list[RawRecord] = []
        seen: set[str] = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("id") or "")
            if not pid or pid in seen:
                continue
            title = (it.get("customTitle") or "").strip()
            price = str(it.get("price") or "").strip()
            parts = [title]
            if price:
                parts.append(f"Price Rs {price}")
            size = it.get("size")
            unit = (it.get("sizeUnit") or "").strip()
            if size:
                parts.append(f"Size {size} {unit}".strip())
            bed, bath = it.get("bed"), it.get("bath")
            bb = []
            if bed:
                bb.append(f"{bed} Beds")
            if bath:
                bb.append(f"{bath} Baths")
            if bb:
                parts.append(", ".join(bb))
            area = ((it.get("area") or {}).get("name") or "").strip()
            city = ((it.get("city") or {}).get("name") or "").strip()
            loc = ", ".join(p for p in (area, city) if p)
            if loc:
                parts.append(loc)
            if not title and not price:
                continue  # don't emit empty records
            seen.add(pid)
            href = hrefs.get(pid)
            records.append(RawRecord(
                platform="graana", platform_post_id=pid, source_id=None,
                author_handle=(it.get("name") or "").strip(),
                text_original=". ".join(p for p in parts if p),
                url=("https://www.graana.com" + href) if href else base,
                published_at=(it.get("createdAt") or None),
                access_route_used="web_public", comments=[]))
        return records

    # ------------------------------------------------------------------
    def _parse_pakwheels(self, text: str, source: dict) -> list[RawRecord]:
        """PakWheels used-car search page (A08 2026-10-15): one ItemList
        block plus one per-listing schema.org Product block (its @type is a
        list containing "Product"). Posted date, seller type and location
        are absent on search pages — stay missing; the city is implied by
        the source URL."""
        base = ""
        for part in (source.get("boundary_note") or "").split(";"):
            part = part.strip()
            if part.startswith("url:"):
                base = part[len("url:"):].strip()
        city = ""
        mc = _pw_city_re.search(base)
        if mc:
            city = mc.group(1).replace("-", " ").title()
        records: list[RawRecord] = []
        seen: set[str] = set()
        want = None
        drop_json = drop_id = drop_empty = 0
        for m in _ld_re.finditer(text):
            try:
                d = json.loads(m.group(1).strip())
            except Exception:
                drop_json += 1
                continue
            if not isinstance(d, dict):
                continue
            dtype = d.get("@type")
            if dtype == "ItemList" and isinstance(d.get("numberOfItems"), int):
                want = d["numberOfItems"]
                continue
            if not (isinstance(dtype, list) and "Product" in dtype):
                continue
            offers = d.get("offers") if isinstance(d.get("offers"), dict) else {}
            url = offers.get("url") or d.get("url") or ""
            mid = _pw_id_re.search(url)
            pid = mid.group(1) if mid else None
            if not pid or pid in seen:
                drop_id += 1
                continue
            title = (d.get("name") or "").strip()
            price = offers.get("price")
            price_text = ""
            if price is not None:
                try:
                    price_text = f"Price Rs {int(float(price))}"
                except (TypeError, ValueError):
                    price_text = ""
            year = d.get("modelDate")
            brand = d.get("brand")
            brand = brand.get("name") if isinstance(brand, dict) else brand
            fuel = d.get("fuelType")
            trans = d.get("vehicleTransmission")
            eng = d.get("vehicleEngine")
            eng = eng.get("engineDisplacement") if isinstance(eng, dict) else eng
            odo = d.get("mileageFromOdometer")
            if isinstance(odo, dict):
                odo = f"{odo.get('value')} {odo.get('unitCode') or ''}".strip()
            parts = [title] if title else []
            if price_text:
                parts.append(price_text)
            if year:
                parts.append(f"Model year {year}")
            for extra in (brand, fuel, trans, eng, odo):
                if extra:
                    parts.append(str(extra).strip())
            if city:
                parts.append(city)
            if not title and price is None:
                drop_empty += 1  # don't emit empty records
                continue
            seen.add(pid)
            records.append(RawRecord(
                platform="pakwheels", platform_post_id=pid, source_id=None,
                author_handle="",
                text_original=". ".join(p for p in parts if p),
                url=url, published_at=None,
                access_route_used="web_public", comments=[]))
        # Soft completeness check against the page's own ItemList count.
        if want is not None and want != len(records):
            log.warning("pakwheels: ItemList says %d items, parsed %d "
                        "(dropped: json=%d id=%d empty=%d).",
                        want, len(records), drop_json, drop_id, drop_empty)
        return records
