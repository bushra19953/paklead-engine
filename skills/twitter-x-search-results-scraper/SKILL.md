---
name: twitter-x-search-results-scraper
description: "Collect search results from Twitter/X — matching posts, URLs, authors, engagement. Use when the user wants to collect content matching keywords for research or monitoring."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Twitter X Search Results Scraper

Use this Skill for Twitter/X search research, keyword monitoring, account discovery, hashtag tracking, and social listening on Twitter/X.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect Twitter/X keyword, account, hashtag, language, or date-filtered search results
- Monitor brand, competitor, campaign, and topic mentions
- Discover accounts and conversations around a niche or event
- Export source-linked search result datasets for analysis

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Query, result URL, result type, ranking or recency context, and source page
- Post text, author handle, timestamp, links, hashtags, and mentions
- Replies, reposts, quotes, likes, views when visible, and engagement signals
- Search filters, pagination context, and collection timestamp

## Instructions

1. Identify the target Twitter/X search query, advanced search URL, keyword list, hashtag, account query, or date filter.
2. Identify the requested fields, approximate result count, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Scrape Twitter/X search results for these keywords."
- "Collect Twitter/X posts matching this hashtag and date range."
- "Find accounts and posts mentioning this competitor."
- "Monitor Twitter/X search results for these product terms."

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
