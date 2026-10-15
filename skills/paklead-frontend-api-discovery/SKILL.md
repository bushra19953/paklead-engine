---
name: paklead-frontend-api-discovery
description: "PakLead method for onboarding a new website fast: discover the JSON endpoints the site's own frontend uses for public listings/search, map them to the engine import schema, and wrap them as a polite read-only collector. Learned from open-source OLX/Zameen/Graana projects (2026-10-15)."
license: Project-native (PakLead Engine)
---

# Frontend-API Discovery — how we connect a new site

Most listing/social sites have no developer API, but their own web frontend
loads public data from internal JSON endpoints. Open-source scraper projects
(e.g. the OLX API wrappers, the Zameen/Graana scrapers) all use the same
method. This skill is that method, adapted to PakLead's boundaries.

## The method (per site)
1. **Recon in the browser.** Open the site's search/listing page. Note the URL
   pattern (many Pakistani portals encode ids in the URL — e.g. Zameen search
   pages look like `/Homes/{City}-{cityId}-{page}.html`; city ids are internal
   and must be read off a real search page, not guessed).
2. **Find the data call.** Watch what the page itself requests (browser
   network view): listing pages are either server-rendered HTML with the data
   inline, or the frontend calls a JSON search endpoint with filter/pagination
   parameters. OLX country sites share one platform: their frontends expose
   search (filters + paging), listing detail, seller listings, categories, and
   location resolution — and the listing payload can carry a previous-price
   field, i.e. a built-in price-drop signal.
3. **Classify the endpoint.** Public (works logged-out, no token) → usable.
   Needs login/session/cookies → STOP: that site stays on the browser-harvest
   route in the operator's own session. Never replay cookies from code, never
   bypass a challenge or a paywall.
4. **Map fields to the import schema.** platform, platform_post_id (the site's
   own listing id), author_handle (seller/agency), text_original (title +
   description + price + area, verbatim signals included), url, published_at.
5. **Wrap as a collector.** One adapter per site behind the standard
   CollectResult interface; per-source file/watermark via the source row.
6. **Be polite or don't run.** Stay inside the engine's rate budget
   (source.rate_budget_per_min), back off exponentially on errors, and halt
   the source on 403/429/CAPTCHA — a block is an answer, not an obstacle.
   A "CAPTCHA" stop is declared ONLY when a real challenge marker is present
   AND the page carries no listing content — never the bare word "captcha"
   (verified 2026-10-15: Zameen ships the JS flag `STRAT_WITH_CAPTCHA:true` on
   ordinary result pages). Markers: `cf-challenge`, `challenge-platform`,
   `just a moment`, `verify you are human`, `are you a robot`, `recaptcha`,
   `hcaptcha`, `perimeterx`, `px-captcha`, `datadome`, `arkose`,
   `captcha-delivery`, `enter the characters you see`, `security check`.
7. **Verify on real data.** First run is small (1 page / ~10 listings),
   human-checked against the visible site, then cadence is set.

## Site notes (verified live 2026-10-15, fleet A08/A09/A10/A12/B25)
- **OLX (olx.com.pk):** search pages embed schema.org JSON-LD (CollectionPage →
  mainEntity ItemList → RealEstateListing with name, ad URL + numeric ID,
  offers.price in PKR, bedrooms/baths, floorSize, address). CRITICAL: the JSON-LD
  ad URLs (`/ad/<slug>-ID<digits>.html`) are STALE and 404; the live scheme is
  `/item/<slug>-iid-<digits>` (DOM card anchor hrefs — same numeric id, so
  `/ad/` → `/item/` is rewritable). Pages 2+ carry no embedded listings (cards
  load from the search backend — which we NEVER call), so the public-page route
  covers page 1 only (~20 JSON-LD records; DOM holds ~24 card links). NO
  price-history field exists on olx.com.pk: drop all price-drop expectations.
  The page source also exposes the internal search-backend host and an embedded
  credential — NEVER use either. Note: `window.state` carries full records for
  newest ads missing from JSON-LD but ALSO the embedded credential — it is
  off-limits; fill from DOM card fields instead.
- **Zameen.com:** URL-driven search with internal city ids (Lahore-1,
  Karachi-2, Islamabad-3 pattern); open-source scrapers use plain HTTP +
  HTML parsing or a real browser. DEEP PAGINATION WORKS: town pages
  (`-1.html` → `-{n}.html`, or `&page=N` appended to a `?sort=date_desc` URL —
  the sort param is load-bearing) serve full 25-card pages with zero duplicates
  through page 6+ (verified floor: 554 G-13 listings to page 23); HTTP 404 is
  the natural end-of-listings signal — stop and keep records, not a failure.
  Note: Zameen's robots.txt disallows the city-listing paths — keep volume
  human-scale and browser-based; this is the operator reading public pages,
  not a crawl fleet.
- **Graana.com:** REAL search pattern `/sale/{type}-sale-{city}-{id}/` with a
  `__NEXT_DATA__` payload (`props.pageProps.properties`, 30/page:
  `id, customTitle, price, size, sizeUnit, bed, bath, area{name}, city{name},
  createdAt, name` seller). NO town-level search URLs exist — Graana links no
  town pages from its own pages, and invented town slugs return HTTP 200 but
  silently serve the identical city result set (double-ingest risk). Per-town
  route: fetch city pages and filter/split by `area.name` in the payload.
  One open-source n8n system polls Graana every 2h and matches listings to
  client requirements — the same shape as PakLead's cycles.
- **PakWheels (pakwheels.com):** VERIFIED 2026-10-15 (fleet A08, collector
  profile live — B25). City search URLs from the homepage's own link tree:
  `/used-cars/{city}/{cityId}` (Islamabad `24856`, Lahore `24858`, Karachi
  `24857`, Rawalpindi `24831`, Peshawar `24821`, Faisalabad `24753`, Multan
  `24810`); `?page=N` pagination, 25/page server-rendered. Same pattern as
  OLX: `application/ld+json` blocks — one ItemList + one `Product` block per
  listing. Extractable: listing id (URL slug tail), title, canonical URL,
  price (numeric PKR), model year, brand, fuel, transmission, engine
  displacement, mileage string, cover image; city from the source URL.
  MISSING on search pages (never invent): posted date, seller type,
  exact location/town. Recency needs listing-ID watermarking.
- **Lamudi / others:** same method from step 1; no assumptions carried over
  from sister sites.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Public data only; read-only; no outreach is ever sent by a collector.
- No stealth tooling, no proxy rotation to dodge limits, no cookie/session
  replay, no CAPTCHA solving services. A site that resists is parked, and
  the operator decides.
- Every record lands in the engine import schema; never invent records.
