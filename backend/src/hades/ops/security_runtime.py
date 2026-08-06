"""Security Engine runtime composition — assembles the guardrail subsystem.

Process-level wiring (the ops layer may import application + infrastructure). It
builds the whole Security graph from the container's collaborators and subscribes
it to the Feature Engine's ``FeaturesComputed`` event, so every token that is
measured is then screened:

    features ─FeaturesComputed─► [assembler → analyzers → scorer → engine] ─►
        SecurityScoreComputed / TokenApproved / TokenRejected

Nothing here decides a trade. It only starts the observer and a Redis status
publisher for the dashboard, and returns the tasks so the Worker can cancel them.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.security.application.analyzers import (
    AuthorityAnalyzer,
    BehaviorAnalyzer,
    ContractAnalyzer,
    DeveloperAnalyzer,
    HolderAnalyzer,
    HoneypotAnalyzer,
    LiquidityAnalyzer,
    PoolAnalyzer,
    TransactionAnalyzer,
    WalletClusterAnalyzer,
)
from hades.contexts.security.application.engine import SecurityEngine
from hades.contexts.security.application.lists import BlacklistEngine, WhitelistEngine
from hades.contexts.security.application.metrics import SecurityMetrics
from hades.contexts.security.application.scoring import SecurityScorer
from hades.contexts.security.application.subscriber import SecurityAnalysisHandler
from hades.contexts.security.domain.events import TokenApproved, TokenRejected
from hades.contexts.security.domain.ports import (
    ClusterDetector,
    DeveloperReputationStore,
    ListRegistry,
    SecurityCheck,
    SecurityRepository,
    TokenFactsSource,
)
from hades.contexts.security.infrastructure.assembler import SecurityContextAssembler
from hades.contexts.security.infrastructure.cluster_detector import (
    FundingGraphClusterDetector,
    NullClusterDetector,
)
from hades.contexts.security.infrastructure.developer_repository import (
    InMemoryDeveloperReputationStore,
    PostgresDeveloperReputationStore,
)
from hades.contexts.security.infrastructure.list_registry import (
    InMemoryListRegistry,
    PostgresListRegistry,
)
from hades.contexts.security.infrastructure.onchain_reader import RpcOnChainReader
from hades.contexts.security.infrastructure.security_repository import (
    InMemorySecurityRepository,
    PostgresSecurityRepository,
)
from hades.contexts.security.infrastructure.swap_simulator import (
    JupiterSwapSimulator,
    NullSwapSimulator,
)
from hades.contexts.security.infrastructure.token_facts import (
    InMemoryTokenFactsSource,
    PostgresTokenFactsSource,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events.bus import LaneBus
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.solana import RpcManager
from hades.shared_kernel.solana.rpc_manager import build_endpoints

_logger = get_logger("security.runtime")

#: Redis namespace + key the Worker publishes live security status under.
SECURITY_STATUS_NAMESPACE = "security"
SECURITY_STATUS_KEY = "status"
_STATUS_INTERVAL_SECONDS = 5.0
_STATUS_TTL_SECONDS = 30


class SecurityRuntime:
    """Owns the wired Security Engine subsystem and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._bus = LaneBus(container.event_bus, "security")
        self._stop = asyncio.Event()
        self._metrics = SecurityMetrics(container.metrics)
        self._status_cache = CacheService(container.redis, namespace=SECURITY_STATUS_NAMESPACE)
        # Rolling counters for the dashboard (Prometheus owns the source of truth).
        self._analyzed = 0
        self._approved = 0
        self._rejected = 0
        self._score_sum = 0.0
        self._rpc = self._build_rpc()
        self._swap = self._build_swap()
        self._engine = self._build_engine()
        self._assembler = self._build_assembler()
        self._register()

    # -- construction ---------------------------------------------------------

    def _build_rpc(self) -> RpcManager:
        s = self._c.settings
        endpoints = build_endpoints(s.rpc, s.solana.rpc_http_url)
        return RpcManager(endpoints, settings=s.rpc, metrics=self._c.metrics)

    def _build_swap(self) -> JupiterSwapSimulator | NullSwapSimulator:
        s = self._c.settings.security
        if s.honeypot_enabled:
            return JupiterSwapSimulator(base_url=s.honeypot_quote_url)
        return NullSwapSimulator()

    def _list_registry(self) -> ListRegistry:
        if self._c.database is not None:
            return PostgresListRegistry(self._c.database)
        return InMemoryListRegistry()

    def _developer_store(self) -> DeveloperReputationStore:
        if self._c.database is not None:
            return PostgresDeveloperReputationStore(self._c.database)
        return InMemoryDeveloperReputationStore()

    def _repository(self) -> SecurityRepository:
        if self._c.database is not None:
            return PostgresSecurityRepository(self._c.database)
        return InMemorySecurityRepository()

    def _facts_source(self) -> TokenFactsSource:
        if self._c.database is not None:
            return PostgresTokenFactsSource(self._c.database)
        return InMemoryTokenFactsSource()

    def _cluster_detector(self) -> ClusterDetector:
        sec = self._c.settings.security
        if sec.cluster_live_lookups:
            return FundingGraphClusterDetector(
                RpcOnChainReader(self._rpc),
                max_holders=sec.cluster_max_holders,
                min_members=sec.cluster_min_members,
            )
        return NullClusterDetector()

    def _build_analyzers(self) -> tuple[SecurityCheck, ...]:
        sec = self._c.settings.security
        return (
            ContractAnalyzer(),
            AuthorityAnalyzer(),
            LiquidityAnalyzer(
                min_liquidity_usd=sec.min_liquidity_usd,
                thin_liquidity_usd=sec.thin_liquidity_usd,
                min_secured_lp_pct=sec.min_secured_lp_pct,
            ),
            PoolAnalyzer(),
            HolderAnalyzer(
                max_top1_pct=sec.max_top1_pct,
                max_top10_pct=sec.max_top10_pct,
                min_holders=sec.min_holders,
            ),
            HoneypotAnalyzer(),
            DeveloperAnalyzer(max_rug_rate=sec.max_rug_rate),
            WalletClusterAnalyzer(),
            TransactionAnalyzer(),
            BehaviorAnalyzer(),
        )

    def _build_engine(self) -> SecurityEngine:
        sec = self._c.settings.security
        scorer = SecurityScorer(
            min_security_score=sec.min_security_score,
            max_insufficient_analyzers=sec.max_insufficient_analyzers,
            doubt_buffer=sec.doubt_buffer,
        )
        registry = self._list_registry()
        return SecurityEngine(
            analyzers=self._build_analyzers(),
            scorer=scorer,
            blacklist=BlacklistEngine(registry),
            whitelist=WhitelistEngine(registry),
            repository=self._repository(),
            developer_store=self._developer_store(),
            event_bus=self._bus,
            metrics=self._metrics,
            auto_blacklist_honeypots=sec.auto_blacklist_honeypots,
        )

    def _build_assembler(self) -> SecurityContextAssembler:
        sec = self._c.settings.security
        return SecurityContextAssembler(
            reader=RpcOnChainReader(self._rpc),
            facts_source=self._facts_source(),
            swap_simulator=self._swap,
            developer_store=self._developer_store(),
            cluster_detector=self._cluster_detector(),
            top_holders=sec.top_holders,
            honeypot_probe_usd=sec.honeypot_probe_usd,
            fetch_holder_count=sec.fetch_holder_count,
        )

    def _register(self) -> None:
        SecurityAnalysisHandler(self._engine, self._assembler).register(self._bus)
        self._bus.subscribe(TokenApproved.__name__, self._on_approved)
        self._bus.subscribe(TokenRejected.__name__, self._on_rejected)

    # -- dashboard stats ------------------------------------------------------

    async def _on_approved(self, event: DomainEvent) -> None:
        if isinstance(event, TokenApproved):
            self._analyzed += 1
            self._approved += 1
            self._score_sum += event.score

    async def _on_rejected(self, event: DomainEvent) -> None:
        if isinstance(event, TokenRejected):
            self._analyzed += 1
            self._rejected += 1
            self._score_sum += event.score

    def snapshot(self) -> dict[str, Any]:
        """Live status for the dashboard (analysis only — never trade info)."""
        avg = self._score_sum / self._analyzed if self._analyzed else 0.0
        return {
            "enabled": self._c.settings.security.enabled,
            "analyzed_total": self._analyzed,
            "approved_total": self._approved,
            "rejected_total": self._rejected,
            "approval_rate": (round(self._approved / self._analyzed, 3) if self._analyzed else 0.0),
            "avg_security_score": round(avg, 2),
            "min_security_score": self._c.settings.security.min_security_score,
            "honeypot_enabled": self._c.settings.security.honeypot_enabled,
            "cluster_live_lookups": self._c.settings.security.cluster_live_lookups,
            "rpc_active": self._rpc.active_endpoint,
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._status_cache.set(
                    SECURITY_STATUS_KEY, self.snapshot(), ttl_seconds=_STATUS_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("security_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STATUS_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        status_task = asyncio.create_task(self._publish_status_loop(), name="security-status")
        _logger.info("security_runtime_started")
        return [status_task]

    async def stop(self) -> None:
        self._stop.set()
        await self._rpc.aclose()
        if isinstance(self._swap, JupiterSwapSimulator):
            await self._swap.aclose()
        _logger.info("security_runtime_stopped")
