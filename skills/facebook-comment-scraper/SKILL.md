---
name: facebook-comment-scraper
description: "Collect comments from Facebook — text, author, timestamp, likes, replies. Use when the user wants to mine audience feedback, questions, or sentiment."
license: MIT
metadata:
  author: sam6dvpte34
  version: "0.1.0"
---

# Facebook Comment Scraper

Use this Skill for Facebook comment collection, audience research, reply mining, sentiment analysis, and feedback tracking on Facebook.

This Skill runs through the PakLead Runtime defined at the end of this file — the parent agent's live browser task in the account holder's own authenticated session.

## Common Use Cases

- Collect visible comments and replies from public or authorized Facebook posts
- Mine audience questions, objections, praise, and product feedback
- Track Page, group, or campaign reactions in comment threads
- Export source-linked comment datasets for analysis

## Common Data

Depending on what is visible and authorized, relevant fields can include:

- Comment text, author name, profile link, timestamp, and reply depth
- Reaction count, Page reply signals, pinned status when visible, and moderation context
- Source post URL, post author, post snippet, and Page or group context
- Thread structure, pagination context, and collection timestamp

## Instructions

1. Identify the target Facebook post URL, public Page post, authorized group post, or list of post URLs.
2. Identify the requested fields, approximate result count, and preferred output format.
3. Invoke the `browser-act` Skill when live browser access or website interaction is
   required, and follow its current instructions.
4. Work only with public data or data the user is authorized to access.
5. Return the requested result directly when available. If access or data is unavailable,
   state the limitation without inventing records.

Do not build or require a platform-specific script before attempting the task. Let
the PakLead runtime choose the appropriate browser workflow at runtime.

## Example Requests

- "Scrape visible comments from this Facebook post."
- "Collect Facebook post comments and replies with authors and timestamps."
- "Find repeated customer questions under these Facebook Page posts."
- "Export Facebook comment text and source links for sentiment analysis."

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
