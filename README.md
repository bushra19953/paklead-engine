# PakLead Engine — Pakistan Social-Lead Intelligence Platform

Daily, automated discovery of high-intent leads from Pakistani Facebook
Groups, Reddit, and manually-imported social/classified sources — with
intent classification in English, Roman Urdu, and Urdu script, cross-
platform comparison, loophole detection, transparent scoring, and a
dashboard. 100% free and open-source.

**Design doctrine (binding):** the system automates *discovery and
scoring*. It never evades platform protections, never contacts authors,
never signs anything, never moves money. Humans make every consequential
move. See `docs/legal_boundaries.md` (SHR rules) and `docs/` for the full
specification.

## Quick start (fresh clone)

```bash
git clone https://github.com/bushra19953/paklead-engine
cd paklead-engine
python -m venv .venv && source .venv/bin/activate

# 1) Demo run — no credentials, no network, fictional sample data:
python run_daily.py --demo

# 2) Tests (the engine core is pure stdlib — no install needed to test):
python tests/test_engine.py

# 3) Optional extras — only what you actually use:
pip install -r requirements.txt   # ~470 MB; Reddit (praw), scheduler
                                  # (apscheduler), API (fastapi/uvicorn),
                                  # dashboard (streamlit), .env (dotenv)
cp .env.example .env              # fill in real values; never commit .env
python run_daily.py               # live run (needs Reddit creds in .env)

# 4) API + scheduler + dashboard (Docker):
docker compose up
# API:       http://localhost:8000/docs
# Dashboard: http://localhost:8501
```

Minimum to develop on the engine (classifier, scorer, comparator,
pipeline, collectors, tests): **nothing but Python 3.10+**. Extras
install only for the live pieces you enable: Reddit API, the daily
scheduler, the FastAPI service, or the Streamlit dashboard.

## Web sweep worked example

The engine collects OLX / Zameen / Graana / PakWheels through their
public pages' own structured data (`web_public` access route). Source
rows are generated from town trees the sites themselves link to — never
guessed URLs (see `docs/platform_connections.md` and
`engine/town_source_factory.py`):

```bash
# Dry-run: show the source rows that would be created for Islamabad towns
python -m engine.town_source_factory --city islamabad
# Human-readable plan (creates nothing). For piping:
# python -m engine.town_source_factory --city islamabad --emit-json
# Typical boundary_note: "url:https://www.olx.com.pk/g-13_g5001007/houses_c1721;pages:1"
#                         "url:https://www.zameen.com/Houses/Islamabad-3/G_13-330.html;pages:3"

# A source's pages are fetched with politeness delays and per-host caps;
# a 403/429/CAPTCHA stops that source for the run (SHR-1, SHR-11).
```

## Command map

| What you want | Command |
|---|---|
| Score the demo posts, see a digest | `python run_daily.py --demo` |
| Run one full collection cycle | `python run_daily.py` |
| Daily digest of HOT leads (text) | `python run_daily.py` — prints the run summary incl. new-HOT alerts |
| Add new town sources (dry-run) | `engine/town_source_factory.py` `dry_run()` first, then insert |
| Retire stale leads (180-day, dry-run) | `retention.purge(conn, days=180)` (dry-run is the default; real purge also needs `confirm=True`) |
| Buyer registry + buyer matching | `engine/buyer_registry.py` `derive_buyers(conn)`, `match_buyers(conn, seller_lead)` |
| Per-source yield alarms | `engine/yield_monitor.py` (alarms written during runs) |
| Data-quality report | `python engine/data_quality.py` |
| API (leads, buyers, detail, sync) | `uvicorn api:app` → http://localhost:8000/docs |
| Dashboard (max-information view) | `streamlit run dashboard.py` |
| Kill all collection immediately | set `KILL_SWITCH=true` in `.env` (SHR-12) |

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
