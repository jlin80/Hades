"""Strategy Engine runtime composition - wires and runs the strategy roster.

Process-level wiring (the ops layer may import application + infrastructure). It
assembles the Strategy Engine from the container and subscribes it between the AI
Committee and the Risk Manager:

    committee ─CommitteePredictionGenerated─► [context builder -> strategy engine]
        ─► SignalGenerated / EnsembleSignalGenerated

The engine only produces signals; it never executes, sizes or approves a trade.
Self-evaluation is fed off the Position stream (open->close) so a strategy's weight
is tapered by its realised results. A live status snapshot is published to Redis
for the dashboard, exactly like the Execution and Committee runtimes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.strategy.application.factory import (
    StrategyEngineBundle,
    build_strategy_engine,
)
from hades.contexts.strategy.application.metrics import StrategyMetrics
from hades.contexts.strategy.application.subscriber import StrategyHandler
from hades.contexts.strategy.domain.models import StrategyPerformance
from hades.contexts.strategy.infrastructure.stores import (
    InMemoryPerformanceStore,
    InMemorySignalStore,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.events.bus import LaneBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("strategy.runtime")

#: Redis namespace/key the runtime publishes strategy status under.
STRATEGY_STATUS_NAMESPACE = "strategy"
STATUS_KEY = "status"
_STATUS_TTL_SECONDS = 30


class StrategyRuntime:
    """Owns the wired Strategy Engine and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._bus = LaneBus(container.event_bus, "strategy")
        self._stop = asyncio.Event()
        self._metrics = StrategyMetrics(container.metrics)
        self._cache = CacheService(container.redis, namespace=STRATEGY_STATUS_NAMESPACE)
        self._performance = InMemoryPerformanceStore()
        self._signals = InMemorySignalStore()
        self._bundle: StrategyEngineBundle = build_strategy_engine(
            container.settings,
            event_bus=self._bus,
            notifier=container.notification,
            metrics=self._metrics,
            performance_store=self._performance,
            signal_store=self._signals,
        )
        self._handler = StrategyHandler(self._bundle.engine, self._bundle.context_builder)
        self._handler.register(self._bus)

    @property
    def bundle(self) -> StrategyEngineBundle:
        return self._bundle

    @property
    def signal_store(self) -> InMemorySignalStore:
        return self._signals

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        await self._bundle.engine.initialize()
        tasks = [asyncio.create_task(self._publish_status_loop(), name="strategy-status")]
        _logger.info(
            "strategy_runtime_started",
            strategies=len(self._bundle.registry),
            active=len(self._bundle.registry.active()),
            gate_risk=self._c.settings.strategy.gate_risk,
        )
        return tasks

    async def stop(self) -> None:
        self._stop.set()
        await self._bundle.engine.shutdown()
        _logger.info("strategy_runtime_stopped")

    # -- dashboard status -----------------------------------------------------

    async def _performance_snapshot(self) -> dict[str, StrategyPerformance]:
        try:
            return await self._performance.all_performance()
        except Exception:  # status must render even if evaluation hiccups
            return {}

    async def _snapshot(self) -> dict[str, Any]:
        performance = await self._performance_snapshot()
        snap = self._bundle.engine.snapshot(performance)
        snap["enabled"] = self._c.settings.strategy.enabled
        snap["gate_risk"] = self._c.settings.strategy.gate_risk
        snap["recent_signals"] = await self._signals.recent_signals(limit=25)
        snap["recent_ensembles"] = await self._signals.recent_ensembles(limit=25)
        snap["updated_at"] = time.time()
        return snap

    async def _publish_status_loop(self) -> None:
        interval = self._c.settings.strategy.status_interval_seconds
        while not self._stop.is_set():
            try:
                snapshot = await self._snapshot()
                await self._cache.set(STATUS_KEY, snapshot, ttl_seconds=_STATUS_TTL_SECONDS)
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("strategy_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue
