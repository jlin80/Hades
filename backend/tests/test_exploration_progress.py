"""Progress toward a trainable model — and whether the budget can get there.

The properties that matter, in order of what they would cost to get wrong:

1. Progress is the **binding** condition, never the average. 60 lessons that are
   all losses must not read as nearly finished — that is the exact state the
   evidence design exists to prevent.
2. An unknown stays **unknown**. A cold-start programme's problem is missing
   data; a report that invents numbers to fill its gaps lies about its subject.
3. ``budget_exhausts_first`` is **conservative**. It fires only when the
   arithmetic is known and says the runway is short.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hades.contexts.exploration.domain.models import (
    EvidenceStatus,
    EvidenceTarget,
    ExplorationBudget,
    ExplorationRecord,
    ExplorationSpend,
)
from hades.contexts.exploration.domain.progress import (
    MIN_TRADES_FOR_ESTIMATE,
    compute_progress,
)

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_TARGET = EvidenceTarget(min_lessons=60, min_per_class=15)


def _evidence(
    *, lessons: int, positive: int, negative: int, available: bool = True
) -> EvidenceStatus:
    return EvidenceStatus(
        lessons=lessons,
        positive=positive,
        negative=negative,
        target=_TARGET,
        available=available,
    )


def _records(*, count: int, oldest_hours_ago: float) -> list[ExplorationRecord]:
    oldest = _NOW - timedelta(hours=oldest_hours_ago)
    return [
        ExplorationRecord(
            subject=f"tok{i}",
            granted_at=oldest + timedelta(hours=i * 0.1),
            notional_usd=1.0,
        )
        for i in range(count)
    ]


def _budget(*, total_usd: float = 250.0, per_trade: float = 1.0) -> ExplorationBudget:
    return ExplorationBudget(per_trade_usd=per_trade, total_usd=total_usd)


# -- progress is the binding condition ----------------------------------------


def test_a_single_class_dataset_is_not_nearly_finished() -> None:
    """60 lessons, all losses: satisfies the count, cannot validate a model."""
    progress = compute_progress(
        evidence=_evidence(lessons=60, positive=0, negative=60),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=60, total_usd=60.0),
        records=_records(count=60, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.pct_complete == 0.0, "the binding condition is 0 positives"
    assert progress.complete is False
    assert progress.positive_needed == 15
    assert progress.negative_needed == 0


def test_progress_tracks_the_tightest_of_the_three_conditions() -> None:
    # 30/60 lessons (0.5), 15/15 positive (1.0), 15/15 negative (1.0) → 0.5
    progress = compute_progress(
        evidence=_evidence(lessons=30, positive=15, negative=15),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=30, total_usd=30.0),
        records=_records(count=30, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.pct_complete == 0.5


def test_sufficiency_reads_as_complete() -> None:
    progress = compute_progress(
        evidence=_evidence(lessons=60, positive=30, negative=30),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=60, total_usd=60.0),
        records=_records(count=60, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.complete is True
    assert progress.note == "evidence sufficient"
    assert progress.budget_exhausts_first is False


# -- unknowns stay unknown -----------------------------------------------------


def test_an_unreadable_memory_is_not_zero_percent() -> None:
    """A young platform and a broken one must never look identical."""
    progress = compute_progress(
        evidence=_evidence(lessons=0, positive=0, negative=0, available=False),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=20, total_usd=20.0),
        records=_records(count=20, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.note == "memory unreadable — progress unknown"
    assert progress.lessons_per_trade is None
    assert progress.eta_days is None
    assert progress.budget_exhausts_first is False, "no alarm on an unknown"


def test_too_few_trades_produces_no_rate_and_no_eta() -> None:
    progress = compute_progress(
        evidence=_evidence(lessons=2, positive=1, negative=1),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=MIN_TRADES_FOR_ESTIMATE - 1, total_usd=4.0),
        records=_records(count=4, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.lessons_per_trade is None
    assert progress.lessons_per_day is None
    assert progress.eta_days is None
    assert progress.budget_exhausts_first is False
    assert "before estimating a rate" in progress.note


def test_too_little_elapsed_time_produces_a_conversion_but_no_daily_rate() -> None:
    """Lessons-per-trade needs no clock; lessons-per-day does."""
    progress = compute_progress(
        evidence=_evidence(lessons=10, positive=5, negative=5),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=20, total_usd=20.0),
        records=_records(count=20, oldest_hours_ago=1.0),  # under the 6h floor
        now=_NOW,
    )
    assert progress.lessons_per_trade == 0.5
    assert progress.lessons_per_day is None
    assert progress.eta_days is None


def test_an_empty_ledger_yields_no_daily_rate() -> None:
    progress = compute_progress(
        evidence=_evidence(lessons=10, positive=5, negative=5),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=20, total_usd=20.0),
        records=[],
        now=_NOW,
    )
    assert progress.lessons_per_day is None
    assert progress.eta_days is None


# -- the estimate --------------------------------------------------------------


def test_eta_is_derived_from_the_observed_rate() -> None:
    # 30 lessons over 10 days = 3/day; 30 more needed → 10 days.
    progress = compute_progress(
        evidence=_evidence(lessons=30, positive=15, negative=15),
        budget=_budget(total_usd=250.0),
        spend=ExplorationSpend(total_trades=30, total_usd=30.0),
        records=_records(count=30, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.lessons_per_day == 3.0
    assert progress.eta_days == 10.0
    assert progress.lessons_per_trade == 1.0


def test_trades_remaining_comes_from_the_lifetime_budget() -> None:
    progress = compute_progress(
        evidence=_evidence(lessons=10, positive=5, negative=5),
        budget=_budget(total_usd=100.0, per_trade=2.0),  # funds 50 trades ever
        spend=ExplorationSpend(total_trades=20, total_usd=40.0),
        records=_records(count=20, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.trades_remaining == 30


# -- the alert -----------------------------------------------------------------


def test_budget_exhausts_first_when_the_runway_cannot_buy_what_is_missing() -> None:
    # 0.5 lessons/trade, 10 trades left → ~5 more lessons, but 50 are missing.
    progress = compute_progress(
        evidence=_evidence(lessons=10, positive=5, negative=5),
        budget=_budget(total_usd=30.0),  # funds 30 trades ever
        spend=ExplorationSpend(total_trades=20, total_usd=20.0),
        records=_records(count=20, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.lessons_per_trade == 0.5
    assert progress.trades_remaining == 10
    assert progress.budget_exhausts_first is True
    assert "runs out before the evidence" in progress.note


def test_a_healthy_runway_raises_no_alarm() -> None:
    progress = compute_progress(
        evidence=_evidence(lessons=30, positive=15, negative=15),
        budget=_budget(total_usd=500.0),
        spend=ExplorationSpend(total_trades=30, total_usd=30.0),
        records=_records(count=30, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.budget_exhausts_first is False


def test_spending_with_zero_lessons_is_flagged_as_spending_without_learning() -> None:
    progress = compute_progress(
        evidence=_evidence(lessons=0, positive=0, negative=0),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=40, total_usd=40.0),
        records=_records(count=40, oldest_hours_ago=240),
        now=_NOW,
    )
    assert progress.lessons_per_trade == 0.0
    assert progress.budget_exhausts_first is True
    assert "spending without learning" in progress.note


def test_stalled_requires_a_measured_rate_of_zero_not_an_absent_one() -> None:
    measured_zero = compute_progress(
        evidence=_evidence(lessons=0, positive=0, negative=0),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=40, total_usd=40.0),
        records=_records(count=40, oldest_hours_ago=240),
        now=_NOW,
    )
    assert measured_zero.lessons_per_day == 0.0
    assert measured_zero.stalled is True

    no_history = compute_progress(
        evidence=_evidence(lessons=0, positive=0, negative=0),
        budget=_budget(),
        spend=ExplorationSpend(total_trades=2, total_usd=2.0),
        records=[],
        now=_NOW,
    )
    assert no_history.lessons_per_day is None
    assert no_history.stalled is False, "absence of a rate is not a rate of zero"
