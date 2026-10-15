---
name: paklead-pakwheels-harvest
description: "PakLead harvest playbook for PakWheels (pakwheels.com) public used-car listings across Pakistan. Use when collecting PakWheels listings as PakLead leads via the operator's browser or the frontend-API discovery method."
license: Project-native (PakLead Engine)
---

# PakLead PakWheels Harvest

Collect PUBLIC used-car listings from PakWheels through the operator's browser (or the frontend-API discovery method) and emit them in the engine import schema. PakWheels is Pakistan's primary used-car portal — its seller listings are a strong lead source for the dealer/finance vertical; seller names are not published on search pages.

## What to collect per listing
- Title + structured fields (this becomes `text_original`; always include price, model year, brand, fuel, transmission, engine size, mileage, and city).
- `platform`: "pakwheels"
- `platform_post_id`: the numeric listing id from the URL slug tail (e.g. `12058426`).
- `author_handle`: seller display name — NOT published on search pages (record as unknown; never invent).
- `url`: the canonical detail URL (same URL appears in the page's ItemList and the listing's Product block).
- `published_at`: NOT present on search pages (record as unknown; never invent a date).
- Keep the mileage string verbatim ("61,000 km" etc. — the engine normalizes it).

## URL patterns (verified live 2026-10-15 — read from the homepage's own link tree, never guess)
- City search: `https://www.pakwheels.com/used-cars/{city}/{cityId}` — verified ids: Islamabad `24856`, Lahore `24858`, Karachi `24857`, Rawalpindi `24831`, Peshawar `24821`, Faisalabad `24753`, Multan `24810`.
- Pagination: append `?page=N` (e.g. `/used-cars/islamabad/24856?page=2`); 25 listings per page, server-rendered (no JS needed, no login/cookies required).
- Listing pages are schema.org `application/ld+json`: one `ItemList` block + one `Product` block per listing (the `Product` block's `url` == the ItemList element's `url` == canonical detail URL).

## Fields present vs missing on search pages (verified 2026-10-15)
- PRESENT (0 missing across 25 audited page-1 records): listing id (URL slug tail), title, url, price (numeric PKR + `priceCurrency`), model year (`modelDate`), brand, fuel (`fuelType`), transmission (`vehicleTransmission`), engine displacement (`vehicleEngine.engineDisplacement`), mileage (`mileageFromOdometer`), cover image. City comes from the source URL's city segment, not the JSON.
- MISSING (never invent): posted/timestamp (no "x days ago" text or date in cards), seller type (no dealer-vs-owner flag in structured data), exact location/town (city only, from URL/title).

## Recency caveat
Because search pages carry no posted date, recency detection MUST use listing-ID watermarking (persist the highest seen listing id per source and treat higher ids as newer) or a detail-page follow-up — never infer dates from ids.

## Priority targets
- Used cars for sale in Islamabad, Lahore, Karachi — newest first.
- Dealer-priced or urgency-language listings ("urgent sale", "final price", "negotiable").

## Method
Browser harvest by default. If the frontend-API discovery skill
(paklead-frontend-api-discovery) verifies a public JSON endpoint for PakWheels
search results, a collector may use it under the same runtime rules.
Politeness: honest UA, ≥2 s same-host delay, stop on 403/429.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task or a verified public endpoint — read-only, always (PakWheels public pages need no login — never log in unless she instructed it).
- Never contact a seller, never submit a form, never change account state.
- Stop immediately and hand off on any checkpoint, verification prompt, or rate-limit notice.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
