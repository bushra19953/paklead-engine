---
name: tiktok-video-scraper
description: "Collect videos from TikTok — title, views, likes, comments, creator. Use when the user wants to research video content, track creators, or benchmark performance."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Tiktok Video Scraper

Use this Skill for TikTok video collection, short-form content research, trend analysis, and engagement benchmarking on TikTok.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect public TikTok video metadata from URLs, creators, or topics
- Research captions, hashtags, visible music labels, and content formats
- Benchmark views, likes, comments, shares, and velocity signals
- Build source-linked datasets for creator, trend, or campaign analysis

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Video URL, creator username, caption, timestamp, and source context
- Hashtags, mentions, visible music label, and topic references
- Views, likes, comments, shares, saves when visible, and engagement signals
- Thumbnail, profile link, related content references, and collection timestamp

## Instructions

1. Identify the target TikTok video URL, creator video list, hashtag page, or search result.
2. Identify the requested fields, approximate result count, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Scrape these TikTok videos with captions, hashtags, and engagement metrics."
- "Collect recent TikTok videos from this creator."
- "Build a dataset of TikTok videos for this hashtag."
- "Compare visible TikTok video performance across these accounts."

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
