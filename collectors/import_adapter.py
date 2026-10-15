"""
Manual import adapter (TRD FR-PK-16, FR-PK-19).

The zero-platform-risk route for Instagram / X / LinkedIn / OLX / PakWheels
exports and any other manually collected material: the operator reads the
platform in their own browser and saves rows to CSV/JSON, which this
adapter validates and imports.

Accepted schema (CSV header or JSON objects):
  platform, platform_post_id, author_handle, text_original, url,
  published_at (ISO-8601, optional), source_id (optional)

Validation: rows missing `text_original` or `url` are rejected and logged;
rows matching scam red-flag patterns are quarantined for human review
(TRD FR-PK-38) instead of being silently stored or discarded.
"""

from __future__ import annotations

import csv
import json
import logging
import os

from .base import CollectorAdapter, RawRecord, make_result

log = logging.getLogger("paklead.import")


class ImportAdapter(CollectorAdapter):
    platform = "manual"
    access_route = "manual_import"

    def __init__(self, file_path: str = "", platform_default: str = "manual"):
        self.file_path = file_path
        self.platform_default = platform_default

    # ------------------------------------------------------------------
    def collect(self, source: dict, since: str | None, budget_per_min: int,
                ctx: dict) -> CollectResult:
        if not self.file_path or not os.path.exists(self.file_path):
            log.warning("Import file '%s' not found — source '%s' skipped.",
                        self.file_path, source.get("source_id"))
            return make_result(status="failed", error_class="import_file_missing")

        from engine.classifier import is_scam_suspect  # local import: engine is stdlib-only

        records: list[RawRecord] = []
        rejected = 0
        try:
            rows = self._read_rows()
        except Exception as exc:
            log.error("Import parse failed: %s", exc)
            return make_result(status="failed", error_class="import_parse_error")

        for row in rows:
            text = (row.get("text_original") or row.get("text") or "").strip()
            url = (row.get("url") or "").strip()
            if not text or not url:
                rejected += 1
                continue
            platform = (row.get("platform") or source.get("platform")
                        or self.platform_default).strip()
            if is_scam_suspect(text):
                ctx.setdefault("quarantined_imports", []).append(
                    {"url": url, "reason": "scam_red_flag",
                     "access_route_used": self.access_route})
                continue  # quarantined, not stored (TRD FR-PK-38)
            pid = str(row.get("platform_post_id") or row.get("id") or url)
            records.append(RawRecord(
                platform=platform,
                platform_post_id=pid,
                source_id=row.get("source_id") or source.get("source_id"),
                author_handle=row.get("author_handle") or row.get("author") or "",
                text_original=text,
                url=url,
                published_at=row.get("published_at"),
                access_route_used=self.access_route,
                comments=[],
            ))
        if rejected:
            log.warning("Import: %d rows rejected (missing text/url).", rejected)
        return make_result(records, fetched=len(rows), status="ok")

    # ------------------------------------------------------------------
    def _read_rows(self) -> list[dict]:
        if self.file_path.endswith(".json"):
            with open(self.file_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data = data.get("posts", data.get("rows", []))
            return list(data)
        with open(self.file_path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
