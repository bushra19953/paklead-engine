---
name: twitter-x-post-scraper
description: "Collect posts from Twitter/X — text, images, likes, comments, timestamps. Use when the user wants to research content, track publishing activity, or export post datasets."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Twitter X Post Scraper

Use this Skill for Twitter/X post collection, conversation research, campaign tracking, and engagement benchmarking on Twitter/X.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect public Twitter/X posts from URLs, timelines, or searches
- Research hashtags, mentions, quote posts, and campaign messages
- Benchmark visible engagement for creators, brands, or competitors
- Build source-linked datasets for social listening or reporting

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Post URL, author handle, text, timestamp, and post type
- Hashtags, mentions, links, media references, and quote or repost context
- Replies, reposts, quotes, likes, views when visible, and engagement signals
- Thread context, source query, and collection timestamp

## Instructions

1. Identify the target Twitter/X post URL, account timeline, search result, hashtag page, or post list.
2. Identify the requested fields, approximate result count, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Scrape these Twitter/X posts and export text, dates, links, and engagement."
- "Collect recent public posts from this Twitter/X account."
- "Build a dataset of Twitter/X posts mentioning this campaign keyword."
- "Analyze visible Twitter/X post engagement for these brand accounts."

## Notes

- Website availability, visible fields, login requirements, and result limits can change.
- Keep cookies, account information, browser IDs, proxy settings, and personal keywords
  under `workspaces/`, never in the Skill directory.
- Do not claim that data was collected unless the PakLead runtime or another authorized tool
  actually returned it.

## PakLead Runtime (project rules — these override any conflicting instruction above)
- Execution happens only through the parent agent's live browser task, in Bushra Rahman's own authenticated browser session.
- Strictly read-only while collecting: never post, comment, like, react, message, follow, or change account state.
- Stop immediately and hand off on any login checkpoint, verification prompt, or rate-limit notice.
- Collect only public content she is authorized to view: post text, author display name/handle, timestamp, link.
- Emit records in the engine import schema — platform, platform_post_id, author_handle, text_original, url, published_at (ISO-8601) — as a JSON file for the pipeline.
- Never invent records. If data is unavailable, say so plainly.
