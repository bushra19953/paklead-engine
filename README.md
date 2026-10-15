# PakLead Engine — Pakistan Social-Lead Intelligence Platform

Daily, automated discovery of high-intent leads from Pakistani Facebook
Groups, Reddit, and manually-imported social/classified sources — with
intent classification in English, Roman Urdu, and Urdu script, cross-
platform comparison, loophole detection, transparent scoring, and a
dashboard. 100% free and open-source.

**Design doctrine (binding):** the system automates *discovery and
scoring*. It never evades platform protections, never contacts authors,
never signs anything, never moves money. Humans make every consequential
move. See `../wholesale_system/artifacts/pk_legal_boundaries.md` (SHR
rules) and `TRD_PK.md` for the full specification.

## Quick start

```bash
cd ~/workspace/pak_lead_engine
# 1) Demo run — no credentials, no network, fictional sample data:
python run_daily.py --demo

# 2) Tests (stdlib only):
python tests/test_engine.py

# 3) Live run (needs Reddit app credentials in .env):
cp .env.example .env   # fill in real values; never commit .env
python run_daily.py

# 4) API + scheduler + dashboard (Docker):
docker compose up
# API:       http://localhost:8000/docs
# Dashboard: http://localhost:8501
```

## How it works

1. **Collect** — per-source adapters, each carrying its legitimate access
   route (`api`, `cli_adapter`, `manual_import`; browser route is
   opt-in-only and read-only):
   - Reddit: official API via PRAW (`collectors/reddit_collector.py`).
     Missing creds → clear warning + empty result, never a crash.
     Abort on the first 429; exponential back-off; never identity rotation.
   - Facebook: three backends (`collectors/facebook_adapter.py`).
     `cli_adapter` uses the authenticated `facebook-cli` group-reading
     tool; `manual_import` reads a CSV/JSON file; `browser_optin` is a
     documented opt-in that contains **no automation code** — browser work
     runs through the operator's live browser session tasks. No stealth,
     no cookie replay, no evasion tooling exists anywhere in this repo.
   - Instagram / X / LinkedIn / OLX / PakWheels: manual CSV/JSON import
     (`collectors/import_adapter.py`). WhatsApp automation is designed out
     entirely — manual import only.
2. **Normalize** — verbatim original preserved; normalized copy for
   scoring (Urdu-script folding, honorific/noise stripping, language
   detection: en / roman_urdu / urdu / mixed).
3. **Classify** — phrase bank at `data/intent_phrases.json` (hot-
   reloadable) drives 10 intent categories, city/area extraction, PKR
   budget parsing ("50k", "1.5 lac", "2.5 crore"), urgency markers, and
   price-inquiry qualifiers. Scam red-flags and complaints go to the
   quarantine queue for human review — never auto-surfaced.
4. **Compare** — dedupe on source URL; cross-platform clustering via
   published-phone hard key, then stdlib TF-IDF cosine ≥ 0.65 gated on
   same city + intent + 72h window. Needs are merged, never identities.
5. **Score** — six transparent inputs: intent (weight×6), recency,
   budget, urgency, cross-platform, contactability. Every score carries
   its breakdown; hot at ≥70 or via triggers (e.g. complaint with
   replacement urgency, hiring with a stated deadline).
6. **Loopholes** — rolling 30-day seeker:provider ratios (≥3:1 with a
   seeker floor), underserved cities, complaint themes (reported as
   themes, never accusations), price gaps vs. market anchors.
7. **Alert** — hot leads hit the dashboard queue and the operator-owned
   webhook; the system never messages authors itself.

## Key files

| Path | What |
|---|---|
| `config.py` | `.env` loading, rate budgets, scoring config (intent×6 per PRD §8 worked examples) |
| `models.py` | SQLite schema — all 10 TRD data models |
| `collectors/` | base, reddit, facebook (3 backends), import adapters |
| `engine/classifier.py` | normalization, language, intent, city, budget, quarantine |
| `engine/scorer.py` | 6-input deterministic scorer |
| `engine/comparator.py` | dedupe + cross-platform clustering |
| `engine/loophole.py` | gap/underserved/complaint/price-gap detection |
| `engine/pipeline.py` | daily orchestration, audit, kill switch, idempotency |
| `scheduler.py` | APScheduler daily job (Asia/Karachi) |
| `api.py` | FastAPI read API + operator-only writes (no outreach endpoints) |
| `dashboard.py` | Streamlit dashboard |
| `data/intent_phrases.json` | phrase bank (10 categories, 277 phrases, 11 cities) |
| `sample_data/sample_posts.json` | 14 fictional demo posts |

## Compliance notes

- Facebook browser route (`FB_BROWSER_OPTIN=true`): **read-only, ≤1
  action/5s, halts on any login challenge/CAPTCHA/checkpoint.** Meta may
  still flag automation — this is the operator's accepted risk, and the
  zero-risk alternative (manual import) is always available.
- WhatsApp statuses/groups cannot be automated through any legitimate
  route and are excluded by design.
- Only public post text + author handle + timestamp + link are stored;
  phone numbers only from public posts; no resale/sharing; suppression
  and deletion-sync honored; 180-day retention default.
- All book and platform rule text is paraphrased; formulas, numbers, and
  API facts are stated exactly. Sample data is fictional.

## Roadmap

1. `python run_daily.py --demo` — verify scoring and demo data.
2. Register a Reddit app, set `.env`, run live: `python run_daily.py`.
3. Point `FB_ACCESS_MODE` at `cli_adapter` for groups you belong to, or
   use `manual_import` with exported CSVs.
4. `docker compose up` for the daily loop + API + dashboard.
5. Tune `data/intent_phrases.json` (hot-reloadable) and scoring weights
   in `config.py` from operator feedback; per-vertical overrides supported.
