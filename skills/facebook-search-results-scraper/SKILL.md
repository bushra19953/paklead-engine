---
name: facebook-search-results-scraper
description: "Collect search results from Facebook — matching posts, URLs, authors, engagement. Use when the user wants to collect content matching keywords for research or monitoring."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Facebook Search Results Scraper

Use this Skill for Facebook search results collection, research, monitoring, analysis, and export.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect available public Page, post, group, event, and video search results
- Research and compare search results across selected Facebook targets
- Monitor visible activity, changes, and engagement signals over repeated collections
- Export structured, source-linked search results records for analysis or operations

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Search query, active filters, result type, rank, and result URL
- Result title, name, handle, snippet, category, and visible metadata
- Visible audience, engagement, date, status, and platform-specific signals
- Result page, pagination context, collection timestamp, and source reference

## Instructions

1. Identify the target Facebook search query, filters, result page, or target record list.
2. Identify the requested fields, approximate result count, filters, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Use facebook-search-results-scraper to collect available public Page, post, group, event, and video search results."
- "Collect visible Facebook search results data for these URLs or targets and export a CSV."
- "Research this Facebook search results workflow and include the relevant fields and source links."
- "Monitor these Facebook targets for search results changes and return a structured comparison."

## Notes

- Website availability, visible fields, login requirements, and result limits can change.
- Keep cookies, account information, browser IDs, proxy settings, and personal keywords
  under `workspaces/`, never in the Skill directory.
- Do not claim that data was collected unless the PakLead runtime or another authorized tool
  actually returned it.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task, in Bushra Rahman's own authenticated browser session.
- Strictly read-only while collecting: never post, comment, like, react, message, or change account state. Joining a group happens only when she explicitly instructs it in chat — never as part of a collection run.
- Stop immediately and hand off on any login checkpoint, verification prompt, or rate-limit notice.
- Collect only from groups and pages she belongs to or is authorized to view, and only: public post text, author display name, timestamp, link.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
