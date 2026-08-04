"""Turning evidence, budget and ledger history into a progress estimate.

A pure function over value objects, deliberately kept out of the service: it is
arithmetic an operator should be able to check by hand, and the only way to keep
it checkable is to keep it away from I/O.

The design rule throughout: **an unknown is reported as unknown.** Every field
that cannot be computed honestly stays ``None`` rather than taking a default
that looks like a measurement. A cold-start programme's whole problem is that it
lacks data, so a progress report that invents figures to fill its own gaps would
be lying about precisely the subject it exists to describe.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.exploration.domain.models import (
    EvidenceStatus,
    ExplorationBudget,
    ExplorationProgress,
    ExplorationRecord,
    ExplorationSpend,
)

#: Below this many charged trades, conversion and rate are not estimated. Two
#: points can produce any slope at all, and an ETA derived from them would be
#: read as information.
MIN_TRADES_FOR_ESTIMATE = 5

#: And below this much elapsed time, a rate per *day* is not meaningful.
MIN_HOURS_FOR_RATE = 6.0


def _binding_pct(evidence: EvidenceStatus) -> float:
    """Progress against the tightest of the three conditions, capped at 1.0."""
    target = evidence.target
    ratios = (
        evidence.lessons / target.min_lessons if target.min_lessons else 1.0,
        evidence.positive / target.min_per_class if target.min_per_class else 1.0,
        evidence.negative / target.min_per_class if target.min_per_class else 1.0,
    )
    return round(min(1.0, min(ratios)), 4)


def compute_progress(
    *,
    evidence: EvidenceStatus,
    budget: ExplorationBudget,
    spend: ExplorationSpend,
    records: list[ExplorationRecord],
    now: datetime | None = None,
) -> ExplorationProgress:
    """Estimate how far exploration has come and whether it can finish.

    ``records`` is the recent slice of the append-only ledger (newest first). It
    is used only to derive elapsed time; the counts come from ``spend``, which is
    the authoritative total.
    """
    moment = now or datetime.now(UTC)
    target = evidence.target

    lessons_needed = max(0, target.min_lessons - evidence.lessons)
    positive_needed = max(0, target.min_per_class - evidence.positive)
    negative_needed = max(0, target.min_per_class - evidence.negative)
    trades_remaining = max(0, budget.max_trades_ever - spend.total_trades)

    if not evidence.available:
        # A memory that could not be read is not a memory that is empty. Saying
        # "0% complete" here is the exact confusion that once made a young
        # platform and a broken one look identical for weeks.
        return ExplorationProgress(
            lessons_needed=lessons_needed,
            positive_needed=positive_needed,
            negative_needed=negative_needed,
            trades_remaining=trades_remaining,
            note="memory unreadable — progress unknown",
        )

    progress = ExplorationProgress(
        lessons_needed=lessons_needed,
        positive_needed=positive_needed,
        negative_needed=negative_needed,
        pct_complete=_binding_pct(evidence),
        trades_remaining=trades_remaining,
    )
    if progress.complete:
        return progress.model_copy(update={"note": "evidence sufficient"})

    if spend.total_trades < MIN_TRADES_FOR_ESTIMATE:
        return progress.model_copy(
            update={
                "note": (
                    f"only {spend.total_trades} charged trades — "
                    f"need {MIN_TRADES_FOR_ESTIMATE} before estimating a rate"
                )
            }
        )

    lessons_per_trade = round(evidence.lessons / spend.total_trades, 4)
    updates: dict[str, object] = {"lessons_per_trade": lessons_per_trade}

    elapsed_hours = _elapsed_hours(records, moment)
    if elapsed_hours is not None and elapsed_hours >= MIN_HOURS_FOR_RATE:
        per_day = round(evidence.lessons / (elapsed_hours / 24.0), 4)
        updates["lessons_per_day"] = per_day
        if per_day > 0 and lessons_needed > 0:
            updates["eta_days"] = round(lessons_needed / per_day, 2)

    # Can the remaining runway still buy what is missing? Only asked when the
    # conversion is actually known — an unknown conversion raises no alarm.
    if lessons_per_trade > 0:
        affordable = trades_remaining * lessons_per_trade
        if affordable < lessons_needed:
            updates["budget_exhausts_first"] = True
            updates["note"] = (
                f"runway buys ~{affordable:.1f} more lessons but {lessons_needed} "
                f"are missing — the budget runs out before the evidence does"
            )
    elif spend.total_trades > 0:
        updates["budget_exhausts_first"] = True
        updates["note"] = (
            f"{spend.total_trades} trades charged and no settled lessons yet — "
            "spending without learning"
        )

    return progress.model_copy(update=updates)


def _elapsed_hours(records: list[ExplorationRecord], now: datetime) -> float | None:
    """Hours since the oldest record in the slice, or ``None`` if unusable."""
    if not records:
        return None
    stamps = [_as_utc(r.granted_at) for r in records]
    oldest = min(stamps)
    elapsed = (now - oldest).total_seconds() / 3600.0
    return elapsed if elapsed > 0 else None


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


__all__ = ["MIN_HOURS_FOR_RATE", "MIN_TRADES_FOR_ESTIMATE", "compute_progress"]
