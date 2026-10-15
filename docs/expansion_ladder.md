# PakLead Expansion Ladder (Bushra's strategy, stated 2026-10-15)

The system this serves is the "How to Wholesale Real Estate" book-to-system
project (WholesaleSpine), adapted for Pakistan as the PakLead Engine. The
book's own farm-area discipline, applied to data coverage:

**Be specific first: start from TOWNS. Master them. Then extend town by
town. Then go up and up — city, region, country — faster at every rung.**

## Rungs

1. **Town (DONE for the starter set, 2026-10-15)** — per-town sources on
   OLX + Zameen. Starter farm = 10 Islamabad towns (G-13, DHA Defence,
   B-17, Bahria Town, Gulberg, F-7, G-11, E-11, D-12, Top City-1) + the
   Islamabad city pages + Rawalpindi city. First town sweep: 21/21
   sources ok, +433 posts in under 2 minutes (DB 728 posts / 612 leads).
   Town pages surface fresh listings the city pages' first page misses.
2. **Extend the towns (VERIFIED, row-ready — fleet 2026-10-15)** — all
   trees below were harvested from the sites' own pages, ids never guessed,
   sample towns parser-verified with locality spot-checks. A town-source
   factory now exists (`engine/town_source_factory.py`, fleet B24): it reads
   the fleet JSON trees and emits 377 validated source rows — OLX rows always
   `pages:1`, Zameen rows `pages:3` (see rung 5) with optional one-time
   `--backfill-pages` for deep first runs.
   - **Lahore:** OLX 40 towns (fleet A01: `olx_lahore_towns.json`; pages carry
     21 items = 20 organic + 1 promoted pin, so expected-count checks accept
     ≥20); Zameen 80 areas (fleet A02: `zameen_lahore_towns.json`).
   - **Karachi:** OLX 40 towns (fleet A03: `olx_karachi_towns.json`; ids mix
     old numeric and new-style forms — the factory accepts both); Zameen 79
     areas (fleet A04: `zameen_karachi_towns.json`; note: Zameen detail
     hrefs use capital `/Property/`, lowercase matches nothing).
   - The 7 new cities' OLX town trees are also harvested (fleet A05):
     Faisalabad 32, Multan 35, Sialkot 31, Gujranwala 11, Hyderabad 9,
     Peshawar 4, Quetta 2 (Peshawar/Quetta trees are genuinely small on the
     site's own filter — not an extraction miss).
3. **Up: more cities (VERIFIED, 2026-10-15)** — 7 new cities, ids discovered
   ONLY from the sites' own pages (OLX homepage /properties / sitemap; Zameen
   homepage), each city houses page parser-verified:
   - **OLX** (fleet A05, `olx_new_cities.json`): Faisalabad g4060660,
     Multan g4060675, Peshawar g4060643, Gujranwala g4060662,
     Sialkot g4060686, Hyderabad g4060693, Quetta g4060602.
   - **Zameen** (fleet A06, `zameen_new_cities.json`): Faisalabad 16,
     Multan 15, Peshawar 17, Gujranwala 327, Sialkot 480, Hyderabad 30,
     Quetta 18. **Quetta is thin-market (5 listings on page 1, verified the
     market, not a parse failure)** — its rows carry `thin_market` metadata so
     the yield monitor does not alarm on it; sweep it cheaply anyway.
   - Factory emits city rows `olx-web-{city}-houses` / `zameen-web-{city}-houses`
     plus the new-city OLX town rows (211 OLX + 166 Zameen rows total incl.
     Lahore/Karachi).
4. **Up: more categories per town (MOTORS STARTED 2026-10-15)** — plots,
   flats, commercial (Islamabad city-level already live); **vehicles now
   live**: OLX `cars_c84` via the `Product`/`Car` JSON-LD branch in the same
   collector (21 records/page, incl. model year / km / fuel — fleet A07/B25)
   and **PakWheels** as a new `web_public` platform (city URL pattern
   `pakwheels.com/used-cars/<city>/<city-id>`, Islamabad pages verified to
   page 513; search JSON-LD has no posted date — treat as inventory until
   recency is watermarked). OLX jobs/services feasibility is MEDIUM (thin
   JSON-LD, needs card-level parsing — not built yet).
5. **Faster at every rung (ADVANCED 2026-10-15 — fleet Wave B)** — the
   politeness delays are per-source, so coverage multiplies intake without
   breaking site limits. Speed/robustness additions, all verified + suite green:
   - **Concurrent fetching** (`collectors/concurrent.py`, fleet B17):
     ThreadPoolExecutor over `web_public` sources with per-host politeness
     gates — ≤2 in-flight requests per host, ≥1.5s serialized request starts
     per host, 403/429 fail the source only (no retries); verified
     live-parity vs sequential collection. It never touches the DB.
   - **Batched scoring** (`engine/scoring_batch.py`, fleet B18): one config
     resolution per batch instead of per lead; faithful port of the
     six-input scorer (PRD worked examples still exact); `delta_scoring` for
     incremental re-scores.
   - **Price history + price-drop detection** (`engine/price_history.py`,
     fleet B20): OLX.com.pk exposes NO price history (fleet A10 — verified
     absent), so PakLead builds its own: per-listing price snapshots
     (`platform:platform_post_id` key, 90-day prune), firing only when the
     latest price is ≥10% below the 30-day max AND is the window minimum
     (a drop that recovered never re-fires).
   - **Per-source yield monitor** (`engine/yield_monitor.py`, fleet B19):
     one-row-per-run history per source; `zero_yield_alarm` when a previously
     productive source returns 0 records on consecutive healthy ("ok") runs;
     `thin_market_candidate` (human review, no alarm) for sources that never
     yielded — e.g. Zameen Quetta. Failed/partial runs never count toward
     zero-yield alarms.
   - **Deep-pagination safety** (fleet B25): Zameen town pages server-render
     25 cards down to page ~23 with a hard server 404 past the last page —
     a 404 mid-run now breaks pagination and keeps fetched records instead of
     failing the run and dropping them. CAPTCHA detection requires a
     challenge marker AND no listing content (Zameen's `STRAT_WITH_CAPTCHA`
     JS flag is a false positive).

Rule at every rung: NEVER guess a URL or id — harvest it from the site's
own pages, verify the parser extracts real local records from it, only
then add it as a live source.
