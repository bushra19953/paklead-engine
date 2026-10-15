#!/usr/bin/env python3
"""
PakLead Engine daily CLI.

    python run_daily.py --demo     # run the pipeline on fictional sample data
    python run_daily.py            # run configured collectors (needs credentials)

Exit code 0 on a completed run (even with zero leads); 2 when the kill
switch is on; 1 on an unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from config import Settings  # noqa: E402
from engine.pipeline import Pipeline  # noqa: E402
from models import audit, connect, init_db, utcnow_iso  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

DEFAULT_SOURCES = [
    # (source_id, platform, access_route, display_name, archetype, cities)
    ("reddit-r-pakistan", "reddit", "api", "r/Pakistan", "community", ["Karachi", "Lahore", "Islamabad"]),
    ("reddit-r-karachi", "reddit", "api", "r/karachi", "community", ["Karachi"]),
    ("reddit-r-lahore", "reddit", "api", "r/Lahore", "community", ["Lahore"]),
    ("reddit-r-islamabad", "reddit", "api", "r/islamabad", "community", ["Islamabad"]),
    ("fb-manual", "facebook", "manual_import", "Manual import",
     "general", []),
]


def ensure_sources(conn) -> None:
    """Seed the default source registry if the Source table is empty."""
    if conn.execute("SELECT COUNT(*) c FROM source").fetchone()["c"]:
        return
    for sid, platform, route, name, archetype, cities in DEFAULT_SOURCES:
        conn.execute(
            "INSERT INTO source (source_id, platform, access_route, display_name,"
            " archetype, city_scope, rate_budget_per_min, enabled, boundary_status,"
            " boundary_note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, platform, route, name, archetype, json.dumps(cities), 30, 1,
             "allowed", "TRD access matrix: official API / manual import",
             utcnow_iso()))
    audit(conn, "system", "sources_seeded", None,
          {"count": len(DEFAULT_SOURCES)})
    conn.commit()


def load_demo_records() -> list[dict]:
    data_path = pathlib.Path(__file__).resolve().parent / "sample_data" / "sample_posts.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    records = []
    for p in data["posts"]:
        records.append({
            "platform": p["platform"],
            "platform_post_id": p["platform_post_id"],
            "source_id": p["source_id"],
            "author_handle": p["author_handle"],
            "text_original": p["text_original"],
            "url": p["url"],
            "published_at": p.get("published_at"),
            "access_route_used": "manual_import",
            "comments": [],
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="PakLead Engine daily run")
    parser.add_argument("--demo", action="store_true",
                        help="run on fictional sample data instead of live collectors")
    parser.add_argument("--db", default=None, help="override DB_PATH")
    args = parser.parse_args()

    settings = Settings()
    if args.db:
        settings.DB_PATH = args.db
    os.makedirs(os.path.dirname(settings.DB_PATH) or ".", exist_ok=True)

    conn = connect(settings.DB_PATH)
    init_db(conn)
    ensure_sources(conn)

    pipeline = Pipeline(settings, conn)
    if args.demo:
        records = load_demo_records()
        _demo_city = {"karachi": "Karachi", "khi": "Karachi",
                      "lahore": "Lahore", "lhr": "Lahore",
                      "islamabad": "Islamabad", "isl": "Islamabad"}
        for p in records:
            sid = p["source_id"]
            if not conn.execute("SELECT 1 FROM source WHERE source_id=?",
                                (sid,)).fetchone():
                cities = [c for k, c in _demo_city.items() if k in sid.lower()]
                conn.execute(
                    "INSERT INTO source (source_id, platform, access_route,"
                    " display_name, archetype, city_scope, rate_budget_per_min,"
                    " enabled, boundary_status, boundary_note, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, p["platform"], "manual_import",
                     f"Demo source {sid}", "general", json.dumps(cities), 30, 1,
                     "allowed", "demo: fictional sample data", utcnow_iso()))
        conn.commit()
        summary = pipeline.run_daily(demo_records=records)
    else:
        missing = settings.missing_required_for_live()
        if missing:
            logging.warning("Live collection limited — missing: %s", "; ".join(missing))
        summary = pipeline.run_daily()

    print(json.dumps({
        "at": utcnow_iso(),
        "status": summary.get("status"),
        "sources": len(summary.get("runs", [])),
        "leads_total": summary.get("leads"),
        "alerts": [{"lead_id": a["lead_id"], "score": a["score"],
                    "intent": a["intent"], "city": a["city"]} for a in summary.get("alerts", [])],
    }, ensure_ascii=False, indent=2))

    if summary.get("status") == "killed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
