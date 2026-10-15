"""
PakLead Engine configuration.

Loads environment variables (python-dotenv if installed, otherwise plain
os.environ), the versioned scoring config, and per-platform rate budgets.
Core engine paths (config + models + engine) never import third-party
packages — the classifier, scorer, comparator and loophole engine are
pure stdlib. Connectors, scheduler, API and dashboard degrade gracefully
when their optional dependencies are missing.

No credentials are ever printed, logged, or committed.
"""

from __future__ import annotations

import json
import os
import pathlib

try:  # optional, free dependency
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback is plain env parsing

    def load_dotenv(*args, **kwargs):  # type: ignore
        return False


BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SCORING_CONFIG_PATH = DATA_DIR / "scoring_config.yaml"
PHRASE_BANK_PATH = DATA_DIR / "intent_phrases.json"


def _load_env_file(path: pathlib.Path) -> dict:
    """Minimal .env parser used when python-dotenv is unavailable."""
    values: dict = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


class Settings:
    """All runtime settings. Fails closed on missing required secrets."""

    def __init__(self, env_path: str | None = None):
        load_dotenv(env_path or str(BASE_DIR / ".env"))
        file_vals = _load_env_file(BASE_DIR / ".env")

        def get(name: str, default: str = "") -> str:
            return os.environ.get(name, file_vals.get(name, default))

        # --- Reddit (official API; creds only needed for live collection) ---
        self.REDDIT_CLIENT_ID = get("REDDIT_CLIENT_ID")
        self.REDDIT_CLIENT_SECRET = get("REDDIT_CLIENT_SECRET")
        self.REDDIT_USER_AGENT = get("REDDIT_USER_AGENT", "PakLeadEngine/1.0")
        self.REDDIT_USERNAME = get("REDDIT_USERNAME")

        # --- Facebook adapter ---
        self.FB_ACCESS_MODE = get("FB_ACCESS_MODE", "manual_import")  # api|cli_adapter|manual_import
        self.FB_BROWSER_OPTIN = get("FB_BROWSER_OPTIN", "false").strip().lower() == "true"

        # --- Alerts / dashboard ---
        self.ALERT_WEBHOOK_URL = get("ALERT_WEBHOOK_URL")
        self.DASHBOARD_API_TOKEN = get("DASHBOARD_API_TOKEN")

        # --- Storage / schedule ---
        self.DB_PATH = get("DB_PATH", str(BASE_DIR / "data" / "pak_leads.db"))
        self.CHROMA_PATH = get("CHROMA_PATH", str(BASE_DIR / "data" / "chroma"))
        self.RUN_TIME = get("RUN_TIME", "0 6 * * *")

        # --- Kill switch (SHR-12): stops collection, never deletes data ---
        self.KILL_SWITCH = get("KILL_SWITCH", "false").strip().lower() == "true"

        # --- Per-platform rate budgets (requests/minute). Config can only
        #     lower these, never exceed the documented ceilings. ---
        self.RATE_BUDGETS = {
            "reddit": int(get("RATE_BUDGET_REDDIT", "30")),
            "facebook": int(get("RATE_BUDGET_FACEBOOK", "12")),
            "public_page": int(get("RATE_BUDGET_PUBLIC_PAGE", "30")),
        }

        self.SCORING_CONFIG_VERSION = "1.0.0"

    def reddit_configured(self) -> bool:
        return bool(self.REDDIT_CLIENT_ID and self.REDDIT_CLIENT_SECRET and self.REDDIT_USER_AGENT)

    def missing_required_for_live(self) -> list[str]:
        missing = []
        if not self.reddit_configured():
            missing.append("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT")
        return missing


def load_scoring_config() -> dict:
    """Versioned scoring design parameters (weights, windows, thresholds).

    NOTE on intent scale: TRD FR-PK-39 labels the intent input "0-40", but
    PRD section 8's worked acceptance examples compute intent as
    category_weight x 6 (8x6 = 48, 7x6 = 42) and sum to 93 / 47. The
    acceptance examples are the testable contract, so they win:
    intent_multiplier = 6, totals capped at 100.
    """
    return {
        "version": "1.0.0",
        "intent_multiplier": 6,            # category weight (1-10) x 6  (PRD S8 worked examples)
        "price_inquiry_bonus": 5,          # +5 if price_inquiry qualifier present
        "complaint_replacement_bonus": 10, # complaint_pain + replacement urgency, quarantine-cleared only
        "recency_max": 15,                 # 15 within 24h, linear decay to 0 at freshness window end
        "recency_full_hours": 24,
        "budget_parsed": 10,               # budget_pkr successfully parsed
        "budget_phrase_only": 5,           # budget phrase matched, no parseable amount
        "urgency_per_marker": 10.0,        # distinct markers x 10.0 (PRD S8: one "urgent" -> +10)
        "urgency_cap": 10,
        "urgency_marker_count_cap": 4,
        "cross_platform_two": 8,           # exactly 2 distinct platforms
        "cross_platform_three_plus": 15,   # >= 3 distinct platforms
        "contact_phone_published": 10,     # phone/WhatsApp in the public post
        "contact_open_thread": 5,          # open comment thread
        "contact_inbox_gate": 3,           # "DM for price" / inbox gate
        "serious_buyer_bonus": 2,          # cultural: "sirf serious buyer rabta kare"
        "hot_threshold": 70,
        # Freshness windows (days) per intent family (AKU-PK-17 design parameters)
        "freshness_windows": {
            "seeking": 7,    # seeking_service, seeking_product, freelance_seeking, job_seeking, hiring
            "selling": 30,   # selling, property_offered, property_wanted, freelance_offering
            "complaint": 3,  # complaint_pain
        },
        "intent_window_family": {
            "seeking_service": "seeking", "seeking_product": "seeking",
            "freelance_seeking": "seeking", "job_seeking": "seeking", "hiring": "seeking",
            "selling": "selling", "property_offered": "selling", "property_wanted": "selling",
            "freelance_offering": "selling", "complaint_pain": "complaint",
        },
        "cluster_text_similarity_threshold": 0.65,  # TF-IDF cosine, stdlib
        "cluster_time_window_hours": 72,
        "cluster_phone_window_days": 7,            # published-phone hard key (PRD AC-M5-1)
        "loophole_window_days": 30,
        "loophole_min_ratio": 3.0,
        "loophole_min_seekers": 5,
        "underserved_min_seekers": 10,
        "underserved_max_provider_reply_rate": 0.05,
        "price_gap_pct": 25.0,
        "alert_daily_cap_per_vertical": 25,
        "retention_days": 180,
        # Per-vertical weight overrides (default: global scheme)
        "vertical_overrides": {
            "property": {"property_wanted": 9, "property_offered": 8},
        },
    }


def load_phrase_bank(path: pathlib.Path | None = None) -> dict:
    with open(path or PHRASE_BANK_PATH, encoding="utf-8") as fh:
        return json.load(fh)
