"""
Collector adapter interface.

Every adapter implements one method and every record it produces carries
`access_route_used` (TRD FR-PK-20) so the audit log proves which legitimate
route produced each item. Adapters never write posts, comments, or messages
to any platform — collection is read-only by design (SHR-14).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# Allowed access routes (TRD §1 access matrix). Nothing else may appear.
ACCESS_ROUTES = ("api", "cli_adapter", "browser_optin", "manual_import", "public_page")


class RawRecord(dict):
    """A raw platform record before normalization. Keys:

    platform, platform_post_id, source_id, author_handle, text_original,
    url, published_at (ISO-8601 or None), access_route_used, comments
    (list of dicts: platform_comment_id, author_handle, text,
    published_at), boundary_note (optional).
    """


class CollectResult(dict):
    """records, fetched, rate_limit_events, status, error_class,
    new_watermark — plain dict subclass for convenience."""


class CollectorAdapter(ABC):
    """Base class for all platform collectors."""

    platform: str = "unknown"
    access_route: str = "manual_import"

    @abstractmethod
    def collect(self, source: dict, since: str | None, budget_per_min: int,
                ctx: dict) -> CollectResult:
        """Fetch new items from `source` since the `since` watermark.

        ctx carries: conn (sqlite), audit callback, phrase-bank helpers,
        and any runtime flags. Must never raise on auth/dependency
        problems — return a failed CollectResult instead, so the pipeline
        can log the CollectionRun row and continue with other sources.
        """
        raise NotImplementedError


def make_result(records=None, fetched=0, rate_limit_events=0, status="ok",
                error_class=None, new_watermark=None) -> CollectResult:
    return CollectResult(
        records=records or [],
        fetched=fetched,
        rate_limit_events=rate_limit_events,
        status=status,
        error_class=error_class,
        new_watermark=new_watermark,
    )
