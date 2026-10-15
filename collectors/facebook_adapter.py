"""
Facebook connector — one interface, three interchangeable backends
(TRD FR-PK-08…15). Selected by FB_ACCESS_MODE:

  (a) cli_adapter   — Meta-approved surfaces: the platform's authenticated
                      group-reading tooling (here: the `facebook-cli` tool)
                      for groups the operator legitimately belongs to.
  (b) browser_optin — operator's OWN logged-in session, read-only, enabled
                      only with FB_BROWSER_OPTIN=true. This backend contains
                      NO automation code: in this deployment it raises
                      NotImplementedError and documents that browser work is
                      executed by the operator's live browser session tasks,
                      never by scrapers (SHR-1, TRD FR-S2).
  (c) manual_import — operator reads groups manually, imports CSV/JSON.
                      Always available, zero platform risk.

Backends may scroll/read feeds, open posts, and expand comments (b only
when executed by the live session). No backend may post, comment, react,
message, or join/leave groups (SHR-14). Evasion tooling, session-cookie replay, headless
bot farms and identity rotation are prohibited by design and are simply
not written (SHR-1, SHR never #1).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from .base import CollectorAdapter, RawRecord, make_result

log = logging.getLogger("paklead.facebook")

BROWSER_OPTIN_WARNING = (
    "WARNING (TRD FR-PK-08b, FR-R3, pk_legal_boundaries SHR matrix row 2): the "
    "Facebook browser-session route automates the operator's OWN logged-in "
    "session. Meta may flag automated behaviour and the account can face "
    "restrictions or a ban. Read-only, rate-limited, stops on any login "
    "challenge/CAPTCHA/checkpoint — the operator accepts this risk by setting "
    "FB_BROWSER_OPTIN=true. The zero-risk alternative is adapter (c) manual "
    "import."
)


class FacebookAdapter(CollectorAdapter):
    platform = "facebook"

    def __init__(self, mode: str = "manual_import", browser_optin: bool = False):
        self.mode = mode
        self.browser_optin = browser_optin
        self.access_route = {
            "cli_adapter": "cli_adapter",
            "api": "api",
            "browser_optin": "browser_optin",
            "manual_import": "manual_import",
        }.get(mode, "manual_import")

    # ------------------------------------------------------------------
    def collect(self, source: dict, since: str | None, budget_per_min: int,
                ctx: dict) -> CollectResult:
        if self.mode == "browser_optin":
            return self._browser_backend(source, ctx)
        if self.mode in ("cli_adapter", "api"):
            return self._cli_backend(source, since, ctx)
        return self._import_backend(source, since, ctx)

    # ------------------------------------------------------------------
    def _cli_backend(self, source: dict, since: str | None, ctx: dict) -> CollectResult:
        """Backend (a): the environment's authenticated group-reading tool."""
        if shutil.which("facebook-cli") is None:
            log.warning("facebook-cli not found — Facebook source '%s' skipped.",
                        source.get("source_id"))
            return make_result(status="failed", error_class="dependency_missing")
        group_id = source.get("display_name") or source.get("source_id")
        cmd = ["facebook-cli", "groups", "posts", "--group-id", str(group_id),
               "--limit", "50"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as exc:
            log.error("facebook-cli failed: %s", exc)
            return make_result(status="failed", error_class="cli_error")
        records: list[RawRecord] = []
        try:
            data = json.loads(out.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        for item in data.get("data", []) if isinstance(data, dict) else []:
            pid = str(item.get("id") or item.get("post_id") or "")
            if not pid:
                continue
            records.append(RawRecord(
                platform="facebook",
                platform_post_id=pid,
                source_id=source.get("source_id"),
                author_handle=item.get("author") or item.get("author_name") or "",
                text_original=item.get("text") or item.get("message") or "",
                url=item.get("url") or item.get("permalink_url") or "",
                published_at=item.get("created_time") or item.get("published_at"),
                access_route_used=self.access_route,
                comments=[],
            ))
        return make_result(records, fetched=len(records), status="ok")

    # ------------------------------------------------------------------
    def _browser_backend(self, source: dict, ctx: dict) -> CollectResult:
        """Backend (b): operator's own live session. Contains NO automation.

        Any browser work for this source is executed by the operator's live
        browser session tasks (read-only feed scrolling, opening posts,
        expanding comments) — never by code in this repository. This stub
        exists so the access decision is audit-logged per AKU-PK-18.
        """
        log.warning(BROWSER_OPTIN_WARNING)
        if not self.browser_optin:
            return make_result(status="aborted_boundary",
                               error_class="browser_optin_not_set")
        raise NotImplementedError(
            "Backend (b) 'browser_optin' deliberately contains no automation "
            "code (SHR-1: no evasion tooling, no session-cookie replay). "
            "Reading groups through the operator's own logged-in browser is "
            "executed by the operator's live browser session tasks, read-only "
            "and rate-limited (<= 1 read action per 5 seconds, halt on any "
            "challenge/CAPTCHA/checkpoint per TRD FR-R4). The operator imports "
            "the observed posts through adapter (c) manual_import, or uses "
            "adapter (a) cli_adapter. This access decision is audit-logged."
        )

    # ------------------------------------------------------------------
    def _import_backend(self, source: dict, since: str | None,
                        ctx: dict) -> CollectResult:
        """Backend (c): CSV/JSON import. Always available, zero platform risk."""
        import os

        from .import_adapter import ImportAdapter

        # Per-source import file first: boundary_note "file:<path>"; falls back
        # to the global settings FB_IMPORT_FILE, then the env var (TRD FR-PK-16).
        note = (source.get("boundary_note") or "")
        path = (note[len("file:"):] if note.startswith("file:")
                else (ctx.get("settings") and
                      getattr(ctx["settings"], "FB_IMPORT_FILE", None))) or \
               os.environ.get("FB_IMPORT_FILE", "")
        adapter = ImportAdapter(file_path=path)
        return adapter.collect(source, since,
                               int(ctx.get("budget_per_min", 30) or 30), ctx)
