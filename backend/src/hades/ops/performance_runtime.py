"""Performance runtime — hosts the Performance Monitor in the Worker.

Registers the monitor on the event bus (so throughput is measured from live
events) and periodically publishes its snapshot to Redis under the
``performance`` namespace, exactly like the risk/portfolio status snapshots the
dashboard already consumes. The API reads that snapshot for
``GET /api/v1/metrics/performance``.
"""

from __future__ import annotations

import asyncio

from hades.bootstrap import Container
from hades.contexts.monitoring.application.performance_monitor import PerformanceMonitor
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.events.bus import LaneBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("performance.runtime")

PERFORMANCE_NAMESPACE = "performance"
STATUS_KEY = "status"
_INTERVAL_SECONDS = 5.0
_TTL_SECONDS = 30


class PerformanceRuntime:
    """Owns the Performance Monitor and its Redis status publishing."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._bus = LaneBus(container.event_bus, "performance")
        self._stop = asyncio.Event()
        self._monitor = PerformanceMonitor(container.metrics)
        self._cache = CacheService(container.redis, namespace=PERFORMANCE_NAMESPACE)

    @property
    def monitor(self) -> PerformanceMonitor:
        return self._monitor

    async def start(self) -> list[asyncio.Task[None]]:
        self._monitor.register(self._bus)
        _logger.info("performance_runtime_started")
        return [asyncio.create_task(self._publish_loop(), name="performance-status")]

    async def stop(self) -> None:
        self._stop.set()
        _logger.info("performance_runtime_stopped")

    async def _publish_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._cache.set(
                    STATUS_KEY, self._monitor.snapshot(), ttl_seconds=_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("performance_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_INTERVAL_SECONDS)
            except TimeoutError:
                continue
