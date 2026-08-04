"""Scanner runtime composition — assembles the acquisition subsystem.

This is process-level wiring (the ops layer may import application +
infrastructure). It builds the whole data-acquisition graph from the container's
collaborators and starts it inside the Worker service:

    RPC Manager -> DEX sources -> Discovery Engine -> Acquisition Pipeline
                                                          |
                          Feature Engine + History Builder (react to events)

Nothing here decides anything; it only starts the observers and returns the tasks
so the Worker's lifecycle can cancel them on shutdown.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.features.application.feature_engine import FeatureEngine
from hades.contexts.features.application.snapshot_builder import HistoryBuilder
from hades.contexts.features.application.subscriber import FeatureComputationHandler
from hades.contexts.features.domain.ports import FeatureStore, SnapshotStore
from hades.contexts.features.infrastructure.assembler import HintInputsAssembler
from hades.contexts.features.infrastructure.feature_store import (
    CachingFeatureStore,
    InMemoryFeatureStore,
    PostgresFeatureStore,
)
from hades.contexts.features.infrastructure.snapshot_repository import (
    InMemorySnapshotRepository,
    SnapshotRepository,
)
from hades.contexts.scanner.application.discovery_engine import DiscoveryEngine
from hades.contexts.scanner.application.metadata_collector import MetadataCollector
from hades.contexts.scanner.application.metrics import ScannerMetrics
from hades.contexts.scanner.application.pipeline import AcquisitionPipeline
from hades.contexts.scanner.application.quality_validator import QualityValidator
from hades.contexts.scanner.domain.events import RpcEndpointSwitched
from hades.contexts.scanner.domain.ports import DiscoveryRepository as DiscoveryRepositoryPort
from hades.contexts.scanner.domain.ports import MetadataProvider
from hades.contexts.scanner.infrastructure.metadata_providers import (
    DexScreenerMetadataProvider,
    RpcMetadataProvider,
)
from hades.contexts.scanner.infrastructure.repository import (
    DiscoveryRepository,
    InMemoryDiscoveryRepository,
)
from hades.contexts.scanner.infrastructure.seen_registry import RedisSeenTokenRegistry
from hades.contexts.scanner.infrastructure.sources import build_sources
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.solana import RpcManager, RpcSwitch
from hades.shared_kernel.solana.rpc_manager import build_endpoints

_logger = get_logger("scanner.runtime")

#: Redis namespace + key the Worker publishes live scanner status under, and the
#: API reads from for the dashboard (transient — always TTL'd).
SCANNER_STATUS_NAMESPACE = "scanner"
SCANNER_STATUS_KEY = "status"
_STATUS_INTERVAL_SECONDS = 5.0
_STATUS_TTL_SECONDS = 30


class ScannerRuntime:
    """Owns the wired acquisition subsystem and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._metrics = ScannerMetrics(container.metrics)
        self._features_total = 0
        self._status_cache = CacheService(container.redis, namespace=SCANNER_STATUS_NAMESPACE)
        self._last_features = 0
        self._last_status_at = time.monotonic()
        self._rpc = self._build_rpc()
        self._pipeline = self._build_pipeline()
        self._discovery = self._build_discovery()
        self._wire_features()

    def _count_features(self, n: int) -> None:
        self._metrics.features_computed.inc(n)
        self._features_total += n

    def _observe_features_latency(self, seconds: float) -> None:
        """End-to-end discovered → features-ready, the figure §2.3 compares on."""
        self._metrics.discovered_to_features_seconds.observe(seconds)

    # -- construction ---------------------------------------------------------

    def _build_rpc(self) -> RpcManager:
        s = self._c.settings
        endpoints = build_endpoints(s.rpc, s.solana.rpc_http_url)
        bus = self._c.event_bus

        async def on_switch(switch: RpcSwitch) -> None:
            await bus.publish(
                RpcEndpointSwitched(
                    aggregate_id=new_id(),
                    previous=switch.previous,
                    current=switch.current,
                    reason=switch.reason,
                )
            )

        return RpcManager(
            endpoints,
            settings=s.rpc,
            metrics=self._c.metrics,
            on_switch=on_switch,
        )

    def _repository(self) -> DiscoveryRepositoryPort:
        if self._c.database is not None:
            return DiscoveryRepository(self._c.database)
        return InMemoryDiscoveryRepository()

    def _feature_store(self) -> FeatureStore:
        if self._c.database is None:
            return InMemoryFeatureStore()
        inner = PostgresFeatureStore(self._c.database)
        return CachingFeatureStore(
            inner, self._c.redis, ttl_seconds=self._c.settings.feature.cache_ttl_seconds
        )

    def _snapshot_store(self) -> SnapshotStore:
        if self._c.database is None:
            return InMemorySnapshotRepository()
        return SnapshotRepository(self._c.database)

    def _build_pipeline(self) -> AcquisitionPipeline:
        s = self._c.settings
        providers: list[MetadataProvider] = [
            RpcMetadataProvider(self._rpc),
            DexScreenerMetadataProvider(timeout_seconds=s.rpc.request_timeout_seconds),
        ]
        collector = MetadataCollector(providers, timeout_seconds=s.rpc.request_timeout_seconds)
        return AcquisitionPipeline(
            collector,
            QualityValidator(),
            self._repository(),
            settings=s.pipeline,
            event_bus=self._c.event_bus,
            metrics=self._metrics,
        )

    def _build_discovery(self) -> DiscoveryEngine:
        s = self._c.settings
        sources = build_sources(
            s.scanner.source_list,
            poll_interval_seconds=s.scanner.poll_interval_seconds,
            timeout_seconds=s.scanner.source_timeout_seconds,
            url_overrides=s.scanner.source_urls,
        )
        return DiscoveryEngine(
            sources,
            RedisSeenTokenRegistry(self._c.redis, ttl_seconds=s.scanner.dedup_ttl_seconds),
            self._pipeline.enqueue,
            settings=s.scanner,
            event_bus=self._c.event_bus,
            metrics=self._metrics,
        )

    def _wire_features(self) -> None:
        s = self._c.settings
        engine = FeatureEngine(
            self._feature_store(),
            event_bus=self._c.event_bus,
            schema_version=s.feature.schema_version,
            on_computed=self._count_features,
        )
        FeatureComputationHandler(
            engine, HintInputsAssembler(), on_latency=self._observe_features_latency
        ).register(self._c.event_bus)
        if s.pipeline.snapshot_enabled:
            HistoryBuilder(
                self._snapshot_store(), schema_version=s.feature.schema_version
            ).register(self._c.event_bus)

    # -- status ---------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Live status for the dashboard (no risk info — discovery only)."""
        now = time.monotonic()
        elapsed = max(1e-6, now - self._last_status_at)
        features_per_second = (self._features_total - self._last_features) / elapsed
        self._last_features = self._features_total
        self._last_status_at = now
        health = self._discovery.source_health()
        return {
            "rpc_active": self._rpc.active_endpoint,
            "rpc_endpoints": self._rpc.endpoints(),
            "queue_depth": self._pipeline.queue_depth(),
            "active_workers": self._pipeline.active_workers(),
            "worker_count": self._c.settings.pipeline.workers,
            "sources": health,
            "sources_available": sum(1 for up in health.values() if up),
            "sources_total": len(health),
            "features_total": self._features_total,
            "features_per_second": round(features_per_second, 3),
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._status_cache.set(
                    SCANNER_STATUS_KEY, self.snapshot(), ttl_seconds=_STATUS_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("scanner_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STATUS_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        await self._pipeline.start()
        discovery_task = asyncio.create_task(
            self._discovery.run(self._stop), name="scanner-discovery"
        )
        status_task = asyncio.create_task(self._publish_status_loop(), name="scanner-status")
        _logger.info("scanner_runtime_started", sources=self._c.settings.scanner.source_list)
        return [discovery_task, status_task]

    async def stop(self) -> None:
        self._stop.set()
        await self._pipeline.stop()
        await self._rpc.aclose()
        _logger.info("scanner_runtime_stopped")
