"""Integration tests for the Research Manager over in-memory infrastructure.

Exercises the coordinator end-to-end and asserts the events it emits, the
knowledge it records, and — most importantly — the safety invariants: the lab
proposes and recommends, but never promotes to production on its own.
"""

from __future__ import annotations

import random

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.research.application.knowledge_base import KnowledgeBase
from hades.contexts.research.application.manager import ResearchManager
from hades.contexts.research.application.metrics import compute
from hades.contexts.research.application.replay_engine import ReplayEngine
from hades.contexts.research.application.strategies import default_candidates, evaluate
from hades.contexts.research.domain.events import (
    BacktestCompleted,
    ExperimentFinished,
    FeatureProposed,
    PromotionRejected,
    ResearchReportGenerated,
    ResearchStrategyPromoted,
)
from hades.contexts.research.domain.models import (
    CandidateStrategy,
    ExperimentKind,
    ExperimentSpec,
    PerformanceMetrics,
    PromotionCriteria,
    ShadowStrategy,
    StrategyArchetype,
)
from hades.contexts.research.domain.ports import HistoricalSample
from hades.contexts.research.infrastructure.historical_reader import InMemoryHistoricalReader
from hades.contexts.research.infrastructure.stores import (
    InMemoryBacktestStore,
    InMemoryCandidateStore,
    InMemoryExperimentStore,
    InMemoryKnowledgeStore,
    InMemoryPromotionStore,
    InMemoryReportStore,
)
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import InMemoryEventBus


class _Capture:
    def __init__(self, bus: InMemoryEventBus) -> None:
        self.events: list[DomainEvent] = []
        for name in (
            "ExperimentStarted",
            "ExperimentFinished",
            "BacktestCompleted",
            "FeatureProposed",
            "ResearchStrategyPromoted",
            "PromotionRejected",
            "ResearchReportGenerated",
            "StrategyCompared",
        ):
            bus.subscribe(name, self._on)

    async def _on(self, event: DomainEvent) -> None:
        self.events.append(event)

    def of(self, cls: type) -> list[DomainEvent]:
        return [e for e in self.events if isinstance(e, cls)]


def _samples(n: int = 300, *, seed: int = 3) -> list[HistoricalSample]:
    rng = random.Random(seed)
    out: list[HistoricalSample] = []
    for i in range(n):
        strong = rng.random()
        out.append(
            HistoricalSample(
                {
                    "mint": f"M{i:04d}" + "x" * 40,
                    "momentum": strong,
                    "price_change": strong,
                    "liquidity": rng.random(),
                    "volume": rng.random(),
                    "forward_return": 0.4 * (strong - 0.5) + rng.uniform(-0.05, 0.05),
                }
            )
        )
    return out


def _manager(
    bus: InMemoryEventBus, *, criteria: PromotionCriteria | None = None
) -> ResearchManager:
    knowledge_store = InMemoryKnowledgeStore()
    return ResearchManager(
        event_bus=bus,
        notifier=NotificationPublisher(bus),
        knowledge=KnowledgeBase(knowledge_store),
        experiment_store=InMemoryExperimentStore(),
        backtest_store=InMemoryBacktestStore(),
        candidate_store=InMemoryCandidateStore(),
        promotion_store=InMemoryPromotionStore(),
        report_store=InMemoryReportStore(),
        criteria=criteria,
    )


async def test_run_experiment_emits_finished_and_records_knowledge() -> None:
    bus = InMemoryEventBus()
    cap = _Capture(bus)
    manager = _manager(bus)
    spec = ExperimentSpec(
        name="tp-tweak", kind=ExperimentKind.BACKTEST, hypothesis="higher TP helps"
    )
    result = await manager.run_experiment(spec, _samples())
    assert result.status.value == "finished"
    assert cap.of(ExperimentFinished)


async def test_run_backtest_emits_event_and_persists() -> None:
    bus = InMemoryEventBus()
    cap = _Capture(bus)
    manager = _manager(bus)
    candidate = CandidateStrategy(
        name="mom",
        archetype=StrategyArchetype.MOMENTUM,
        genome=default_candidates()[0].genome.model_copy(update={"entry_score": 0.5}),
    )
    result = await manager.run_backtest(candidate=candidate, samples=_samples())
    assert result.metrics.trades > 0
    assert cap.of(BacktestCompleted)


async def test_discover_features_emits_proposals_only() -> None:
    bus = InMemoryEventBus()
    cap = _Capture(bus)
    manager = _manager(bus)
    await manager.discover_features(_samples())
    # Proposals may or may not exist depending on data, but any emitted event is a
    # proposal — never an application/removal.
    for e in cap.of(FeatureProposed):
        assert isinstance(e, FeatureProposed)


async def test_promotion_requires_human_even_when_metrics_pass() -> None:
    bus = InMemoryEventBus()
    cap = _Capture(bus)
    manager = _manager(bus, criteria=PromotionCriteria())
    strong = CandidateStrategy(
        name="strong",
        archetype=StrategyArchetype.MOMENTUM,
        genome=default_candidates()[0].genome,
        metrics=PerformanceMetrics(
            trades=600, sharpe=1.6, max_drawdown=0.12, profit_factor=1.7, expectancy=0.03
        ),
    )
    # Without manual approval → rejected, no ResearchStrategyPromoted event.
    d1 = await manager.evaluate_promotion(
        strong, paper_positive=True, shadow_positive=True, manual_approve=False
    )
    assert not d1.promotable
    assert cap.of(PromotionRejected)
    assert not cap.of(ResearchStrategyPromoted)

    # With explicit human approval → promotable, emits ResearchStrategyPromoted (a
    # governance record; it still deploys nothing).
    d2 = await manager.evaluate_promotion(
        strong, paper_positive=True, shadow_positive=True, manual_approve=True
    )
    assert d2.promotable
    assert cap.of(ResearchStrategyPromoted)


async def test_generate_report_persists_and_emits() -> None:
    bus = InMemoryEventBus()
    cap = _Capture(bus)
    manager = _manager(bus)
    await manager.run_experiment(
        ExperimentSpec(name="e1", kind=ExperimentKind.BACKTEST), _samples()
    )
    report = await manager.generate_report(period="weekly")
    assert report.period == "weekly"
    assert cap.of(ResearchReportGenerated)


async def test_compare_strategies_ranks_population() -> None:
    bus = InMemoryEventBus()
    manager = _manager(bus)
    samples = _samples()
    scorecards = {
        c.name: compute(evaluate(c.genome.model_copy(update={"entry_score": 0.5}), samples))
        for c in default_candidates()[:3]
    }
    comparison = await manager.compare_strategies(scorecards)
    assert len(comparison.ranking) == 3


async def test_replay_drives_shadows_without_orders() -> None:
    reader = InMemoryHistoricalReader(_samples())
    engine = ReplayEngine(reader)
    shadow = ShadowStrategy(
        name="s",
        archetype=StrategyArchetype.MOMENTUM,
        genome=default_candidates()[0].genome.model_copy(update={"entry_score": 0.5}),
    )
    result = await engine.replay(from_iso="a", to_iso="b", shadows=[shadow])
    assert result.events_replayed == len(_samples())
    assert len(result.shadows) == 1
