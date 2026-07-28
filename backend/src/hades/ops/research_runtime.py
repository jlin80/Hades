"""Research Lab runtime composition — wires and runs the offline lab.

Process-level wiring (the ops layer may import application + infrastructure). It
assembles the :class:`ResearchManager`, subscribes a shadow-strategy handler to
the live ``FeaturesComputed`` stream (virtual, zero-capital), and — when
``auto_research`` is on — runs a periodic, off-the-hot-path loop that pulls copied
history (the committee's labelled outcome ledger), backtests the candidate
strategies, compares them, discovers features and generates reports.

Two invariants are structural here: the lab reads a **copy** of history through a
read-only reader, and it has **no** Execution/Risk/Portfolio collaborator. It can
publish knowledge events and a Redis status snapshot for the dashboard; it can
never place an order or enable live trading. Every loop iteration is defensive —
a research failure must never disturb the trading system.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.features.domain.events import FeaturesComputed
from hades.contexts.research.application.knowledge_base import KnowledgeBase
from hades.contexts.research.application.manager import ResearchManager
from hades.contexts.research.application.metrics import compute
from hades.contexts.research.application.scheduler import AutoResearchScheduler, ResearchTask
from hades.contexts.research.application.strategies import default_candidates, evaluate
from hades.contexts.research.application.subscriber import ResearchShadowHandler
from hades.contexts.research.domain.events import ShadowStrategyUpdated
from hades.contexts.research.domain.models import (
    CandidateStrategy,
    PerformanceMetrics,
    PromotionCriteria,
    ShadowStrategy,
)
from hades.contexts.research.infrastructure.historical_reader import (
    InMemoryHistoricalReader,
    OutcomeHistoricalReader,
)
from hades.contexts.research.infrastructure.stores import (
    InMemoryBacktestStore,
    InMemoryCandidateStore,
    InMemoryExperimentStore,
    InMemoryKnowledgeStore,
    InMemoryPromotionStore,
    InMemoryReportStore,
    InMemoryShadowStore,
    PostgresBacktestStore,
    PostgresCandidateStore,
    PostgresExperimentStore,
    PostgresKnowledgeStore,
    PostgresPromotionStore,
    PostgresReportStore,
    PostgresShadowStore,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

_logger = get_logger("research.runtime")

RESEARCH_STATUS_NAMESPACE = "research"
RESEARCH_STATUS_KEY = "status"
_STATUS_TTL_SECONDS = 30


class ResearchRuntime:
    """Owns the wired Research Lab and its lifecycle. Never blocks production."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._cache = CacheService(container.redis, namespace=RESEARCH_STATUS_NAMESPACE)
        rs = container.settings.research

        db = container.database
        if db is not None:
            self._experiments: Any = PostgresExperimentStore(db)
            self._backtests: Any = PostgresBacktestStore(db)
            self._candidates: Any = PostgresCandidateStore(db)
            self._shadows: Any = PostgresShadowStore(db)
            self._promotions: Any = PostgresPromotionStore(db)
            self._reports: Any = PostgresReportStore(db)
            self._knowledge_store: Any = PostgresKnowledgeStore(db)
            self._reader: Any = OutcomeHistoricalReader(db)
        else:
            self._experiments = InMemoryExperimentStore()
            self._backtests = InMemoryBacktestStore()
            self._candidates = InMemoryCandidateStore()
            self._shadows = InMemoryShadowStore()
            self._promotions = InMemoryPromotionStore()
            self._reports = InMemoryReportStore()
            self._knowledge_store = InMemoryKnowledgeStore()
            self._reader = InMemoryHistoricalReader([])

        self._manager = ResearchManager(
            event_bus=container.event_bus,
            notifier=container.notification,
            knowledge=KnowledgeBase(self._knowledge_store),
            experiment_store=self._experiments,
            backtest_store=self._backtests,
            candidate_store=self._candidates,
            promotion_store=self._promotions,
            report_store=self._reports,
            criteria=PromotionCriteria(
                min_trades=rs.promo_min_trades,
                min_sharpe=rs.promo_min_sharpe,
                max_drawdown=rs.promo_max_drawdown,
                min_profit_factor=rs.promo_min_profit_factor,
            ),
        )

        # The research population: one candidate per archetype + a shadow each.
        self._population: tuple[CandidateStrategy, ...] = default_candidates()
        shadows = [
            ShadowStrategy(name=c.name, archetype=c.archetype, genome=c.genome)
            for c in self._population
        ]
        self._shadow_handler = ResearchShadowHandler(shadows)
        self._scheduler = AutoResearchScheduler()
        self._scheduler.seed(time.time())  # nothing is due the instant we boot
        self._register()

    def _register(self) -> None:
        if self._c.settings.research.shadow_enabled:
            self._c.event_bus.subscribe(FeaturesComputed.__name__, self._on_features_computed)

    async def _on_features_computed(self, event: DomainEvent) -> None:
        await self._shadow_handler.on_features_computed(event)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        tasks = [
            asyncio.create_task(self._publish_status_loop(), name="research-status"),
            asyncio.create_task(self._shadow_publish_loop(), name="research-shadow"),
        ]
        if self._c.settings.research.auto_research:
            tasks.append(asyncio.create_task(self._auto_research_loop(), name="research-auto"))
        _logger.info(
            "research_runtime_started",
            lab_enabled=self._c.settings.research.lab_enabled,
            auto_research=self._c.settings.research.auto_research,
            candidates=len(self._population),
        )
        return tasks

    async def stop(self) -> None:
        self._stop.set()
        _logger.info("research_runtime_stopped")

    # -- shadow snapshots -----------------------------------------------------

    async def _shadow_publish_loop(self) -> None:
        interval = self._c.settings.research.shadow_publish_interval_seconds
        while not self._stop.is_set():
            try:
                for shadow in self._shadow_handler.shadows:
                    await self._shadows.save(shadow)
                    await self._c.event_bus.publish(
                        ShadowStrategyUpdated(
                            aggregate_id=shadow.shadow_id,
                            shadow_id=str(shadow.shadow_id),
                            name=shadow.name,
                            metrics=shadow.metrics,
                        )
                    )
            except Exception as exc:  # shadow publishing is best-effort
                _logger.debug("shadow_publish_failed", error=str(exc))
            if await self._sleep(interval):
                break

    # -- auto research --------------------------------------------------------

    async def _auto_research_loop(self) -> None:
        # Give the system time to accrue outcomes before the first heavy pass.
        if await self._sleep(30.0):
            return
        while not self._stop.is_set():
            try:
                await self._run_due_research()
            except Exception as exc:  # a research pass must never crash the worker
                _logger.warning("auto_research_failed", error=str(exc))
            if await self._sleep(300.0):
                break

    async def _run_due_research(self) -> None:
        now = time.time()
        due = set(self._scheduler.due(now))
        if not due:
            return
        rs = self._c.settings.research
        samples = list(await self._reader.load_samples(limit=50_000))
        if len(samples) < rs.min_samples:
            _logger.info("auto_research_deferred", samples=len(samples), need=rs.min_samples)
            return

        scorecards: dict[str, PerformanceMetrics] = {}
        if ResearchTask.BACKTEST in due:
            for candidate in self._population:
                result = await self._manager.run_backtest(candidate=candidate, samples=samples)
                scorecards[candidate.name] = result.metrics
            self._scheduler.mark_ran(ResearchTask.BACKTEST, now)

        if ResearchTask.STRATEGY_COMPARISON in due:
            if not scorecards:
                scorecards = {
                    c.name: compute(evaluate(c.genome, samples)) for c in self._population
                }
            await self._manager.compare_strategies(scorecards)
            self._scheduler.mark_ran(ResearchTask.STRATEGY_COMPARISON, now)

        if ResearchTask.FEATURE_DISCOVERY in due:
            await self._manager.discover_features(samples)
            self._scheduler.mark_ran(ResearchTask.FEATURE_DISCOVERY, now)

        if ResearchTask.WEEKLY_REPORT in due:
            await self._manager.generate_report(period="weekly")
            self._scheduler.mark_ran(ResearchTask.WEEKLY_REPORT, now)
        elif ResearchTask.DAILY_REPORT in due:
            await self._manager.generate_report(period="daily")
            self._scheduler.mark_ran(ResearchTask.DAILY_REPORT, now)

    # -- dashboard status -----------------------------------------------------

    async def _snapshot(self) -> dict[str, Any]:
        shadows = self._shadow_handler.shadows
        return {
            "lab_enabled": self._c.settings.research.lab_enabled,
            "auto_research": self._c.settings.research.auto_research,
            "candidates": len(self._population),
            "shadow_strategies": [
                {
                    "name": s.name,
                    "archetype": s.archetype.value,
                    "open_trades": len(s.open_trades),
                    "trades": s.metrics.trades,
                    "total_return": s.metrics.total_return,
                    "expectancy": s.metrics.expectancy,
                }
                for s in shadows
            ],
            "historical_samples": await self._safe_count(),
            "updated_at": time.time(),
        }

    async def _safe_count(self) -> int:
        try:
            return int(await self._reader.count())
        except Exception:
            return 0

    async def _publish_status_loop(self) -> None:
        interval = self._c.settings.research.status_interval_seconds
        while not self._stop.is_set():
            try:
                snapshot = await self._snapshot()
                await self._cache.set(
                    RESEARCH_STATUS_KEY, snapshot, ttl_seconds=_STATUS_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("research_status_publish_failed", error=str(exc))
            if await self._sleep(interval):
                break

    async def _sleep(self, seconds: float) -> bool:
        """Sleep up to ``seconds`` or until stop. Returns True if stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    # -- accessors for the API ------------------------------------------------

    @property
    def manager(self) -> ResearchManager:
        return self._manager
