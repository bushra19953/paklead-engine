---
name: paklead-graana-harvest
description: "PakLead harvest playbook for Graana.com public property listings across Pakistan. Use when collecting Graana listings as PakLead leads via the operator's browser or the frontend-API discovery method."
license: Project-native (PakLead Engine)
---

# PakLead Graana Harvest

Collect PUBLIC listings from Graana.com and emit them in the engine import schema. Graana is a primary Pakistani property portal (Islamabad-heavy) — its sellers are the wholesale book's core lead type, same as Zameen.

## What to collect per listing
- Title + description text (becomes `text_original`; always include asking price, area/size, locality + city).
- `platform`: "graana"
- `platform_post_id`: the property id from the URL when visible, else the URL slug.
- `author_handle`: agency/agent name when shown (prefix "Agency:").
- `url`: the listing URL.
- `published_at`: listing date converted to ISO-8601 when shown.
- Urgency/value signals verbatim: "urgent", "investor rate", "below market", "final", "negotiable".

## Priority targets
- Houses/plots for sale in Islamabad, Lahore, Rawalpindi, Karachi — newest first.
- Any listing with investor-rate or urgent language.

## Method
Browser harvest by default. If the frontend-API discovery skill
(paklead-frontend-api-discovery) verifies a public JSON endpoint for Graana
search results, a collector may use it under the same runtime rules.

Search URL pattern (verified live 2026-10-15): `/sale/{type}-sale-{city}-{id}/`
(e.g. `/sale/house-sale-islamabad-1/`). Search pages carry a `__NEXT_DATA__`
payload (`props.pageProps.properties`, 30 records/page) with
`id, customTitle, price, size, sizeUnit, bed, bath, area{name}, city{name},
createdAt, name` (seller name); detail pages are Next.js `__NEXT_DATA__`
→ `props.pageProps.data` (single listing, no JSON-LD).

NO town-level search URLs: Graana links no town search pages from its own
pages, and invented town slugs (e.g. `…/sale/house-sale-bahria-town-phase-4-
islamabad-1/`) return HTTP 200 but IGNORE the slug — they silently serve the
identical city result set and would double-ingest. Do NOT mint per-town
Graana sources by URL. The per-town route is: fetch city search pages and
split/filter records by `area.name` (present in every search-payload record,
e.g. "Bahria Town Phase 4", "DHA Phase 2", "Soan Garden").

HOT or near-HOT leads may be detail-enriched (page GET only): extract
`description`, `agency{ name, phone, email }`, `agentDetails{ name,
phone_number }`, listing `phone`, `lat/lng` + `address`, `propertyImages`,
`builtYear`, `condition`, `daysOnGraana`, `propertyAvailability`. Same
politeness budget as search fetches.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task or a verified public endpoint — read-only, always.
- Never contact an agent, never submit a form, never change account state.
- Stop immediately and hand off on any checkpoint, verification prompt, or rate-limit notice.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
