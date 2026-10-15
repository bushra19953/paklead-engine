---
name: paklead-olx-harvest
description: "PakLead harvest playbook for OLX Pakistan (olx.com.pk) public listings — property and general classifieds. Use when collecting OLX listings as PakLead leads via the operator's browser."
license: Project-native (PakLead Engine)
---

# PakLead OLX Harvest

Collect PUBLIC listings from OLX Pakistan through the operator's browser and emit them in the engine import schema.

## What to collect per listing
- Title + full description text (this becomes `text_original`; include the price line and area in the text when the description is thin).
- `platform`: "olx"
- `platform_post_id`: the listing id from the URL when visible, else the URL slug.
- `author_handle`: seller display name when shown (note "dealer/agent" in the text when OLX marks the seller as a business).
- `url`: the listing URL.
- `published_at`: listing time converted to ISO-8601 when shown.
- Price in PKR, area/locality + city, and any urgency signals in the text ("urgent sale", "final price", "negotiable", "must sell") — keep them verbatim in `text_original`; the engine's classifier and budget extractor read them from there.

## Priority targets
- Property for sale (houses, plots) in Islamabad, Lahore, Karachi — newest first.
- "Urgent sale" / distress listings in any category.
- Wanted/buying posts where the category supports them.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task, in Bushra Rahman's own authenticated browser session (OLX public pages usually need no login — never log in unless she instructed it).
- Strictly read-only while collecting: never message a seller, never post, never change account state.
- Stop immediately and hand off on any checkpoint, verification prompt, or rate-limit notice.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
