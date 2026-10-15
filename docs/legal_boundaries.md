# Legal & Ethical Boundaries (SHR rules)

The machine- and process-enforced boundary layer for every collection,
storage, and surfacing step of the PakLead Engine. Every hard rule is
traceable by its SHR id. This is a standalone summary; the full rulebook
with legal citations lives in the wholesale-system design artifacts.

**Not legal advice.** Platform terms change; Pakistan's Prevention of
Electronic Crimes Act (PECA) and general computer-misuse law are caution
points only — consult qualified counsel in your own jurisdiction.

## Per-platform access matrix

| Platform | Legitimate access route | NOT allowed |
|---|---|---|
| Reddit | Official Reddit API (PRAW, within rate limits, attribution) | HTML scraping to bypass the API; limit circumvention |
| Facebook | Operator's own authenticated session (their own clicks) or CLI/manual CSV import | Stealth browser automation, cookie-session replay, headless farms |
| OLX / Zameen / Graana / PakWheels | Public pages' own structured data (JSON-LD, server cards, `__NEXT_DATA__`), honest UA, per-host politeness delays | Internal search backends, embedded credentials, bulk crawling past bot-blocks |
| X / TikTok | Public pages via the operator's browser route | Any credential/cookie automation |
| WhatsApp | **None — designed out entirely** | Any automated monitoring of statuses/groups/chats |
| LinkedIn / Instagram | Official APIs only, within their scopes | Profile/search scraping |

**SHR-1:** only approved/legitimate surfaces. A 403/429/CAPTCHA/block is an
answer, not an obstacle — stop that source, log it, never rotate
identities, IPs, or tooling to continue.

## Data-protection rules

- **SHR-2** Minimum viable record: public post text, author handle,
  timestamp, source URL. No profile photos, friend graphs, follower lists.
- **SHR-3** Phones/emails/addresses the author themselves published in a
  public post may be stored minimally (one field, attached to the lead)
  for the operator's own follow-up. Never resold, shared, published, or
  exported to third parties.
- **SHR-4** Honor deletions: re-check stored leads against source URLs;
  purge records whose posts were deleted or made private.
- **SHR-5** Suppression/opt-out list: excluded from every export, match,
  and alert.
- **SHR-6** 180-day retention floor: purge leads with no activity (no
  contact, update, or score change) unless the operator opts to keep them
  (see `engine/retention.py`, dry-run mode).
- **SHR-7/8** PECA (Pakistan) + general computer-misuse caution: anything
  requiring bypassing an access control (logins, CAPTCHAs, rate gates) is
  out of scope.
- **SHR-9** No private-identity resolution: no reverse-lookup, no phone
  enrichment, no cross-platform identity merge without a strong shared
  key (e.g. an identical published phone). Name-only matches stay
  unmerged candidates.

## Operational guardrails (enforced in code)

- **SHR-10** Rate-limit budgets are configuration. A run hitting a
  429/rate-limit stops that platform's collection for the run, logs it,
  backs off — never rotates identities.
- **SHR-11** robots.txt honored before HTTP fetches; bot-blocks are
  never worked around.
- **SHR-12** Kill-switch: one flag (`KILL_SWITCH`) disables all
  collection immediately; disabling never deletes stored leads.
- **SHR-13** Audit log: every run records platform, start/end, items
  fetched/stored/purged, rate-limit events, access route used.
- **SHR-14** No automated outreach: the system never messages, comments,
  DMs, or contacts any author. Outreach is the human's job, through the
  platform's own UI.
- **SHR-15** Idempotent daily loop: re-running never duplicates leads.
  Dedup key = source URL (platform + post id).
- **SHR-16** Credentials load from environment/secret files only — never
  hard-coded, never committed, never printed in logs.
- **SHR-17** Language handling: no script-transliteration identity
  inference or geolocation beyond city tags the author stated.

## System must never

1. Use stealth automation, cookie replay, headless farms, or identity
   rotation to collect data.
2. Monitor WhatsApp statuses/groups/chats by any automated means.
3. Scrape LinkedIn profiles/search results or other API-gated surfaces.
4. Circumvent a rate limit, CAPTCHA, login wall, or bot-block by
   switching identities, IPs, or tooling.
5. Store, buy, or enrich private contact details the author did not
   publish in the post itself.
6. Resell, share, publish, or export collected lead data to third
   parties, brokers, or marketing lists.
