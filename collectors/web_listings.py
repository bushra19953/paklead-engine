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
Zameen search URLs use the same ?page=N/&page=N pattern.
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
_pid_re = re.compile(r"-(\d+)-\d+-\d+\.html")


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
                # One backoff retry, then stop.
                time.sleep(delay * 2)
                status, text, err = _fetch(url)
                if status != 200:
                    log.warning("web source 5xx persisted: %s", url)
                    return make_result(records, fetched=len(records),
                                       status="failed", error_class="http_5xx")
            if status != 200 or not text:
                log.warning("web unexpected status %s: %s", status, url)
                return make_result(status="failed", error_class="http_unexpected")
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
        return make_result(records, fetched=len(records),
                           rate_limit_events=rate_events, status="ok")

    # ------------------------------------------------------------------
    def _parse_page(self, platform: str, text: str, source: dict) -> list[RawRecord]:
        if platform == "olx":
            return self._parse_olx(text)
        if platform == "zameen":
            return self._parse_zameen(text)
        if platform == "graana":
            return self._parse_graana(text, source)
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

    def _parse_olx(self, text: str) -> list[RawRecord]:
        records: list[RawRecord] = []
        seen_ids: set[str] = set()
        for m in _ld_re.finditer(text):
            payload = m.group(1).strip()
            try:
                data = json.loads(payload)
            except Exception:
                continue
            for ilist in self._main_itemlists(data):
                for el in ilist.get("itemListElement", []) or []:
                    it = (el or {}).get("item", el) if isinstance(el, dict) else None
                    if not isinstance(it, dict) or it.get("@type") != "RealEstateListing":
                        continue
                    rec = self._olx_record(it)
                    if rec and rec["platform_post_id"] not in seen_ids:
                        seen_ids.add(rec["platform_post_id"])
                        records.append(rec)
        # Completeness cross-check: listing-card links in the same HTML.
        # (The dataLayer ad_ids list covers a different widget and is NOT a
        # valid cross-check target.)
        anchors = set(re.findall(r'/ad/[^"\']*?ID(\d+)\.html', text))
        if anchors:
            missing = anchors - seen_ids
            if missing:
                log.warning("olx: extracted %d/%d card links (%d missing).",
                            len(seen_ids & anchors), len(anchors), len(missing))
        return records

    def _olx_record(self, it: dict) -> RawRecord | None:
        url = it.get("url") or ""
        pid = None
        md = _did_re.search(url)
        if md:
            pid = md.group(1)
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
                         url=url, published_at=None,
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
