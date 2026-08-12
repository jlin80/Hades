"""Wallet Intelligence runtime composition — assembles the knowledge base.

Process-level wiring (the ops layer may import application + infrastructure). It
builds the whole intelligence graph from the container's collaborators and
subscribes it to the Security Engine's ``SecurityScoreComputed`` event, so every
token that is analysed feeds the wallet knowledge base:

    security ─SecurityScoreComputed─► [assembler → clustering → profiler → engine]
        ─► WalletRegistered / WalletProfileComputed / ClusterCreated /
           WalletIntelligenceComputed

Nothing here decides a trade or runs ML. It only starts the observer and a Redis
status publisher for the dashboard, and returns the tasks so the Worker can cancel
them.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.intelligence.application.behavior import BehaviorEngine
from hades.contexts.intelligence.application.clustering import ClusterBuilder
from hades.contexts.intelligence.application.engine import WalletIntelligenceEngine
from hades.contexts.intelligence.application.funding import FundingAnalyzer
from hades.contexts.intelligence.application.influence import InfluenceEngine
from hades.contexts.intelligence.application.metrics import IntelligenceMetrics
from hades.contexts.intelligence.application.profiler import WalletProfiler
from hades.contexts.intelligence.application.reputation import ReputationEngine
from hades.contexts.intelligence.application.scoring import WalletScorer
from hades.contexts.intelligence.application.smart_money import SmartMoneyEngine
from hades.contexts.intelligence.application.subscriber import WalletIntelligenceHandler
from hades.contexts.intelligence.domain.events import (
    ClusterCreated,
    WalletRegistered,
)
from hades.contexts.intelligence.domain.ports import (
    ClusterStore,
    KnowledgeBase,
    RelationshipStore,
    TimelineStore,
    WalletRepository,
)
from hades.contexts.intelligence.infrastructure.assembler import WalletObservationAssembler
from hades.contexts.intelligence.infrastructure.chain_reader import SecurityReaderAdapter
from hades.contexts.intelligence.infrastructure.stores import (
    InMemoryClusterStore,
    InMemoryKnowledgeBase,
    InMemoryRelationshipStore,
    InMemoryTimelineStore,
    PostgresClusterStore,
    PostgresKnowledgeBase,
    PostgresRelationshipStore,
    PostgresTimelineStore,
)
from hades.contexts.intelligence.infrastructure.wallet_repository import (
    InMemoryWalletRepository,
    PostgresWalletRepository,
)
from hades.contexts.security.infrastructure.onchain_reader import RpcOnChainReader
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.solana import RpcManager
from hades.shared_kernel.solana.rpc_manager import build_endpoints

_logger = get_logger("intelligence.runtime")

#: Redis namespace + key the Worker publishes live intelligence status under.
INTELLIGENCE_STATUS_NAMESPACE = "intelligence"
INTELLIGENCE_STATUS_KEY = "status"
_STATUS_INTERVAL_SECONDS = 5.0
_STATUS_TTL_SECONDS = 30


class IntelligenceRuntime:
    """Owns the wired Wallet Intelligence subsystem and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._metrics = IntelligenceMetrics(container.metrics)
        self._status_cache = CacheService(container.redis, namespace=INTELLIGENCE_STATUS_NAMESPACE)
        self._registered = 0
        self._clusters = 0
        self._rpc = self._build_rpc()
        self._engine = self._build_engine()
        self._assembler = self._build_assembler()
        self._register()

    # -- construction ---------------------------------------------------------

    def _build_rpc(self) -> RpcManager:
        s = self._c.settings
        endpoints = build_endpoints(s.rpc, s.solana.rpc_http_url, s.solana.rpc_fallback_urls)
        return RpcManager(endpoints, settings=s.rpc, metrics=self._c.metrics)

    def _repository(self) -> WalletRepository:
        if self._c.database is not None:
            return PostgresWalletRepository(self._c.database)
        return InMemoryWalletRepository()

    def _timeline(self) -> TimelineStore:
        if self._c.database is not None:
            return PostgresTimelineStore(self._c.database)
        return InMemoryTimelineStore()

    def _relationships(self) -> RelationshipStore:
        if self._c.database is not None:
            return PostgresRelationshipStore(self._c.database)
        return InMemoryRelationshipStore()

    def _cluster_store(self) -> ClusterStore:
        if self._c.database is not None:
            return PostgresClusterStore(self._c.database)
        return InMemoryClusterStore()

    def _knowledge_base(self) -> KnowledgeBase:
        if self._c.database is not None:
            return PostgresKnowledgeBase(self._c.database)
        return InMemoryKnowledgeBase()

    def _build_engine(self) -> WalletIntelligenceEngine:
        intel = self._c.settings.intelligence
        profiler = WalletProfiler(
            reputation=ReputationEngine(alpha=intel.reputation_alpha),
            behavior=BehaviorEngine(),
            smart_money=SmartMoneyEngine(
                smart_threshold=intel.smart_money_threshold,
            ),
            influence=InfluenceEngine(),
            scorer=WalletScorer(smart_band=intel.min_score_for_smart),
        )
        return WalletIntelligenceEngine(
            profiler=profiler,
            cluster_builder=ClusterBuilder(min_members=intel.min_cluster_members),
            funding_analyzer=FundingAnalyzer(),
            repository=self._repository(),
            timeline=self._timeline(),
            relationships=self._relationships(),
            clusters=self._cluster_store(),
            knowledge_base=self._knowledge_base(),
            event_bus=self._c.event_bus,
            metrics=self._metrics,
        )

    def _build_assembler(self) -> WalletObservationAssembler:
        intel = self._c.settings.intelligence
        reader = SecurityReaderAdapter(RpcOnChainReader(self._rpc))
        return WalletObservationAssembler(
            reader=reader,
            live_lookups=intel.live_lookups,
            top_holders=intel.top_holders,
            max_funder_lookups=intel.max_funder_lookups,
            funders_per_wallet=intel.funders_per_wallet,
        )

    def _register(self) -> None:
        WalletIntelligenceHandler(self._engine, self._assembler).register(self._c.event_bus)
        self._c.event_bus.subscribe(WalletRegistered.__name__, self._on_registered)
        self._c.event_bus.subscribe(ClusterCreated.__name__, self._on_cluster)

    # -- dashboard stats ------------------------------------------------------

    async def _on_registered(self, event: DomainEvent) -> None:
        if isinstance(event, WalletRegistered):
            self._registered += 1

    async def _on_cluster(self, event: DomainEvent) -> None:
        if isinstance(event, ClusterCreated):
            self._clusters += 1

    def snapshot(self) -> dict[str, Any]:
        """Live status for the dashboard (knowledge only — never trade info)."""
        return {
            "enabled": self._c.settings.intelligence.enabled,
            "wallets_registered_session": self._registered,
            "clusters_session": self._clusters,
            "live_lookups": self._c.settings.intelligence.live_lookups,
            "rpc_active": self._rpc.active_endpoint,
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._status_cache.set(
                    INTELLIGENCE_STATUS_KEY,
                    self.snapshot(),
                    ttl_seconds=_STATUS_TTL_SECONDS,
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("intelligence_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STATUS_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        status_task = asyncio.create_task(self._publish_status_loop(), name="intelligence-status")
        _logger.info("intelligence_runtime_started")
        return [status_task]

    async def stop(self) -> None:
        self._stop.set()
        await self._rpc.aclose()
        _logger.info("intelligence_runtime_stopped")
