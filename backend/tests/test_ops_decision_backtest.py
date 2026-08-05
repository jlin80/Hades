"""The backtest that runs the production decision path, not a copy of it.

What these tests fix:

1. It really is the **production** committee and sizing engine — change a
   threshold and the backtest's verdict changes with it. That is the property
   the checklist asked for and the one a parallel implementation would lose.
2. It **cannot** trade: no TradeApproved is ever constructed, and the bus it
   publishes on is isolated from production.
3. It refuses to claim a return statistic it cannot support. A single-class
   history yields ``can_measure_returns == False`` — which is exactly the state
   the platform's outcome ledger is in today.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.learning.application.committee.factory import default_committee
from hades.contexts.learning.application.committee.manager import (
    CommitteeManager,
    QualitySignals,
)
from hades.contexts.learning.application.confidence import ConfidenceEngine
from hades.contexts.learning.application.explainability import ExplanationBuilder
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.regime import MarketRegimeClassifier
from hades.contexts.learning.infrastructure.stores import InMemoryPredictionStore
from hades.contexts.research.domain.ports import HistoricalDecisionRow
from hades.contexts.research.infrastructure.historical_reader import (
    InMemoryDecisionHistoryReader,
    project_decision,
)
from hades.contexts.risk.application.policies import MinProbabilityPolicy
from hades.contexts.risk.application.sizing import PositionSizingEngine
from hades.contexts.risk.domain.models import SizingConfig
from hades.ops.decision_backtest import (
    DecisionBacktest,
    DecisionBacktestReport,
    DecisionOutcome,
)
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry
from hades.shared_kernel.persistence.models.learning import CommitteeOutcomeRecord

_MINT = "So11111111111111111111111111111111111111112"


def _committee() -> tuple[CommitteeManager, InMemoryEventBus]:
    """The real committee, on an isolated bus and store."""
    bus = InMemoryEventBus()
    manager = CommitteeManager(
        active=default_committee(),
        regime=MarketRegimeClassifier(),
        confidence=ConfidenceEngine(),
        explainer=ExplanationBuilder(),
        event_bus=bus,
        metrics=LearningMetrics(MetricsRegistry()),
        prediction_store=InMemoryPredictionStore(),
        quality=QualitySignals(),
    )
    return manager, bus


def _rows(count: int = 3, *, label: bool | None = None) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for i in range(count):
        row: dict[str, object] = {
            "mint": _MINT,
            "at": datetime(2026, 8, 1, 12, i, tzinfo=UTC).isoformat(),
            "features": {
                "basic.liquidity": 0.8,
                "tech.rsi_14": 0.5,
                "holders.gini": 0.4,
                "pool.depth_usd": 0.7,
                "security.score": 80.0,
            },
        }
        if label is not None:
            row["label_roi_positive"] = label
            row["realized_roi"] = 0.1 if label else -0.2
        out.append(row)
    return out


def _backtest(*, min_prob: float = 0.55) -> DecisionBacktest:
    manager, _ = _committee()
    config = SizingConfig(min_prob_roi_positive=min_prob)
    return DecisionBacktest(
        committee=manager,
        sizing=PositionSizingEngine(config),
        policies=(MinProbabilityPolicy(config),),
        equity_usd=1_000.0,
    )


# -- it runs the production path ----------------------------------------------


async def test_every_row_is_evaluated_and_accounted_for() -> None:
    report = await _backtest().run(_rows(5))
    assert report.evaluated == 5
    assert report.approved + report.rejected == 5
    assert report.errors == 0


async def test_the_production_threshold_actually_governs_the_verdict() -> None:
    """Move the real risk threshold and the backtest moves with it."""
    strict = await _backtest(min_prob=0.99).run(_rows(4))
    permissive = await _backtest(min_prob=0.0).run(_rows(4))
    assert strict.approved <= permissive.approved
    assert strict.approved == 0, "nothing clears a 0.99 floor on default priors"


async def test_rejections_are_attributed_to_the_rule_that_caused_them() -> None:
    report = await _backtest(min_prob=0.99).run(_rows(3))
    assert report.rejected == 3
    causes = set(report.rejections_by_cause)
    assert causes <= {"sizing", "min_probability"}
    assert sum(report.rejections_by_cause.values()) == 3


# -- it cannot trade -----------------------------------------------------------


async def test_no_trade_approved_event_is_ever_published() -> None:
    """The money-safety invariant, asserted rather than assumed."""
    manager, bus = _committee()
    published: list[str] = []

    async def _record(event: object) -> None:
        published.append(type(event).__name__)

    for event_type in ("TradeApproved", "OrderSubmitted", "OrderFilled", "PositionOpened"):
        bus.subscribe(event_type, _record)

    config = SizingConfig(min_prob_roi_positive=0.0)
    backtest = DecisionBacktest(
        committee=manager,
        sizing=PositionSizingEngine(config),
        policies=(MinProbabilityPolicy(config),),
    )
    report = await backtest.run(_rows(5))

    assert report.evaluated == 5
    assert published == [], f"a replay published {published}"


def test_the_replay_never_imports_execution_or_portfolio() -> None:
    """It had to leave the Research Lab to compose Risk; it took the rest of the
    ban with it. Sizing computes a number, Execution places an order — reading
    the first is why this module exists, reaching the second is what it must
    never be able to do, and the AST says so rather than a comment."""
    import ast
    from pathlib import Path

    import hades.ops.decision_backtest as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("hades.contexts.execution", "hades.contexts.portfolio")
    offenders = [name for name in imported if name.startswith(forbidden)]
    assert not offenders, f"a replay must not reach execution: {offenders}"
    assert not any("risk.application.manager" in name for name in imported), (
        "the Risk Manager is the only constructor of TradeApproved; a replay must not hold it"
    )


# -- it refuses to overclaim ---------------------------------------------------


async def test_unlabelled_history_cannot_measure_returns() -> None:
    report = await _backtest().run(_rows(4))
    assert report.labelled == 0
    assert report.can_measure_returns is False


async def test_single_class_history_still_cannot_measure_returns() -> None:
    """The platform's ledger today: 1,771 rows, every one a rejection."""
    report = await _backtest().run(_rows(6, label=False))
    assert report.labelled == 6
    assert report.can_measure_returns is False


def test_both_classes_are_required_before_returns_mean_anything() -> None:
    report = DecisionBacktestReport(
        evaluated=2,
        labelled=2,
        outcomes=[
            DecisionOutcome(mint=_MINT, at=datetime.now(UTC), label_roi_positive=True),
            DecisionOutcome(mint=_MINT, at=datetime.now(UTC), label_roi_positive=False),
        ],
    )
    assert report.can_measure_returns is True


# -- it survives bad history ---------------------------------------------------


async def test_a_malformed_row_is_counted_and_skipped_not_fatal() -> None:
    bad: dict[str, object] = {"mint": "not-a-valid-mint", "features": {}}
    rows = [*_rows(2), bad, *_rows(2)]
    report = await _backtest().run(rows)
    assert report.errors == 1
    assert report.evaluated == 4, "the other rows still ran"


async def test_non_numeric_features_are_dropped_rather_than_crashing() -> None:
    rows = _rows(1)
    features = rows[0]["features"]
    assert isinstance(features, dict)
    features["tech.macd"] = "not a number"
    features["tech.atr_14"] = float("inf")
    report = await _backtest().run(rows)
    assert report.evaluated == 1
    assert report.errors == 0


async def test_an_empty_history_produces_an_empty_report_not_a_division_error() -> None:
    report = await _backtest().run([])
    assert report.evaluated == 0
    assert report.approval_rate == 0.0
    assert report.can_measure_returns is False


# -- it loads the platform's own history (§3.2) --------------------------------


async def test_run_history_replays_whatever_the_reader_hands_it() -> None:
    reader = InMemoryDecisionHistoryReader(
        [HistoricalDecisionRow(row) for row in _rows(4, label=False)]
    )
    report = await _backtest().run_history(reader)
    assert report.evaluated == 4


def test_a_rejected_outcome_is_handed_over_unlabelled() -> None:
    """The column default on a never-executed row is not a measurement."""
    record = CommitteeOutcomeRecord(
        mint=_MINT,
        at=datetime(2026, 8, 1, tzinfo=UTC),
        features={"basic.liquidity": 0.8},
        label_roi_positive=False,
        realized_roi=0.0,
        was_executed=False,
        was_rejected=True,
    )
    row = project_decision(record)
    assert row["label_roi_positive"] is None
    assert row["realized_roi"] is None
    assert row["features"] == {"basic.liquidity": 0.8}


def test_an_executed_outcome_keeps_its_realised_label() -> None:
    record = CommitteeOutcomeRecord(
        mint=_MINT,
        at=datetime(2026, 8, 1, tzinfo=UTC),
        features={"basic.liquidity": 0.8},
        label_roi_positive=True,
        realized_roi=0.14,
        was_executed=True,
        was_rejected=False,
    )
    row = project_decision(record)
    assert row["label_roi_positive"] is True
    assert row["realized_roi"] == 0.14


async def test_the_ledger_as_it_stands_today_yields_no_return_statistic() -> None:
    """1,771 rejections in, still no P&L out — the guard holds end to end."""
    records = [
        CommitteeOutcomeRecord(
            mint=_MINT,
            at=datetime(2026, 8, 1, 12, i, tzinfo=UTC),
            features={"basic.liquidity": 0.8, "security.score": 80.0},
            label_roi_positive=False,
            realized_roi=0.0,
            was_executed=False,
            was_rejected=True,
        )
        for i in range(5)
    ]
    reader = InMemoryDecisionHistoryReader([project_decision(r) for r in records])
    report = await _backtest().run_history(reader)
    assert report.evaluated == 5
    assert report.labelled == 0
    assert report.can_measure_returns is False
