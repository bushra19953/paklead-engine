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

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task or a verified public endpoint — read-only, always.
- Never contact an agent, never submit a form, never change account state.
- Stop immediately and hand off on any checkpoint, verification prompt, or rate-limit notice.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
