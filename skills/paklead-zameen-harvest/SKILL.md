---
name: paklead-zameen-harvest
description: "PakLead harvest playbook for Zameen.com public property listings — houses and plots for sale across Pakistan. Use when collecting Zameen listings as PakLead leads via the operator's browser."
license: Project-native (PakLead Engine)
---

# PakLead Zameen Harvest

Collect PUBLIC listings from Zameen.com through the operator's browser and emit them in the engine import schema. Property sellers are the wholesale book's core lead type: a listing is a seller signal, and price/area/urgency text is what the engine scores.

## What to collect per listing
- Title + description text (this becomes `text_original`; always include the asking price, area/size (marla/kanal), and locality + city in the text).
- `platform`: "zameen"
- `platform_post_id`: the listing id from the URL when visible, else the URL slug.
- `author_handle`: agency or agent name when shown (prefix "Agency:" so supply-side records stay identifiable).
- `url`: the listing URL.
- `published_at`: listing date converted to ISO-8601 when shown.
- Urgency/value signals verbatim in the text: "urgent", "investor rate", "below market", "final", "negotiable", "demand" figures.

## Priority targets
- Houses for sale in Islamabad (then Lahore, Karachi) — newest first.
- Plots with investor-rate / urgent language.
- Rentals only when a cycle brief explicitly asks for them.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task, in Bushra Rahman's own authenticated browser session (Zameen public pages need no login — never log in unless she instructed it).
- Strictly read-only while collecting: never contact an agent, never submit a form, never change account state.
- Stop immediately and hand off on any checkpoint, verification prompt, or rate-limit notice.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
