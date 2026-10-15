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
7. **Verify on real data.** First run is small (1 page / ~10 listings),
   human-checked against the visible site, then cadence is set.

## Site notes (researched 2026-10-15)
- **OLX (olx.com.pk):** shared OLX platform; frontend API pattern is publicly
  documented by open-source wrappers for other OLX countries (search /
  product / seller / categories / locations). Price-drop field = deal signal.
  Verify the .com.pk host's exact endpoints in the browser before coding.
- **Zameen.com:** URL-driven search with internal city ids (Lahore-1,
  Karachi-2, Islamabad-3 pattern); open-source scrapers use plain HTTP +
  HTML parsing or a real browser. Note: Zameen's robots.txt disallows the
  city-listing paths — keep volume human-scale and browser-based; this is
  the operator reading public pages, not a crawl fleet.
- **Graana.com:** open-source scrapers read public listing pages with plain
  HTTP + HTML parsing (fields: property id, price, area, beds/baths, city,
  date added). One open-source n8n system polls Graana every 2h and matches
  listings to client requirements — the same shape as PakLead's cycles.
- **Lamudi / PakWheels / others:** same method from step 1; no assumptions
  carried over from sister sites.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Public data only; read-only; no outreach is ever sent by a collector.
- No stealth tooling, no proxy rotation to dodge limits, no cookie/session
  replay, no CAPTCHA solving services. A site that resists is parked, and
  the operator decides.
- Every record lands in the engine import schema; never invent records.
