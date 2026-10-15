# PakLead — Platform Connection Playbook

How the app connects to each platform, and why. Researched 2026-10-15.
Rule for every platform: use the most official route that exists; where no
sanctioned route exists, use the operator's own browser session, read-only,
through the manual-import pipeline. No stealth scraping, no cookie replay,
no third-party scraper APIs (SHR boundaries).

## Facebook — LIVE
- Route: operator's own logged-in browser session (read-only) → manual import.
- Why: the official Graph API does not expose other groups' member posts for
  this use; the facebook-cli connector is not connected in this environment.
- Needs from operator: stay logged in in the shared browser.
- Skills: facebook-group-scraper, facebook-post-scraper, facebook-comment-scraper,
  facebook-search-results-scraper, social-purchase-intent-lead-finder, social-keyword-monitor.
- Cadence: cyclic cron every 2h (08:00–22:00 PKT) + 21:00 digest.

## Reddit — APPROVAL PENDING
- Route: official API only (PRAW, read-only client-credentials).
- Status: since Nov 2025 Reddit requires pre-approval for ALL API access;
  our access request was submitted 2026-10-15 (account u/Inevitable-Watch654,
  subreddits r/Pakistan, r/karachi, r/Lahore, r/islamabad). Reported wait 2–4 weeks.
- On approval: create the script app, operator pastes the secret into .env herself.
- Skills: reddit family in the upstream repo is ADAPT-class but PARKED — never
  used as a bypass around the approval gate.

## X (Twitter) — BROWSER ROUTE (official API priced)
- Official route exists: X API v2. As of Feb 2026 it is pay-per-use for new
  developers (about $0.005 per post read; recent search covers the last 7 days;
  legacy Basic $200/mo and Pro $5,000/mo are closed to new signups; no free
  read tier). Full-archive search needs an access upgrade / Enterprise.
- PakLead's chosen route for now: operator's logged-in browser session →
  manual import (zero cost, read-only). The paid API is a documented upgrade
  option if read volume ever justifies ~$5 per 1,000 posts read.
- Needs from operator: log in to X in the shared browser when the X cycle starts.
- Skills: twitter-x-search-results-scraper, twitter-x-post-scraper.
- NOT used: internal/GraphQL cookie-based access — violates the project's
  no-cookie-automation boundary.

## TikTok — BROWSER ROUTE (no public-content API)
- Official surfaces: Display API (the operator's OWN account videos via OAuth),
  Content Posting API (publishing), Research API (public content — but limited
  to academic institutions in the US/EEA/UK/Switzerland and non-commercial use;
  a Pakistan-based operator is not eligible).
- PakLead's chosen route: browser search/discovery in session or public pages →
  manual import. TikTok's public search is largely usable logged-out.
- Skills: tiktok-search-results-scraper, tiktok-video-scraper, tiktok-comment-scraper.

## OLX Pakistan — BROWSER ROUTE (no developer API)
- No official developer/public API exists. Public listings are readable on
  olx.com.pk without login.
- PakLead's chosen route: browser harvest of public search/category pages →
  manual import (records carry platform "olx").
- Skill: paklead-olx-harvest (project-native).
- **CORRECTED 2026-10-15 (fleet audits A10/B21/B25): the live URL scheme is
  `/item/<slug>-iid-<digits>`.** The search-page JSON-LD `url` values use a
  dead `/ad/<slug>-ID<digits>.html` scheme (they 404, or OLX junk-redirects
  them to a generic search page). The collector now prefers the DOM card
  anchors' `/item/` hrefs and rewrites any `/ad/` URL to `/item/` as a
  fallback — stored pre-fix OLX URLs in old DB rows may be dead links.
- Page shape (verified): one `<script id="search-schema" type="application/ld+json">`
  block → `@graph[0].mainEntity` → results `ItemList` of `RealEstateListing`
  objects (numeric price, size, beds/baths, address, listing id). Page 1
  carries ~20 organic records + 1 promoted pin (21 items) that repeats across
  town pages — factory expected-count checks accept ≥20. Deep pages (page 2+)
  carry NO embedded listings, so OLX sources run `pages:1` only.
- **Motors branch (VERIFIED LIVE 2026-10-15, fleet A07 + B25):** the same
  JSON-LD shell serves `cars_c84` with `@type` `["Product","Car"]` —
  21 records/page, richer than property: numeric price, `vehicleModelDate`
  (model year), `mileageFromOdometer` (km), `fuelType`, locality via
  `availableAtOrFrom`. The collector handles the `Product`/`Car` branch in
  the same parse loop (platform stays "olx"; aligns with the tracked
  **vehicles** lead type). Other sub-categories exist on-site
  (`motorcycles_c81`, `bikes_c1898`, `cars-accessories_c2027`, `spare-parts_c82`,
  `cars-on-installments_c86`) — NOT yet collected.
- Jobs (`jobs_c4`) and services (`services_c619`) feasibility: MEDIUM — their
  JSON-LD items are thin (`position`/`name`/`url`/`image` only, no price or
  location); a usable record needs card-level parsing (`aria-label="Location"`
  + relative time strings), NOT yet built.
- Timestamps: no `datePosted` on OLX search JSON-LD; card HTML carries
  relative time strings ("1 hour ago") recoverable by card-text parsing.
- **Hard boundary (unchanged, re-verified):** OLX pages expose their internal
  search backend (`search.olxpk.net` / `search-strat-production-….amazonaws.com`)
  and an embedded credential for it, incl. inside the `window.state` blob on
  detail pages. PakLead NEVER fetches those hosts and NEVER reads, records,
  or uses that credential. ~4 of the newest ads on a page-1 search have no
  JSON-LD entry (their records live in `window.state`, off-limits) — a future
  fill must come from DOM card hooks only (anchor `title` +
  `aria-label="Location"/"Price"`), never from `window.state`.

## Zameen.com — BROWSER ROUTE (no developer API)
- No official developer/public API exists. Public listings are readable on
  zameen.com without login.
- PakLead's chosen route: browser harvest of public search pages →
  manual import (records carry platform "zameen"). Property sellers are the
  wholesale book's core lead type.
- Skill: paklead-zameen-harvest (project-native).
- **Pagination (VERIFIED LIVE 2026-10-15, fleet A09 + B25): Zameen server-renders
  full 25-card pages deep and clean — no drop-off through page 6, zero
  duplicates; G-13's true floor is page 23 (554 listings), and Zameen's server
  404s past the last page.** The collector's `&page=N` mechanism produces pages
  ID-identical to Zameen's path-style `-N.html` (no code change needed), BUT the
  `?sort=date_desc` param in the source URL is load-bearing — without it the
  page differs. Policy: recurring sweeps use `pages:3` (freshness + rollover
  buffer); one-time deep backfill per town source walks to the 404 boundary;
  a 404 mid-run now breaks pagination and KEEPS fetched records (B25 fix —
  previously a 404 failed the run and discarded them).
- Robots.txt due diligence (fleet A06): the `/Houses_Property/{City}-{id}-1.html`
  search paths the collector uses are NOT disallowed by Zameen's robots.txt
  (only city-named root paths and `/property_listings/` are).
- CAPTCHA-detection rule: the bare word "captcha" is NOT a block signal —
  Zameen's pages carry `STRAT_WITH_CAPTCHA:true` as a JS feature flag on
  ordinary result pages. The collector declares a challenge only when a
  challenge marker is present AND the page carries no listing content.

## Reverse-engineering notes (open-source research, 2026-10-15)
Method skill: paklead-frontend-api-discovery — discover the JSON endpoints a
site's own frontend uses for public data, map to the import schema, wrap as a
polite collector. Learned from open-source projects:
- OLX country sites share one platform. Verified live on olx.com.pk
  (2026-10-15, browser recon + direct page probe): search pages embed
  schema.org JSON-LD (an ItemList of RealEstateListing objects with exact
  numeric prices, size, beds/baths, address, listing URL/id) and a dataLayer
  push with the page's ad_ids and page count; pagination links are ?page=N
  but pages 2+ carry NO embedded listings in the
  public HTML — the public-page route covers page 1 only (~20 organic
  records + 1 promoted pin = 21 items). **Update 2026-10-15 (fleet A10/B25):
  the JSON-LD `url` values use a dead `/ad/<slug>-ID<digits>.html` scheme
  (404 / junk-redirect); the live scheme is the DOM card anchors'
  `/item/<slug>-iid-<digits>`** — the collector prefers card hrefs and
  rewrites `/ad/` → `/item/` as fallback.
  Detail pages add seller_type (individual vs
  agent), a seller id, location ids, and photo count. NO price-history /
  previous-price field exists on .com.pk (that signal exists on some other
  OLX countries only) — do not design for it here. The pages also expose the
  site's internal search backend and an embedded credential for it in the
  page source: PakLead does NOT use that backend or credential — our route
  is only the structured data served inside the public pages.
- Zameen search URLs encode internal city ids (`/Homes/{City}-{id}-{page}.html`;
  Lahore-1, Karachi-2, Islamabad-3 pattern). Robots.txt due diligence
  (fleet A06, 2026-10-15): the `/Houses_Property/{City}-{id}-1.html` search
  paths the collector uses are NOT disallowed (only city-named root paths
  and `/property_listings/` are) — Zameen stays browser-based at human scale.
- Graana public listing pages are readable with plain HTTP + HTML parsing
  (open-source scrapers + one n8n system polling Graana every 2h for client
  matching). Graana added as a first-class property source.
  VERIFIED LIVE 2026-10-15: Graana is a Next.js app; its search pages
  (pattern /sale/{type}-sale-{city}-{id}/, pagination ?pageSize=30&page=N)
  embed a __NEXT_DATA__ JSON whose props.pageProps.properties carries 30
  full listing records per page (id, numeric price, size/unit, beds/baths,
  area + city names, createdAt timestamp, seller name, agency id). The
  collector's graana profile parses exactly that payload; detail links are
  the cards' own /property/...-{id}/ hrefs. The earlier guessed /search
  URL returns an empty JS shell — do not use it.
  (skill: paklead-graana-harvest).

## PakWheels — BROWSER ROUTE (no developer API) — VERIFIED LIVE 2026-10-15
- No official developer/public API exists. Public used-car listings are
  readable without login.
- City search URL pattern (from the site's own homepage links, never guessed):
  `https://www.pakwheels.com/used-cars/<city>/<city-id>` — verified cities:
  islamabad/24856, lahore/24858, karachi/24857, rawalpindi/24831,
  peshawar/24821, faisalabad/24753, multan/24810. Pagination: `?page=N`
  (Islamabad verified to page 513 — ~12.8k listings).
- Structure: server-rendered cards (`div.classified-listing`, 25 per page on
  city pages), schema.org JSON-LD: one `ItemList` block + one `Product` block
  per listing. Fields per listing: numeric id (URL slug tail), title,
  detail URL, numeric price (PKR), model year, brand, fuel type,
  transmission, engine cc, mileage (string, needs normalization), image.
  The collector's `pakwheels` profile parses exactly this (registered in
  `web_listings.py` + pipeline, verified live: Islamabad page 1 → 25 records).
- **Recency caveat:** search pages carry NO posted timestamp, NO seller type,
  and only city-level location (town comes from nowhere on the search page).
  Recency detection needs listing-ID watermarking or a detail-page follow-up —
  until then treat PakWheels records as inventory, not freshness-scored.
- Route: `web_public` collector (skill: paklead-pakwheels-harvest
  project-native; collector profile `_parse_pakwheels`).

## Detail enrichment (built 2026-10-15, Wave-B B21/B22/B23)

Enrichers fetch detail pages ONLY for new or top-scored HOT leads (≤ ~20 per
cycle), ≥2s same-host delays, honest UA, plain GETs, read-only; 403/429 or
challenge markers stop the batch with no retries. All fields come from the
page's own public payloads — enrichment is triage fuel, never outreach.

### OLX (`collectors/detail_olx.py`)
- Source: `window['dataLayer']` offerdetail payload — `ad_id`, `seller_id`,
  `seller_type` (individual/agent/company), `is_agent`, `seller_verified`,
  `number_of_photos`, `package_type` (featured vs organic — the only
  featured signal), price, rooms/baths/size, `furnished`/`has_video`/
  `has_panorama` flags, `ownership_type`, full location id chain
  (`loc_1..4_id`/`loc_breadcrumb`).
- Source: `window.state.adManagement.sellerProfile.data` — seller name,
  `createdAt` (member-since), `activeAdCount` (seller's live ad count —
  dealer signal), `commercialNumber` when populated.
- Source: DOM — full description text (ends before "Related ads").
- **Phone policy:** `phoneNumber` is captured ONLY when
  `isPhoneNumberVisible` is explicitly `true` (plain-text value the seller
  opted to publish); the "Show phone" button is never clicked and no
  `tel:` links exist on the pages.
- Do NOT capture: ad creation timestamps (absent everywhere — never infer
  from `ad_id` numerics), price history (absent on olx.com.pk), the
  search-backend hosts or their embedded credential, the detail JSON-LD
  (fully redundant with dataLayer).
- Fetch against the LIVE `/item/<slug>-iid-<digits>` URL (see OLX section);
  the enricher rewrites dead `/ad/` URLs automatically.

### Zameen (`collectors/detail_zameen.py`)
- Source: `window['dataLayer']` push (~100 keys) — `property_price` (raw PKR),
  `marketed_by` (agency name), `seller_type` (raw string: observed
  `"business"`; owner-side value unverified — captured raw, never normalized
  to a boolean), `agent_id`/`agencyid`/`owner_user_id`/`seller_ids`,
  beds/baths, `property_land_area`/`property_area` (RAW — units unlabelled,
  never converted), `reference_id`/`dynx_itemid` (stable dedupe identity),
  `latitude`/`longitude`, `package_type`/`package_hot`/`package_superhot`
  (promo-tier signal), location hierarchy (`loc_neighbourhood_name` for the
  town label), `completion_status`, `purpose`, `property_type`.
- Source: DOM (optional, ~60% hit rate) — description via
  `div[aria-label="Property description"]`, Amenities as
  (category, name, value) triples, `built_year` regex where present. Pages
  come in two template variants (one degraded: dataLayer only, no
  description/amenities) — every DOM field is treated as optional.
- Do NOT capture: phone numbers (Zameen masks them behind an interactive
  reveal — attempting to mine them programmatically is ToS-abusive; Zameen
  lead contactability stays "unknown", never fabricated), listing age
  (verified absent — store `enriched_at`, the fetch time, honestly), exact
  area conversions, amenity counts as authoritative over dataLayer
  (observed disagreeing — keep both, show "listed as").

### Graana (`collectors/detail_graana.py`)
- Source: static `__NEXT_DATA__` → `props.pageProps.data` — description,
  agency contact (`name`/`phone`/`email`/`website`/`address`),
  agent contact (`name`/`family_name`/`phone_number` +
  `phone_number_verified` flag), listing `phone`, `built_year`, `condition`,
  `propertyAvailability`, `expiresAt`, `daysOnGraana`, lat/lng + address +
  `area`/`city` names, feature groups copied verbatim, `image_count`
  (image URLs never stored).
- Do NOT capture: internal ids (`agencyId`/`agentId`/`userId`/
  `systemUserId`/`riderId`/`propsureId`), Cognito internals
  (`sub`/`Username`/`custom:*`, cloudinary ids), anything behind an
  interaction, anything not in the static payload.
- **Graana town slugs do NOT filter (VERIFIED 2026-10-15, fleet A12):**
  invented `/sale/{type}-sale-{town}-{city}-{id}/` URLs return HTTP 200 but
  serve the identical CITY result set (town slug silently ignored) — building
  per-town Graana URL sources would double-ingest. Per-town coverage =
  fetch city search pages and filter imported records by `area.name`
  (present in every search-payload record). Graana's own pages link no
  town-level search URLs at all.
- Correction note: in the static payload `description` can be `null` where
  the rendered page shows one — enrichers copy the payload faithfully and
  return it empty rather than invent content.

## Buyers / Disposition (built 2026-10-15, audited A17 + builder B50)

The wholesale book's Disposition pillar: a buyer registry + seller→buyer
matcher, registry-and-matching ONLY — there are no outreach code paths
anywhere in `engine/buyer_registry.py` (statically verified).
- **Buyer lane = `intent_primary IN ('property_wanted','seeking_product')`.**
  `intent_secondary='property_wanted'` is 99% seller noise (459 sampled
  posts — all seller ads stating "Demand" as the asking price) and is NEVER
  used for buyer derivation.
- **Identity rules (SHR-9):** merge buyer identities ONLY on identical
  normalized phone numbers (`normalize_phone`: `0333 555 1234` →
  `923335551234`); name-only or handle-only matches = separate candidate
  identities, NEVER merged. Buyers with no identity (the norm today) are
  recorded as town-level demand signals (`merge_key=demand:<city>|<town>`),
  never as named people.
- **Matching** (`match_buyers`): town match 40 · same-city-different-town 10 ·
  budget compatibility 25 · recency 10/5/0 · activity up to 5, with
  human-readable `match_reasons`. Buyer-side comparator merges require the
  SAME `area_tag` + budget within 10% (template "Buying a House in X" ads
  otherwise over-merge across towns).
- **Current DB reality (2026-10-15):** 10 genuine buyer posts (0.4% of posts),
  all anonymous — contactability 0, so the 5 HOT buyer leads are not
  reachable. Buyer supply today = Zameen "required" ads + OLX "Buying a…"
  ads; Facebook currently a dead end for property buyers. **Contact capture
  is the #1 disposition blocker;** Zameen masks numbers behind an interactive
  reveal — never bypass it.
- Disposition matching is indicative, not contractual.

## Still in the original mission scope (not yet connected)
- Instagram (connector exists in this environment; hashtag/location reading
  is a later wave).
- LinkedIn Pakistan (connector not present; public pages only, later wave).
- X/TikTok cycle 1 (fleet audit A16, 2026-10-15): code path verified ready
  (import adapter, watermarks, classifier, comparator all handle X/TikTok
  records with zero changes); blockers are operational — Bushra's X login,
  registering 6 search-query source rows, locking the query list, and using
  the nested-comments convention for comment-scraper output.

## The connection pattern (all platforms)
browser/API collection → JSON in the engine import schema → ImportAdapter
(per-source file via the source row's boundary_note, watermark deltas) →
classifier → scorer → comparator → dashboard/API → digest.
Every new platform is: (1) a source row, (2) a harvest skill, (3) records in
the shared schema. Nothing platform-specific lives outside its skill + source.
