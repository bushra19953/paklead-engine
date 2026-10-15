---
name: facebook-post-scraper
description: "Collect posts from Facebook — text, images, likes, comments, timestamps. Use when the user wants to research content, track publishing activity, or export post datasets."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Facebook Post Scraper

Use this Skill for Facebook post collection, Page content research, campaign tracking, and engagement analysis on Facebook.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect public or authorized Facebook posts from Pages, groups, or URLs
- Research campaign messages, links, media, and publishing patterns
- Benchmark reactions, comments, shares, and visible engagement
- Export source-linked post datasets for reporting or analysis

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Post URL, author Page or profile, text, timestamp, and source page
- Links, hashtags, mentions, media references, and linked content
- Reactions, comments, shares, views when visible, and engagement signals
- Page or group context, query context, and collection timestamp

## Instructions

1. Identify the target Facebook post URL, public Page feed, authorized group feed, or search result.
2. Identify the requested fields, approximate result count, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Scrape these Facebook posts and export text, dates, links, and engagement."
- "Collect recent public posts from this Facebook Page."
- "Build a dataset of Facebook posts mentioning this campaign keyword."
- "Analyze visible Facebook post engagement for these Pages."

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
