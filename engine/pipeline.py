"""
Daily pipeline orchestrator (TRD §1.7, FR-PK-59…64).

Run lifecycle per platform: boundary check → fetch → normalize → classify
→ dedupe/cluster → score → loophole update → expiry sweep → watermark
commit → audit entry. Idempotent end-to-end: re-running a day's job never
duplicates leads, scores, alerts, or reports (source-URL dedupe, SHR-15).
The kill switch stops all collection immediately (SHR-12).
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
import urllib.request

from config import Settings, load_phrase_bank, load_scoring_config
from engine import classifier as C
from engine.comparator import Comparator
from engine.loophole import detect_loopholes
from engine.scorer import score_lead
from models import audit, jdump, jload, new_id, utcnow_iso

log = logging.getLogger("paklead.pipeline")

VERTICAL_OF = {
    "seeking_service": "services", "seeking_product": "products",
    "job_seeking": "jobs", "hiring": "jobs",
    "property_wanted": "property", "property_offered": "property",
    "freelance_seeking": "freelance", "freelance_offering": "freelance",
    "selling": "products", "complaint_pain": "services",
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _budget_phrase_only(text: str, bank: dict) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in ("budget", "demand", "final", "price", "rate"))


class Pipeline:
    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self.s = settings
        self.conn = conn
        self.bank = load_phrase_bank()
        self.cfg = load_scoring_config()
        self.comparator = Comparator(self.cfg)
        self.alerts: list[dict] = []          # dashboard alert queue
        self.quarantine_count = 0

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------
    def get_sources(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM source WHERE enabled=1").fetchall()
        return [dict(r) for r in rows]

    def _adapter_for(self, source: dict):
        platform, route = source["platform"], source["access_route"]
        if platform == "reddit":
            from collectors.reddit_collector import RedditCollector
            return RedditCollector()
        if platform == "facebook":
            from collectors.facebook_adapter import FacebookAdapter
            if route == "browser_optin":
                return FacebookAdapter("browser_optin", self.s.FB_BROWSER_OPTIN)
            if route == "cli_adapter":
                return FacebookAdapter("cli_adapter")
            return FacebookAdapter("manual_import")
        from collectors.import_adapter import ImportAdapter
        return ImportAdapter(file_path=(source.get("boundary_note") or "").replace("file:", ""))

    # ------------------------------------------------------------------
    # Main entry points
    # ------------------------------------------------------------------
    def run_daily(self, demo_records: list[dict] | None = None) -> dict:
        """Full daily run. demo_records: synthetic raw records for --demo."""
        run_stamp = utcnow_iso()
        if self.s.KILL_SWITCH:
            audit(self.conn, "scheduler", "run_skipped_kill_switch",
                  detail={"at": run_stamp})
            log.warning("KILL_SWITCH=true — all collection skipped (SHR-12).")
            return {"status": "killed", "leads": 0, "alerts": []}

        summary = {"status": "ok", "runs": [], "leads": 0, "alerts": []}

        if demo_records is not None:
            self._process_records(demo_records, {
                "source_id": "demo-sample", "platform": "demo",
                "access_route": "manual_import", "city_scope": []}, run_stamp)
        else:
            for source in self.get_sources():
                summary["runs"].append(self._run_source(source, run_stamp))

        self._loophole_update(run_stamp)
        self._expiry_sweep(run_stamp)
        self.conn.commit()
        summary["alerts"] = self.alerts
        summary["leads"] = self.conn.execute("SELECT COUNT(*) c FROM lead").fetchone()["c"]
        audit(self.conn, "scheduler", "run_daily_complete",
              detail={"at": run_stamp, "sources": len(summary["runs"]),
                      "alerts": len(self.alerts)})
        return summary

    # ------------------------------------------------------------------
    def _run_source(self, source: dict, run_stamp: str) -> dict:
        run_id = new_id("run")
        started = utcnow_iso()
        self.conn.execute(
            "INSERT INTO collection_run (run_id, platform, source_id, access_route_used,"
            " started_at, status, kill_switch_state) VALUES (?,?,?,?,?,?,?)",
            (run_id, source["platform"], source["source_id"],
             source["access_route"], started, "ok", int(self.s.KILL_SWITCH)))
        audit(self.conn, "collector", "run_start", source["source_id"],
              {"run_id": run_id, "access_route": source["access_route"]})
        try:
            adapter = self._adapter_for(source)
            ctx = {"settings": self.s, "phrase_bank": self.bank,
                   "conn": self.conn, "audit": audit}
            result = adapter.collect(source, source.get("watermark"),
                                     source.get("rate_budget_per_min", 30) or 30, ctx)
            self._process_records(result["records"], source, run_stamp)
            self.conn.execute(
                "UPDATE collection_run SET ended_at=?, items_fetched=?, items_stored=?,"
                " rate_limit_events=?, status=?, error_class=? WHERE run_id=?",
                (utcnow_iso(), result["fetched"], len(result["records"]),
                 result["rate_limit_events"], result["status"], result["error_class"], run_id))
            if result["new_watermark"]:
                self.conn.execute("UPDATE source SET watermark=? WHERE source_id=?",
                                  (result["new_watermark"], source["source_id"]))
            self.conn.commit()
            return {"source": source["source_id"], "status": result["status"],
                    "fetched": result["fetched"], "stored": len(result["records"])}
        except Exception as exc:  # adapter bugs must not kill the day
            log.exception("Source %s failed", source["source_id"])
            self.conn.execute(
                "UPDATE collection_run SET ended_at=?, status='failed', error_class=?"
                " WHERE run_id=?", (utcnow_iso(), type(exc).__name__, run_id))
            audit(self.conn, "collector", "run_failed", source["source_id"],
                  {"error": str(exc)[:200]})
            self.conn.commit()
            return {"source": source["source_id"], "status": "failed"}

    # ------------------------------------------------------------------
    def _process_records(self, records: list[dict], source: dict, run_stamp: str) -> None:
        for rec in records:
            try:
                self._ingest_record(rec, source, run_stamp)
            except Exception:
                log.exception("Ingest failed for %s", rec.get("url"))
        self.conn.commit()

    def _ingest_record(self, rec: dict, source: dict, run_stamp: str) -> None:
        text = rec.get("text_original", "") or ""
        url = rec.get("url", "") or ""
        if not text or not url:
            return
        post_id = f"{rec.get('platform', 'unknown')}:{rec.get('platform_post_id', url)}"
        if self.conn.execute("SELECT 1 FROM post WHERE post_id=?",
                             (post_id,)).fetchone():
            return  # idempotent re-run (SHR-15)

        handle = rec.get("author_handle", "") or ""
        if self._suppressed(rec.get("platform", ""), handle, post_id, url):
            return

        source_cities = jload(source.get("city_scope"))
        cls = C.classify_post(text, self.bank, source_cities,
                              has_comments=bool(rec.get("comments")))
        quarantined = 0
        quarantine_reason = None
        if cls["scam_suspect"]:
            quarantined, quarantine_reason = 1, "scam_red_flag"
            self.quarantine_count += 1
        if cls["intent_primary"] == "complaint_pain":
            quarantined, quarantine_reason = 1, "complaint_review"
            self.quarantine_count += 1
        if cls["language_mode"] == "undetected":
            quarantined, quarantine_reason = 1, "undetected_language"

        seen = utcnow_iso()
        # Prefer the record's own source id (demo/import records carry theirs);
        # fall back to the collecting source (FR-PK-20 traceability).
        eff_source = rec.get("source_id") or source.get("source_id")
        self.conn.execute(
            "INSERT OR IGNORE INTO post (post_id, platform, source_id, author_handle,"
            " text_original, text_normalized, language_mode, city_tag, area_tag,"
            " intent_primary, intent_secondary, price_inquiry, budget_pkr,"
            " budget_qualifier, urgency_markers, contact_published, contact_type,"
            " url, published_at, seen_at, quarantined, quarantine_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (post_id, rec.get("platform"), eff_source, handle, text,
             cls["text_normalized"], cls["language_mode"], cls["city_tag"],
             cls["area_tag"], cls["intent_primary"], cls["intent_secondary"],
             int(cls["price_inquiry"]), cls["budget_pkr"], cls["budget_qualifier"],
             jdump(cls["urgency_markers"]), cls["contact_published"],
             cls["contact_type"], url, rec.get("published_at"), seen,
             quarantined, quarantine_reason))
        for c in rec.get("comments") or []:
            cid = f"{rec.get('platform')}:{c.get('platform_comment_id', '')}"
            cc = C.classify_comment(c.get("text", ""), self.bank)
            self.conn.execute(
                "INSERT OR IGNORE INTO comment (comment_id, post_id, author_handle,"
                " text, published_at, is_provider_reply, seen_at,"
                " intent_primary, contact_published)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, post_id, c.get("author_handle", ""), c.get("text", ""),
                 c.get("published_at"), int(cc["is_provider_reply"]),
                 seen, cc["intent_primary"], cc["contact_published"]))
        if quarantine_reason == "scam_red_flag":
            # Excluded from scoring, alerts, and exports until cleared (FR-PK-38)
            audit(self.conn, "system", "post_quarantined", post_id,
                  {"reason": quarantine_reason})
            self.conn.commit()
            return
        self._cluster_and_score(post_id, quarantined, run_stamp)

    # ------------------------------------------------------------------
    def _suppressed(self, platform: str, handle: str, post_id: str, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM suppression_entry WHERE platform=? AND target IN (?,?,?)",
            (platform, handle, post_id, url)).fetchone()
        return row is not None

    def _posts_of(self, merged_post_ids: list) -> list[dict]:
        """Fetch post rows for a lead's merged ids (empty-safe)."""
        if not merged_post_ids:
            return []
        return [dict(p) for p in self.conn.execute(
            "SELECT * FROM post WHERE post_id IN (%s)" %
            ",".join("?" * len(merged_post_ids)), merged_post_ids).fetchall()]

    def _existing_leads(self) -> list[dict]:
        leads = []
        for r in self.conn.execute("SELECT * FROM lead").fetchall():
            leads.append({"lead_id": r["lead_id"],
                          "merged_posts": self._posts_of(jload(r["merged_post_ids"])),
                          "city": r["city"], "intent": r["intent"],
                          "status": r["status"]})
        return leads

    def _cluster_and_score(self, post_id: str, quarantined: int, run_stamp: str) -> None:
        post = dict(self.conn.execute("SELECT * FROM post WHERE post_id=?",
                                      (post_id,)).fetchone())
        match, explanation = self.comparator.find_cluster(post, self._existing_leads())
        now = _now()

        if match:
            lead_id = match["lead_id"]
            merged = jload(self.conn.execute(
                "SELECT merged_post_ids FROM lead WHERE lead_id=?",
                (lead_id,)).fetchone()["merged_post_ids"])
            if post_id not in merged:
                merged.append(post_id)
            self.conn.execute(
                "UPDATE lead SET merged_post_ids=?, cluster_explanation=? WHERE lead_id=?",
                (jdump(merged), (match.get("cluster_explanation") or "") + " | " + explanation
                 if match.get("cluster_explanation") else explanation, lead_id))
        else:
            lead_id = new_id("lead")
            window = self.cfg["freshness_windows"][
                self.cfg["intent_window_family"].get(post["intent_primary"], "seeking")]
            self.conn.execute(
                "INSERT INTO lead (lead_id, merged_post_ids, intent, city, vertical,"
                " status, first_seen, expires_at, cluster_explanation)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (lead_id, jdump([post_id]), post["intent_primary"], post["city_tag"],
                 VERTICAL_OF.get(post["intent_primary"], "services"), "new", now.isoformat(),
                 (now + datetime.timedelta(days=window)).isoformat(), explanation))

        score = self._score_lead(lead_id, now, cleared_complaint=False)
        status = "new"
        if score["hot_flag"] and not quarantined:
            status = "hot"
            if score.get("new_hot"):
                self._emit_alert(lead_id, score)
        self.conn.execute("UPDATE lead SET status=? WHERE lead_id=?", (status, lead_id))

    # ------------------------------------------------------------------
    def _lead_features(self, lead_id: str) -> dict:
        lead = dict(self.conn.execute("SELECT * FROM lead WHERE lead_id=?",
                                      (lead_id,)).fetchone())
        posts = self._posts_of(jload(lead["merged_post_ids"]))
        cats = self.bank.get("categories", {})
        weight = float(cats.get(lead["intent"], {}).get("weight", 5))
        override = self.cfg.get("vertical_overrides", {}).get(lead["vertical"], {})
        if lead["intent"] in override:
            weight = float(override[lead["intent"]])
        markers: set[str] = set()
        platforms: set[str] = set()
        best_contact, best_phone = "none", None
        budget = None
        phrase_only = False
        first = min(p["seen_at"] for p in posts) if posts else utcnow_iso()
        price_inq = serious = replacement = False
        deadline = None
        for p in posts:
            markers.update(jload(p["urgency_markers"]))
            platforms.add(p["platform"])
            if p["budget_pkr"] and (budget is None or p["budget_pkr"] > budget):
                budget = p["budget_pkr"]
            elif _budget_phrase_only(p["text_original"], self.bank):
                phrase_only = True
            rank = {"phone": 4, "whatsapp": 4, "open_thread": 3, "inbox": 2, "none": 0}
            if rank.get(p["contact_type"], 0) > rank.get(best_contact, 0):
                best_contact, best_phone = p["contact_type"], p["contact_published"]
            if p["price_inquiry"]:
                price_inq = True
            if p["intent_primary"] == "complaint_pain" and jload(p["urgency_markers"]):
                replacement = True
            if lead["intent"] == "hiring" and re.search(
                    r"(kal|tomorrow|deadline|last date)", p["text_original"], re.I):
                deadline = True
        # Open-thread contactability: at least one non-provider comment means
        # the thread itself is a reachable channel. Provider replies are
        # sellers and must never count (SHR-safe). Comment phones are NOT
        # used for identity merge (posts only, TRD FR-PK-44).
        post_ids = [p["post_id"] for p in posts]
        has_open_thread = False
        if post_ids:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM comment WHERE post_id IN (%s)"
                " AND is_provider_reply = 0" %
                ",".join("?" * len(post_ids)), post_ids).fetchone()
            has_open_thread = bool(row and row["n"])
        rank = {"phone": 4, "whatsapp": 4, "open_thread": 3, "inbox": 2, "none": 0}
        if has_open_thread and rank.get(best_contact, 0) < 3:
            best_contact = "open_thread"  # activates contact_open_thread = 5
        return {
            "intent": lead["intent"], "category_weight": weight,
            "price_inquiry": price_inq, "first_seen": first,
            "budget_pkr": budget, "budget_phrase_matched": phrase_only and budget is None,
            "urgency_count": min(len(markers), self.cfg["urgency_marker_count_cap"]),
            "platform_count": len(platforms), "contact_type": best_contact,
            "serious_buyer": serious, "replacement_demand": replacement,
            "stated_deadline": deadline,
        }

    def _score_lead(self, lead_id: str, now: datetime.datetime,
                    cleared_complaint: bool = False) -> dict:
        feats = self._lead_features(lead_id)
        score = score_lead(feats, self.cfg, now, cleared_complaint=cleared_complaint)
        prev = self.conn.execute("SELECT * FROM lead_score WHERE lead_id=?",
                                 (lead_id,)).fetchone()
        history = jload(prev["score_history"]) if prev else []
        if prev:
            history.append({"computed_at": prev["computed_at"], "total": prev["total"],
                            "config_version": prev["scoring_config_version"]})
        self.conn.execute(
            "INSERT OR REPLACE INTO lead_score (lead_id, computed_at, scoring_config_version,"
            " intent_strength, recency, budget, urgency, cross_platform, contactability,"
            " total, hot_flag, hot_trigger, score_history)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lead_id, utcnow_iso(), score["scoring_config_version"],
             score["intent_strength"], score["recency"], score["budget"],
             score["urgency"], score["cross_platform"], score["contactability"],
             score["total"], int(score["hot_flag"]), score["hot_trigger"],
             jdump(history[-10:])))
        audit(self.conn, "scorer", "score_computed", lead_id,
              {"total": score["total"], "hot": score["hot_flag"],
               "config": score["scoring_config_version"]})
        # Alert de-dup (engine gap fix): a lead alerts only on the
        # not-hot -> hot transition, not on every re-score.
        score["new_hot"] = bool(score["hot_flag"]) and not (prev and prev["hot_flag"])
        return score

    # ------------------------------------------------------------------
    def _emit_alert(self, lead_id: str, score: dict) -> None:
        row = self.conn.execute("SELECT * FROM lead WHERE lead_id=?", (lead_id,)).fetchone()
        payload = {"lead_id": lead_id, "score": score["total"],
                   "breakdown": {k: score[k] for k in
                                 ("intent_strength", "recency", "budget", "urgency",
                                  "cross_platform", "contactability")},
                   "intent": row["intent"], "city": row["city"],
                   "urls": [p["url"] for p in self._posts_of(jload(row["merged_post_ids"]))]}
        self.alerts.append(payload)
        audit(self.conn, "system", "alert_queued", lead_id, {"score": score["total"]})
        if self.s.ALERT_WEBHOOK_URL:
            try:
                req = urllib.request.Request(
                    self.s.ALERT_WEBHOOK_URL,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=10)
                audit(self.conn, "system", "alert_sent", lead_id, {})
            except Exception as exc:
                audit(self.conn, "system", "alert_webhook_failed", lead_id,
                      {"error": str(exc)[:200]})

    # ------------------------------------------------------------------
    def _loophole_update(self, run_stamp: str) -> None:
        from engine import loophole as L

        leads = [dict(r) for r in self.conn.execute("SELECT * FROM lead").fetchall()]
        posts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM post WHERE is_deleted=0").fetchall()]
        comments = [dict(r) for r in self.conn.execute("SELECT * FROM comment").fetchall()]
        # expire stale loopholes (FR-PK-57)
        self.conn.execute("UPDATE loophole_report SET status='stale' WHERE status='active'"
                          " AND window_end < ?",
                          ((_now() - datetime.timedelta(
                              days=self.cfg["loophole_window_days"])).isoformat(),))
        for rep in detect_loopholes(leads, posts, comments, self.cfg):
            rep["report_id"] = new_id("looph")
            rep["created_at"] = utcnow_iso()
            rep["status"] = "active"
            self.conn.execute(
                "INSERT INTO loophole_report (report_id, report_type, window_start,"
                " window_end, vertical, city, seeker_count, provider_count, ratio,"
                " complaint_themes, price_gap_pct, evidence_post_ids, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rep["report_id"], rep["report_type"], rep["window_start"],
                 rep["window_end"], rep["vertical"], rep["city"],
                 rep["seeker_count"], rep["provider_count"], rep["ratio"],
                 json.dumps(rep.get("complaint_themes") or {}, ensure_ascii=False),
                 rep.get("price_gap_pct"), jdump(rep["evidence_post_ids"]),
                 rep["status"], rep["created_at"]))
            audit(self.conn, "system", "loophole_flagged", rep["report_id"],
                  {"type": rep["report_type"], "city": rep["city"],
                   "ratio": rep["ratio"]})
        self.conn.commit()

    # ------------------------------------------------------------------
    def _expiry_sweep(self, run_stamp: str) -> None:
        n = self.conn.execute(
            "UPDATE lead SET status='expired' WHERE status IN ('new','hot')"
            " AND expires_at < ?", (utcnow_iso(),)).rowcount
        if n:
            audit(self.conn, "scheduler", "expiry_sweep", None, {"expired": n})
        self.conn.commit()
