---
name: tiktok-comment-scraper
description: "Collect comments from TikTok — text, author, timestamp, likes, replies. Use when the user wants to mine audience feedback, questions, or sentiment."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Tiktok Comment Scraper

Use this Skill for TikTok comment collection, audience research, reply mining, feedback tracking, and sentiment analysis on TikTok.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect visible comments and replies from public or authorized TikTok videos
- Mine audience questions, objections, praise, and product feedback
- Track creator-audience interactions and campaign responses
- Export source-linked comment datasets for analysis

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Comment text, author username, profile link, timestamp, and reply depth
- Like count, creator reply status, location when visible, and pinned context
- Source video URL, caption snippet, creator, and video metadata
- Thread structure, nested replies, and collection timestamp

## Instructions

1. Identify the target TikTok video URL or list of video URLs.
2. Identify the requested fields, approximate result count, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Scrape visible comments from this TikTok video."
- "Collect TikTok comments and replies with authors and timestamps."
- "Find repeated audience questions under these TikTok videos."
- "Export TikTok comment text and source links for sentiment analysis."

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
