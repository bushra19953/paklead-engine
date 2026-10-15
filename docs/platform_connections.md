# PakLead — Platform Connection Playbook

How the app connects to each platform, and why. Researched 2026-10-15.
Rule for every platform: use the most official route that exists; where no
sanctioned route exists, use the operator's own browser session, read-only,
through the manual-import pipeline. No stealth scraping, no cookie replay,
no third-party scraper APIs (SHR boundaries).

## Facebook — LIVE
- Route: operator's own logged-in browser session (read-only) → manual import.
- Why: the official Graph API does not expose other groups' member posts for
  this use; the facebook-cli connector is not connected in this environment.
- Needs from operator: stay logged in in the shared browser.
- Skills: facebook-group-scraper, facebook-post-scraper, facebook-comment-scraper,
  facebook-search-results-scraper, social-purchase-intent-lead-finder, social-keyword-monitor.
- Cadence: cyclic cron every 2h (08:00–22:00 PKT) + 21:00 digest.

## Reddit — APPROVAL PENDING
- Route: official API only (PRAW, read-only client-credentials).
- Status: since Nov 2025 Reddit requires pre-approval for ALL API access;
  our access request was submitted 2026-10-15 (account u/Inevitable-Watch654,
  subreddits r/Pakistan, r/karachi, r/Lahore, r/islamabad). Reported wait 2–4 weeks.
- On approval: create the script app, operator pastes the secret into .env herself.
- Skills: reddit family in the upstream repo is ADAPT-class but PARKED — never
  used as a bypass around the approval gate.

## X (Twitter) — BROWSER ROUTE (official API priced)
- Official route exists: X API v2. As of Feb 2026 it is pay-per-use for new
  developers (about $0.005 per post read; recent search covers the last 7 days;
  legacy Basic $200/mo and Pro $5,000/mo are closed to new signups; no free
  read tier). Full-archive search needs an access upgrade / Enterprise.
- PakLead's chosen route for now: operator's logged-in browser session →
  manual import (zero cost, read-only). The paid API is a documented upgrade
  option if read volume ever justifies ~$5 per 1,000 posts read.
- Needs from operator: log in to X in the shared browser when the X cycle starts.
- Skills: twitter-x-search-results-scraper, twitter-x-post-scraper.
- NOT used: internal/GraphQL cookie-based access — violates the project's
  no-cookie-automation boundary.

## TikTok — BROWSER ROUTE (no public-content API)
- Official surfaces: Display API (the operator's OWN account videos via OAuth),
  Content Posting API (publishing), Research API (public content — but limited
  to academic institutions in the US/EEA/UK/Switzerland and non-commercial use;
  a Pakistan-based operator is not eligible).
- PakLead's chosen route: browser search/discovery in session or public pages →
  manual import. TikTok's public search is largely usable logged-out.
- Skills: tiktok-search-results-scraper, tiktok-video-scraper, tiktok-comment-scraper.

## OLX Pakistan — BROWSER ROUTE (no developer API)
- No official developer/public API exists. Public listings are readable on
  olx.com.pk without login.
- PakLead's chosen route: browser harvest of public search/category pages →
  manual import (records carry platform "olx").
- Skill: paklead-olx-harvest (project-native).

## Zameen.com — BROWSER ROUTE (no developer API)
- No official developer/public API exists. Public listings are readable on
  zameen.com without login.
- PakLead's chosen route: browser harvest of public search pages →
  manual import (records carry platform "zameen"). Property sellers are the
  wholesale book's core lead type.
- Skill: paklead-zameen-harvest (project-native).

## Reverse-engineering notes (open-source research, 2026-10-15)
Method skill: paklead-frontend-api-discovery — discover the JSON endpoints a
site's own frontend uses for public data, map to the import schema, wrap as a
polite collector. Learned from open-source projects:
- OLX country sites share one platform whose frontend exposes search /
  listing / seller / category / location data calls (publicly documented by
  open-source wrappers for other OLX countries); listing payloads can include
  a previous price — a built-in price-drop (deal) signal. The .com.pk host's
  exact endpoints must be verified in the browser before coding a collector.
- Zameen search URLs encode internal city ids (`/Homes/{City}-{id}-{page}.html`;
  Lahore-1, Karachi-2, Islamabad-3 pattern). Zameen's robots.txt disallows the
  city-listing paths, so Zameen stays browser-based at human scale.
- Graana public listing pages are readable with plain HTTP + HTML parsing
  (open-source scrapers + one n8n system polling Graana every 2h for client
  matching). Graana added as a first-class property source
  (skill: paklead-graana-harvest).

## Also in the original mission scope (not yet connected)
- PakWheels (same pattern as OLX/Zameen: public listings, browser route).
- Instagram (connector exists in this environment; hashtag/location reading
  is a later wave).
- LinkedIn Pakistan (connector not present; public pages only, later wave).

## The connection pattern (all platforms)
browser/API collection → JSON in the engine import schema → ImportAdapter
(per-source file via the source row's boundary_note, watermark deltas) →
classifier → scorer → comparator → dashboard/API → digest.
Every new platform is: (1) a source row, (2) a harvest skill, (3) records in
the shared schema. Nothing platform-specific lives outside its skill + source.
