"""
Reddit collector — official Reddit API route only (TRD FR-PK-01…07).

Uses PRAW under the Reddit API Terms. Reads `new` posts from the watched
subreddit list and runs intent-keyword searches since the source watermark.
On any 429/rate-limit signal the run aborts for Reddit and backs off —
never rotates identities or tooling (SHR-10).

Missing dependency or missing credentials: logs a clear warning and
returns an empty result. Never crashes the pipeline.
"""

from __future__ import annotations

import logging
import time

from .base import CollectorAdapter, RawRecord, make_result

log = logging.getLogger("paklead.reddit")


def _search_terms(ctx: dict, limit: int = 25) -> list[str]:
    """Bounded intent-keyword list from the phrase bank (English phrases,
    distinctive words only). Bounded so the API is never hammered."""
    bank = ctx.get("phrase_bank") or {}
    words: list[str] = []
    seen = set()
    for cat, data in (bank.get("categories") or {}).items():
        for phrase in data.get("phrases_en", [])[:3]:
            for token in phrase.split():
                token = token.strip(".,!?").lower()
                if len(token) > 4 and token not in seen and token.isalpha():
                    seen.add(token)
                    words.append(token)
        if len(words) >= limit:
            break
    return words[:limit]


class RedditCollector(CollectorAdapter):
    platform = "reddit"
    access_route = "api"

    def collect(self, source: dict, since: str | None, budget_per_min: int,
                ctx: dict) -> CollectResult:
        try:
            import praw  # type: ignore
        except Exception:
            log.warning("PRAW not installed — Reddit source '%s' skipped "
                        "(pip install -r requirements.txt).", source.get("source_id"))
            return make_result(status="failed", error_class="dependency_missing")

        settings = ctx.get("settings")
        if settings is None or not settings.reddit_configured():
            log.warning("Reddit credentials missing — source '%s' skipped. "
                        "Set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / "
                        "REDDIT_USER_AGENT to enable live collection.",
                        source.get("source_id"))
            return make_result(status="failed", error_class="credentials_missing")

        subreddit_name = (source.get("display_name") or "").lstrip("r/") or "Pakistan"
        records: list[RawRecord] = []
        fetched = 0
        rate_limit_events = 0
        new_watermark = since

        try:
            reddit = praw.Reddit(
                client_id=settings.REDDIT_CLIENT_ID,
                client_secret=settings.REDDIT_CLIENT_SECRET,
                user_agent=settings.REDDIT_USER_AGENT,
            )
            sub = reddit.subreddit(subreddit_name)

            # 1) new posts since watermark
            for post in sub.new(limit=100):
                fetched += 1
                created = getattr(post, "created_utc", 0)
                if since and created <= float(since):
                    break
                records.append(self._to_record(post, source, ctx))
                new_watermark = str(int(created))

            # 2) keyword searches (bounded)
            for term in _search_terms(ctx):
                for post in sub.search(term, sort="new", limit=25):
                    fetched += 1
                    if any(r["platform_post_id"] == post.id for r in records):
                        continue
                    records.append(self._to_record(post, source, ctx))
                time.sleep(60.0 / max(budget_per_min, 1))

        except Exception as exc:  # pragma: no cover - live-network path
            name = type(exc).__name__
            if "TooManyRequests" in name or "429" in name:
                rate_limit_events += 1
                log.warning("Reddit 429 — aborting this platform's run, backing off.")
                return make_result(records, fetched, rate_limit_events,
                                   "aborted_rate_limit", "rate_limited",
                                   new_watermark)
            log.error("Reddit collection failed for %s: %s", source.get("source_id"), exc)
            return make_result(records, fetched, rate_limit_events, "failed",
                               f"reddit:{name}", new_watermark)

        return make_result(records, fetched, rate_limit_events, "ok", None,
                           new_watermark)

    def _to_record(self, post, source: dict, ctx: dict) -> RawRecord:
        permalink = getattr(post, "permalink", "") or ""
        comments: list[dict] = []
        try:  # top-level comments for provider-reply detection (FR-PK-04)
            post.comments.replace_more(limit=0)
            for c in post.comments.list()[:20]:
                comments.append({
                    "platform_comment_id": getattr(c, "id", ""),
                    "author_handle": str(getattr(c, "author", "") or ""),
                    "text": getattr(c, "body", "") or "",
                    "published_at": None,
                })
        except Exception:
            pass
        return RawRecord(
            platform="reddit",
            platform_post_id=getattr(post, "id", ""),
            source_id=source.get("source_id"),
            author_handle=str(getattr(post, "author", "") or ""),
            text_original=f"{getattr(post, 'title', '')}\n{getattr(post, 'selftext', '')}".strip(),
            url=f"https://www.reddit.com{permalink}" if permalink else "",
            published_at=None,
            access_route_used=self.access_route,
            comments=comments,
        )
