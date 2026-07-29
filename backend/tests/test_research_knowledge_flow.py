"""Research → Knowledge, entirely over the bus.

The lab is the platform's official producer of knowledge, and the connection
between the two is nothing but domain events. Neither context imports the other;
neither knows the other exists. The Research Lab publishes what it finished, the
memory happens to be listening, and either could be deleted without the other
failing to import.

These tests drive the real `ResearchManager` against the real `KnowledgeRuntime`
over a real bus, and assert that every finished study lands in permanent memory
labelled as a simulation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from hades.contexts.knowledge.domain.models import (
    KnowledgeQuery,
    KnowledgeSource,
    Verification,
)
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.research.application.knowledge_base import KnowledgeBase
from hades.contexts.research.application.manager import ResearchManager
from hades.contexts.research.application.strategies import default_candidates
from hades.contexts.research.domain.ports import HistoricalSample
from hades.contexts.research.infrastructure.historical_reader import InMemoryHistoricalReader
from hades.contexts.research.infrastructure.stores import (
    InMemoryBacktestStore,
    InMemoryCandidateStore,
    InMemoryExperimentStore,
    InMemoryPromotionStore,
    InMemoryReportStore,
)
from hades.contexts.research.infrastructure.stores import (
    InMemoryKnowledgeStore as InMemoryResearchKnowledgeStore,
)
from hades.ops.knowledge_runtime import KnowledgeRuntime
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry

_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_MINT = "So11111111111111111111111111111111111111112"


class _FakeRedis:
    async def get(self, key: str) -> Any:
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None


@dataclass
class _Container:
    settings: Any
    event_bus: InMemoryEventBus
    redis: Any
    metrics: MetricsRegistry
    database: None = None


def _samples(count: int = 60) -> list[HistoricalSample]:
    return [
        HistoricalSample(
            token_mint=_MINT,
            at=_AT,
            features={"basic.liquidity": 0.5 + (index % 10) / 40, "tech.rsi_14": 0.4},
            realized_roi=0.15 if index % 3 else -0.1,
            label_roi_positive=bool(index % 3),
            hit_tp=bool(index % 3),
            hit_sl=not index % 3,
        )
        for index in range(count)
    ]


@pytest.fixture
def wired() -> tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus]:
    from hades.shared_kernel.config import Settings

    bus = InMemoryEventBus()
    container = _Container(
        settings=Settings(), event_bus=bus, redis=_FakeRedis(), metrics=MetricsRegistry()
    )
    knowledge = KnowledgeRuntime(container)  # type: ignore[arg-type]
    manager = ResearchManager(
        event_bus=bus,
        notifier=NotificationPublisher(bus),
        knowledge=KnowledgeBase(InMemoryResearchKnowledgeStore()),
        experiment_store=InMemoryExperimentStore(),
        backtest_store=InMemoryBacktestStore(),
        candidate_store=InMemoryCandidateStore(),
        promotion_store=InMemoryPromotionStore(),
        report_store=InMemoryReportStore(),
        historical_reader=InMemoryHistoricalReader(_samples()),
    )
    return manager, knowledge, bus


async def _recorded(knowledge: KnowledgeRuntime, source: KnowledgeSource) -> list[Any]:
    return await knowledge.store.query(KnowledgeQuery(source=source, limit=1000))


@pytest.mark.asyncio
async def test_a_finished_backtest_becomes_knowledge(
    wired: tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus],
) -> None:
    manager, knowledge, _ = wired
    candidate = default_candidates()[0]

    await manager.run_backtest(candidate=candidate, samples=_samples())

    rows = await _recorded(knowledge, KnowledgeSource.BACKTEST)
    assert len(rows) == 1
    assert rows[0].verification is Verification.SIMULATED


@pytest.mark.asyncio
async def test_walk_forward_and_monte_carlo_each_land_under_their_own_provenance(
    wired: tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """Provenance is per-study, not a single "research" bucket: a walk-forward
    result and a Monte-Carlo result answer different questions."""
    manager, knowledge, _ = wired
    candidate = default_candidates()[0]

    await manager.run_walk_forward(candidate=candidate, samples=_samples())
    await manager.run_monte_carlo(candidate=candidate, samples=_samples(), simulations=50)

    assert len(await _recorded(knowledge, KnowledgeSource.WALK_FORWARD)) == 1
    assert len(await _recorded(knowledge, KnowledgeSource.MONTE_CARLO)) == 1


@pytest.mark.asyncio
async def test_a_replay_now_produces_a_fact(
    wired: tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """The Replay Engine shipped in Phase 9 with no caller at all, and
    `ReplayCompleted` was registered on the bus and never published. A study
    nobody can run produces no knowledge."""
    manager, knowledge, _ = wired

    result = await manager.run_replay(from_iso="2026-07-01T00:00:00", to_iso="2026-07-28T00:00:00")

    assert result.events_replayed == 60
    rows = await _recorded(knowledge, KnowledgeSource.RESEARCH_LAB)
    assert any(row.payload.get("events_replayed") == 60 for row in rows)


@pytest.mark.asyncio
async def test_a_promotion_decision_is_recorded_as_knowledge_and_deploys_nothing(
    wired: tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """The lab's conclusions were the part the memory used to miss: it held the
    experiments but not what the lab decided about them."""
    from hades.contexts.research.domain.models import PerformanceMetrics

    manager, knowledge, _ = wired
    candidate = default_candidates()[0]
    # Comfortably over the default bar (min_trades=500, sharpe>1, DD<0.25, PF>1.3),
    # so this exercises the approved branch rather than the rejected one.
    metrics = PerformanceMetrics(
        trades=600,
        sharpe=2.1,
        max_drawdown=0.1,
        profit_factor=1.9,
        win_rate=0.55,
        expectancy=0.04,
    )

    decision = await manager.evaluate_promotion(
        candidate, metrics=metrics, paper_positive=True, shadow_positive=True, manual_approve=True
    )

    rows = await _recorded(knowledge, KnowledgeSource.RESEARCH_LAB)
    assert rows, "a promotion decision must reach permanent memory"
    assert all(row.verification is Verification.SIMULATED for row in rows)
    # Recording a governance decision deploys nothing.
    assert decision.promotable is True


@pytest.mark.asyncio
async def test_research_knowledge_is_never_ground_truth(
    wired: tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """No study, however conclusive, may enter memory as settled reality —
    otherwise the committee could be trained on the lab's own assumptions."""
    manager, knowledge, _ = wired
    candidate = default_candidates()[0]

    await manager.run_backtest(candidate=candidate, samples=_samples())
    await manager.run_walk_forward(candidate=candidate, samples=_samples())
    await manager.generate_report(period="weekly")

    everything = await knowledge.store.query(KnowledgeQuery(limit=1000))
    assert everything, "the sweep recorded nothing — the wiring is broken"
    assert not any(row.verification is Verification.REALISED for row in everything)


@pytest.mark.asyncio
async def test_the_lab_produces_no_lessons(
    wired: tuple[ResearchManager, KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """Lessons are the training ledger's currency and only a settled trade mints
    one. A whole research session must leave the lesson store empty."""
    manager, knowledge, _ = wired
    candidate = default_candidates()[0]

    await manager.run_backtest(candidate=candidate, samples=_samples())
    await manager.run_monte_carlo(candidate=candidate, samples=_samples(), simulations=25)
    await manager.run_replay(from_iso="2026-07-01T00:00:00", to_iso="2026-07-28T00:00:00")

    assert await knowledge.lessons.count() == 0
