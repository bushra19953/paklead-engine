---
name: social-keyword-monitor
description: "Social Keyword Monitor for cross-platform social media keyword monitor collection, research, monitoring, analysis, and export. Use when the user asks to scrape, extract, collect, export, monitor, research, analyze, or find browser-visible cross-platform social media data for this workflow: Monitor public posts and comments for keyword sets, aliases, and query combinations. Covers searches such as Social Keyword Monitor, cross-platform social media keyword monitor scraper, cross-platform social media keyword monitor extractor, cross-platform social media keyword monitor export, cross-platform social media keyword monitor research. Supports public or authorized browser-visible data through the PakLead runtime."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Social Keyword Monitor

Use this Skill for cross-platform social media keyword monitor collection, research, monitoring, analysis, and export.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Monitor public posts and comments for keyword sets, aliases, and query combinations
- Research and compare keyword monitor across selected social platforms and targets
- Monitor visible activity, changes, and engagement signals over repeated collections
- Export structured, source-linked keyword monitor records for analysis or operations

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Platform, target account or keyword, source URL, and matched content type
- Visible text, profile or content metadata, timestamps, and classification context
- Comparable audience, engagement, ranking, velocity, sentiment, or intent signals
- Query rules, monitoring window, collection timestamp, and source references

## Instructions

1. Identify the target platforms, accounts, keywords, URLs, filters, or monitoring criteria.
2. Identify the requested fields, approximate result count, filters, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Use social-keyword-monitor to monitor public posts and comments for keyword sets, aliases, and query combinations."
- "Collect visible cross-platform social media keyword monitor data for these URLs or targets and export a CSV."
- "Research this cross-platform social media keyword monitor workflow and include the relevant fields and source links."
- "Monitor these cross-platform social media targets for keyword monitor changes and return a structured comparison."

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
