"""Batched scoring pass for the PakLead Engine.

Drop-in accelerator over :mod:`engine.scorer`. ``score_lead`` resolves the
scoring config (and several config values) on *every* call; when the
pipeline scores thousands of leads per sweep, that per-lead repetition is
pure overhead. This module resolves the config ONCE into a
:class:`_ScoringContext` and then scores the whole batch against it.

Acceptance guarantee: every score produced here is IDENTICAL (bit for bit)
to calling :func:`engine.scorer.score_lead` on the same lead with the same
``cfg``/``now``/``cleared_complaint`` arguments. The per-lead algorithm is
a faithful port of ``score_lead`` — including the ``int()``/``round()``
truncation points and the hot-trigger cascade — but reads every repeated
config lookup from the cached context. Parity is covered by the
verification script in ``~/workspace/paklead_ops/fleet/b18_scoring_batch.md``.

Only the standard library is used. Nothing here touches the database; the
pipeline decides what to persist.

=====================================================================
INTEGRATION CONTRACT
=====================================================================

1. BATCH SCORING (replaces the per-lead loop after clustering)
---------------------------------------------------------------------
In ``engine/pipeline.py`` the natural insertion point is right after the
clustering step has produced its merged leads (i.e. wherever the code
currently does::

    for lead_id in merged_lead_ids:
        feats = self._lead_features(lead_id)
        score = score_lead(feats, self.cfg, now, cleared_complaint=False)

replace it with a single batched call::

    from engine.scoring_batch import score_leads_batch
    feats_list = [self._lead_features(lid) for lid in merged_lead_ids]
    scores = score_leads_batch(feats_list, self.cfg, now=now)
    for lead_id, score in zip(merged_lead_ids, scores):
        ...persist score exactly as before...

The returned list is aligned 1:1 with the input list, so the existing
``lead_score`` INSERT logic (including ``score_history`` handling) stays
untouched. ``now`` should be the sweep's frozen timestamp — the batch
computes one ``now`` for the whole batch when it is not supplied, which
also removes the microsecond drift that per-lead ``datetime.now()`` calls
can introduce mid-sweep.

Per-lead ``cleared_complaint`` flags (e.g. leads that went through
quarantine review) are supported via a mapping::

    scores = score_leads_batch(feats_list, cfg,
                               cleared_complaint={lid: True for lid in cleared})

2. DELTA SCORING (periodic re-scoring runs)
---------------------------------------------------------------------
Re-scoring every lead on every sweep is wasteful when only a handful of
leads changed (new merged post, quarantine clearance, config bump). The
periodic re-scoring job should:

  a. load the previous score records into ``{lead_id: score_dict}``;
  b. build the feature dicts only for leads whose inputs changed;
  c. call ``delta_scoring(prev_scores, changed_feats, changed_ids, cfg, now)``;
  d. persist only the returned ``changed_scores`` entries (the merged
     mapping contains everything for downstream use).

``delta_scoring`` re-scores ONLY the ``changed_ids`` subset and returns a
new merged mapping (inputs are never mutated) — proving in the
verification below that untouched leads keep byte-identical scores and
that non-requested changed candidates are never re-scored.

3. DRY-RUN MODE
---------------------------------------------------------------------
Pass ``dry_run=True`` to ``delta_scoring`` to preview a re-scoring run
without mutating anything: it returns a report dict with
``n_would_change_ge5`` (how many leads would move by >= 5 points) and a
``delta_distribution`` histogram of score deltas. Use it in the cron job
before persisting, or from an operator console to sanity-check a config
weight change.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, Iterable, List, Mapping, Optional, Union

from config import load_scoring_config

log = logging.getLogger(__name__)

__all__ = [
    "score_leads_batch",
    "delta_scoring",
    "dry_run_report",
    "DELTA_BINS",
]

# ---------------------------------------------------------------------------
# Scoring context: every config value score_lead looks up more than once is
# resolved here exactly once per batch.
# ---------------------------------------------------------------------------

# Contactability lookup is rebuilt in score_lead for every lead; the batch
# resolves it once. The "none"/unknown default of 0 is preserved.
_CONTACT_DEFAULT = 0


class _ScoringContext:
    """All repeated config lookups for one batch, resolved a single time."""

    __slots__ = (
        "version", "intent_multiplier", "price_inquiry_bonus",
        "complaint_replacement_bonus", "serious_buyer_bonus",
        "recency_max", "recency_full_days",
        "budget_parsed", "budget_phrase_only",
        "urgency_cap", "urgency_per_marker",
        "cross_platform_three_plus", "cross_platform_two",
        "contactability", "hot_threshold",
        "_family", "_windows",
    )

    def __init__(self, cfg: Mapping) -> None:
        self.version = cfg["version"]
        self.intent_multiplier = cfg["intent_multiplier"]
        self.price_inquiry_bonus = cfg["price_inquiry_bonus"]
        self.complaint_replacement_bonus = cfg["complaint_replacement_bonus"]
        self.serious_buyer_bonus = cfg["serious_buyer_bonus"]
        self.recency_max = cfg["recency_max"]
        # score_lead computes cfg["recency_full_hours"] / 24.0 per lead.
        self.recency_full_days = cfg["recency_full_hours"] / 24.0
        self.budget_parsed = cfg["budget_parsed"]
        self.budget_phrase_only = cfg["budget_phrase_only"]
        self.urgency_cap = cfg["urgency_cap"]
        self.urgency_per_marker = cfg["urgency_per_marker"]
        self.cross_platform_three_plus = cfg["cross_platform_three_plus"]
        self.cross_platform_two = cfg["cross_platform_two"]
        self.contactability = {
            "phone": cfg["contact_phone_published"],
            "whatsapp": cfg["contact_phone_published"],
            "open_thread": cfg["contact_open_thread"],
            "inbox": cfg["contact_inbox_gate"],
        }
        self.hot_threshold = cfg["hot_threshold"]
        # score_lead calls _family_of/_freshness_days per lead, which do two
        # dict lookups each. Cache per distinct intent string.
        self._family: Dict[str, str] = {}
        self._windows: Dict[str, int] = {}

    def family_and_window(self, intent: str, cfg: Mapping) -> tuple[str, int]:
        """Intent family + freshness window, cached per distinct intent."""
        window = self._windows.get(intent)
        if window is None:
            family = cfg["intent_window_family"].get(intent, "seeking")
            window = cfg["freshness_windows"][family]
            self._family[intent] = family
            self._windows[intent] = window
        return self._family[intent], window


def _parse_first_seen(value) -> datetime.datetime:
    """Same coercion as score_lead: ISO string or datetime passes through."""
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    return value


def _score_one(lead: Mapping, ctx: _ScoringContext,
               now: datetime.datetime, cfg: Mapping,
               cleared_complaint: bool = False) -> Dict:
    """Score a single lead against a pre-resolved context.

    Faithful port of :func:`engine.scorer.score_lead` — same inputs, same
    truncation points (``int()``/``round()``), same hot-trigger cascade —
    so the output is identical to the reference implementation.
    """
    # --- 1. intent_strength ---
    weight = float(lead.get("category_weight", 5))
    intent_strength = int(weight * ctx.intent_multiplier)
    if lead.get("price_inquiry"):
        intent_strength += ctx.price_inquiry_bonus
    intent = lead.get("intent", "")
    if intent == "complaint_pain" and cleared_complaint \
            and lead.get("replacement_demand"):
        intent_strength += ctx.complaint_replacement_bonus
    if lead.get("serious_buyer"):
        intent_strength += ctx.serious_buyer_bonus

    # --- 2. recency ---
    first_seen = _parse_first_seen(lead.get("first_seen"))
    age_days = max(0.0, (now - first_seen).total_seconds() / 86400.0)
    _, window = ctx.family_and_window(intent, cfg)
    full_d = ctx.recency_full_days
    if age_days <= full_d:
        recency = ctx.recency_max
    elif age_days >= window:
        recency = 0
    else:
        recency = int(round(ctx.recency_max * (1 - (age_days - full_d) / (window - full_d))))

    # --- 3. budget ---
    if lead.get("budget_pkr"):
        budget = ctx.budget_parsed
    elif lead.get("budget_phrase_matched"):
        budget = ctx.budget_phrase_only
    else:
        budget = 0

    # --- 4. urgency ---
    urgency = min(ctx.urgency_cap,
                  lead.get("urgency_count", 0) * ctx.urgency_per_marker)

    # --- 5. cross_platform ---
    nplat = lead.get("platform_count", 1)
    cross_platform = (ctx.cross_platform_three_plus if nplat >= 3
                      else ctx.cross_platform_two if nplat == 2 else 0)

    # --- 6. contactability ---
    contactability = ctx.contactability.get(
        lead.get("contact_type", "none"), _CONTACT_DEFAULT)

    total = intent_strength + recency + budget + int(urgency) + cross_platform + contactability
    total = max(0, min(100, total))

    # --- hot triggers (same cascade/order as score_lead) ---
    hot_trigger = None
    if total >= ctx.hot_threshold:
        hot_trigger = "score_threshold"
    if (intent == "complaint_pain" and cleared_complaint
            and lead.get("replacement_demand")):
        hot_trigger = "complaint_with_replacement_urgency"
    if intent == "hiring" and lead.get("stated_deadline"):
        hot_trigger = "hiring_with_deadline"
    if intent.startswith("seeking") and urgency >= ctx.urgency_cap \
            and age_days <= 1.0:
        hot_trigger = "same_day_urgency_seeking"

    return {
        "scoring_config_version": ctx.version,
        "intent_strength": int(intent_strength),
        "recency": int(recency),
        "budget": int(budget),
        "urgency": int(urgency),
        "cross_platform": int(cross_platform),
        "contactability": int(contactability),
        "total": int(total),
        "hot_flag": hot_trigger is not None,
        "hot_trigger": hot_trigger,
        "expires_at": (first_seen + datetime.timedelta(days=window)).isoformat(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_leads_batch(
    leads: List[Mapping],
    cfg: Optional[Mapping] = None,
    now: Optional[datetime.datetime] = None,
    cleared_complaint: Union[bool, Mapping[str, bool]] = False,
) -> List[Dict]:
    """Score a whole batch of leads with one config resolution.

    Args:
        leads: feature dicts in the ``score_lead`` format
            (``intent``, ``category_weight``, ``price_inquiry``,
            ``first_seen``, ``budget_pkr``, ``budget_phrase_matched``,
            ``urgency_count``, ``platform_count``, ``contact_type``,
            ``serious_buyer``, ``replacement_demand``, ``stated_deadline``).
        cfg: scoring config; loaded once via ``load_scoring_config()`` when
            omitted (the per-lead ``cfg or load_scoring_config()`` call in
            ``score_lead`` is the biggest saving — one load, not N).
        now: frozen sweep timestamp; defaults to a single
            ``datetime.now(timezone.utc)`` shared by the whole batch.
        cleared_complaint: ``False`` for the whole batch, ``True`` to treat
            every lead as quarantine-cleared, or a ``{lead_id: bool}``
            mapping (uses each lead's ``lead_id`` key when present,
            otherwise the batch index) for per-lead flags.

    Returns:
        List of score dicts aligned 1:1 with ``leads``. Every element is
        identical to ``score_lead(lead, cfg, now, cleared_complaint=...)``.
    """
    cfg = cfg or load_scoring_config()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ctx = _ScoringContext(cfg)

    if isinstance(cleared_complaint, Mapping):
        flags = cleared_complaint
        results: List[Dict] = []
        for idx, lead in enumerate(leads):
            key = lead.get("lead_id", idx)
            results.append(_score_one(lead, ctx, now, cfg,
                                      cleared_complaint=bool(flags.get(key, False))))
        return results

    flag = bool(cleared_complaint)
    return [_score_one(lead, ctx, now, cfg, cleared_complaint=flag)
            for lead in leads]


# Bins for the dry-run delta distribution report. Each bin is
# (label, lo, hi) inclusive; deltas are new_total - old_total.
DELTA_BINS = [
    ("<=-10", None, -10),
    ("-9..-5", -9, -5),
    ("-4..-1", -4, -1),
    ("0", 0, 0),
    ("1..4", 1, 4),
    ("5..9", 5, 9),
    (">=10", 10, None),
]


def _bin_delta(delta: int) -> str:
    """Assign a score delta to its distribution bin label."""
    for label, lo, hi in DELTA_BINS:
        if lo is not None and delta < lo:
            continue
        if hi is not None and delta > hi:
            continue
        return label
    return "?"  # unreachable


def dry_run_report(
    scored: Mapping[str, Mapping],
    leads: Mapping[str, Mapping],
    changed_ids: Iterable[str],
    cfg: Optional[Mapping] = None,
    now: Optional[datetime.datetime] = None,
    cleared_complaint: Union[bool, Mapping[str, bool]] = False,
    threshold: int = 5,
) -> Dict:
    """Preview a re-scoring run WITHOUT mutating anything.

    Re-scores only ``changed_ids`` (must exist in both ``scored`` and
    ``leads``), then reports how the re-score would move totals.

    Args:
        scored: previous score records keyed by lead_id
            (``{"total": ...}`` at minimum).
        leads: current feature dicts keyed by lead_id.
        changed_ids: lead_ids whose inputs changed and need re-scoring.
        threshold: "would change" means ``abs(new - old) >= threshold``.

    Returns:
        Report dict with ``n_re_scored`` (actually re-scored),
        ``n_would_change_ge<TH>`` (leads moving by >= threshold),
        ``delta_distribution`` (bin label -> count over re-scored leads),
        and per-lead ``details`` ``[{lead_id, old_total, new_total, delta}]``.
        Nothing is written anywhere; inputs are not mutated.
    """
    cfg = cfg or load_scoring_config()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ctx = _ScoringContext(cfg)
    key = f"n_would_change_ge{threshold}"

    details: List[Dict] = []
    distribution: Dict[str, int] = {label: 0 for label, _, _ in DELTA_BINS}
    n_re_scored = 0

    if isinstance(cleared_complaint, Mapping):
        def _flag(lid: str) -> bool:
            return bool(cleared_complaint.get(lid, False))
    else:
        def _flag(lid: str) -> bool:  # noqa: ARG001
            return bool(cleared_complaint)

    for lid in changed_ids:
        if lid not in leads:
            log.warning("dry_run: changed lead_id %r not in leads; skipped", lid)
            continue
        if lid not in scored:
            log.warning("dry_run: lead_id %r has no previous score; skipped", lid)
            continue
        new = _score_one(leads[lid], ctx, now, cfg, cleared_complaint=_flag(lid))
        old_total = int(scored[lid]["total"])
        new_total = int(new["total"])
        delta = new_total - old_total
        details.append({"lead_id": lid, "old_total": old_total,
                        "new_total": new_total, "delta": delta})
        distribution[_bin_delta(delta)] += 1
        n_re_scored += 1

    return {
        "n_re_scored": n_re_scored,
        "n_changed_ids_requested": len(list(changed_ids)) if not isinstance(changed_ids, (set, frozenset, list, tuple)) else len(changed_ids),
        key: sum(1 for d in details if abs(d["delta"]) >= threshold),
        "delta_distribution": distribution,
        "details": details,
    }


def delta_scoring(
    scored: Mapping[str, Mapping],
    leads: Mapping[str, Mapping],
    changed_ids: Iterable[str],
    cfg: Optional[Mapping] = None,
    now: Optional[datetime.datetime] = None,
    cleared_complaint: Union[bool, Mapping[str, bool]] = False,
    dry_run: bool = False,
    threshold: int = 5,
) -> Union[Dict[str, Dict], Dict]:
    """Re-score only the changed leads and merge with previous scores.

    The delta pass for periodic re-scoring runs: ``changed_ids`` are the
    leads whose inputs changed since the last scoring pass (new merged
    post, quarantine clearance, config bump, ...). Only those leads are
    re-scored; everything else keeps its previous score object untouched.

    Args:
        scored: previous score records keyed by lead_id.
        leads: current feature dicts keyed by lead_id.
        changed_ids: lead_ids to re-score. IDs missing from ``leads`` or
            ``scored`` are skipped with a warning, never an exception.
        cfg / now / cleared_complaint: as in :func:`score_leads_batch`.
        dry_run: when True, return the :func:`dry_run_report` dict instead
            of merged scores (no scoring state is produced or written).
        threshold: delta threshold used by the dry-run report.

    Returns:
        ``dry_run=False``: a NEW dict ``{lead_id: score_dict}`` containing
        every entry of ``scored`` plus freshly re-scored entries for the
        valid ``changed_ids``. Neither input mapping is mutated.
        ``dry_run=True``: the dry-run report dict (see
        :func:`dry_run_report`).
    """
    if dry_run:
        return dry_run_report(scored, leads, changed_ids, cfg, now,
                              cleared_complaint, threshold)

    cfg = cfg or load_scoring_config()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ctx = _ScoringContext(cfg)

    if isinstance(cleared_complaint, Mapping):
        def _flag(lid: str) -> bool:
            return bool(cleared_complaint.get(lid, False))
    else:
        def _flag(lid: str) -> bool:  # noqa: ARG001
            return bool(cleared_complaint)

    merged: Dict[str, Dict] = dict(scored)
    n_re_scored = 0
    for lid in changed_ids:
        if lid not in leads:
            log.warning("delta: changed lead_id %r not in leads; skipped", lid)
            continue
        merged[lid] = _score_one(leads[lid], ctx, now, cfg,
                                 cleared_complaint=_flag(lid))
        n_re_scored += 1

    log.info("delta_scoring: re-scored %d lead(s); %d carried over unchanged",
             n_re_scored, len(merged) - n_re_scored)
    return merged
