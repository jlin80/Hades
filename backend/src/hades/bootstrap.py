"""Composition root — the single place where the object graph is assembled.

Clean Architecture keeps construction out of the domain and application layers.
Here (and only here) do we read configuration, choose concrete adapters and wire
them into the buses. Everything downstream receives its collaborators via
constructor injection and depends on abstractions.

The :class:`Container` is intentionally explicit (no magic DI framework) so the
wiring is greppable and testable. Each process passes its ``role`` so the Redis
event bus consumes under a per-service consumer group (every service sees every
event). Contexts register their handlers/subscriptions through the ``register_*``
extension points as they are implemented in later phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hades.contexts.execution.domain.events import (
    OrderFailed,
    OrderFilled,
    OrderSubmitted,
    TradingModeChanged,
)
from hades.contexts.exploration.domain.events import (
    ExplorationBudgetExhausted,
    ExplorationCompleted,
    ExplorationGranted,
    ExplorationSpent,
)
from hades.contexts.features.domain.events import FeaturesComputed
from hades.contexts.intelligence.domain.events import (
    BehaviorChanged,
    ClusterCreated,
    FundingRelationshipFound,
    ReputationUpdated,
    SmartMoneyDetected,
    WalletIntelligenceComputed,
    WalletRegistered,
    WalletScoreUpdated,
    WalletUpdated,
)
from hades.contexts.knowledge.domain.events import (
    DecisionRecorded,
    KnowledgeRecorded,
    KnowledgeRejected,
    LessonLearned,
)
from hades.contexts.learning.domain.events import (
    CommitteeFinished,
    CommitteePredictionGenerated,
    ConfidenceCalculated,
    InferenceCompleted,
    ModelDriftDetected,
    ModelPromoted,
    ModelPromotionProposed,
    ModelRejected,
    ModelTrained,
    ModelValidated,
    PredictionGenerated,
)
from hades.contexts.monitoring.domain.events import (
    ComponentHeartbeat,
    HealthDegraded,
    HealthRecovered,
)
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.events import NotificationRequested
from hades.contexts.portfolio.domain.events import (
    CapitalCommitted,
    CapitalReleased,
    PortfolioUpdated,
    PositionClosed,
    PositionOpened,
    PositionUpdated,
    TrailingStopAdjusted,
)
from hades.contexts.research.domain.events import (
    BacktestCompleted,
    CandidateProposed,
    ExperimentFinished,
    ExperimentStarted,
    FeatureProposed,
    ModelCompared,
    MonteCarloCompleted,
    PromotionRejected,
    ReplayCompleted,
    ResearchReportGenerated,
    ResearchStrategyPromoted,
    ShadowStrategyUpdated,
    StrategyCompared,
    WalkForwardCompleted,
)
from hades.contexts.risk.domain.events import (
    CircuitBreakerReset,
    CircuitBreakerTripped,
    DrawdownLimitBreached,
    EmergencyModeEntered,
    EmergencyModeExited,
    ExposureLimitBreached,
    KillSwitchEngaged,
    KillSwitchLevelChanged,
    KillSwitchReset,
    RiskControlCommandIssued,
    RiskReduced,
    TradeApproved,
    TradeRejected,
)
from hades.contexts.scanner.domain.events import (
    DataQualityAnomalyDetected,
    PoolDiscovered,
    RpcEndpointSwitched,
    SignificantChangeDetected,
    SourceHealthChanged,
    TokenDiscovered,
    TokenMetadataCollected,
)
from hades.contexts.security.domain.events import (
    ClusterFound,
    ContractRiskDetected,
    DeveloperRisk,
    LiquidityWarning,
    SecurityAnalysisStarted,
    SecurityScoreComputed,
    TokenApproved,
    TokenRejected,
)
from hades.contexts.strategy.domain.events import (
    EnsembleSignalGenerated,
    ShadowActivated,
    SignalGenerated,
    SignalRejected,
    StrategyDisabled,
    StrategyError,
    StrategyLoaded,
    StrategyPromoted,
    WeightUpdated,
)
from hades.shared_kernel.analytics import ClickHouseProvider
from hades.shared_kernel.cache import RedisProvider
from hades.shared_kernel.config import Settings, get_settings
from hades.shared_kernel.config.settings import EventBusTransport
from hades.shared_kernel.cqrs import CommandBus, QueryBus
from hades.shared_kernel.events import (
    EventBus,
    EventRegistry,
    InMemoryEventBus,
    InMemoryEventStore,
    RedisEventBus,
)
from hades.shared_kernel.events.store import EventStore
from hades.shared_kernel.http import HttpClientProvider
from hades.shared_kernel.logging import configure_logging, get_logger
from hades.shared_kernel.observability import MetricsRegistry, get_metrics_registry
from hades.shared_kernel.persistence import Database


@dataclass
class Container:
    """Holds the wired singletons for the running process."""

    settings: Settings
    role: str
    metrics: MetricsRegistry
    event_bus: EventBus
    event_store: EventStore
    event_registry: EventRegistry
    command_bus: CommandBus
    query_bus: QueryBus
    redis: RedisProvider
    clickhouse: ClickHouseProvider
    notification: NotificationPublisher
    #: Shared outbound HTTP client. Callers such as the health probes are rebuilt
    #: per request, so the connection pool has to outlive them or every check
    #: pays a fresh TLS handshake and reports it as the dependency's latency.
    http: HttpClientProvider = field(default_factory=HttpClientProvider)
    database: Database | None = field(default=None)

    async def shutdown(self) -> None:
        if self.database is not None:
            await self.database.dispose()
        await self.redis.close()
        await self.http.close()
        self.clickhouse.close()


def _build_registry() -> EventRegistry:
    """Register every event that may cross the Redis transport boundary."""
    registry = EventRegistry()
    registry.register(
        NotificationRequested,
        HealthDegraded,
        HealthRecovered,
        ComponentHeartbeat,
        TradingModeChanged,
        # Execution Engine order lifecycle. These were published from the day the
        # engine shipped but never registered here, so under the Redis transport
        # they were dropped at the process boundary — `EventRegistry.rebuild`
        # returns None for an unknown type and the bus discards it. It went
        # unnoticed because their only consumers happened to live in the same
        # process; anything subscribing from another service (the Knowledge
        # memory now does) would simply never have heard a fill.
        OrderSubmitted,
        OrderFilled,
        OrderFailed,
        # Scanner / data-acquisition events.
        TokenDiscovered,
        PoolDiscovered,
        TokenMetadataCollected,
        SignificantChangeDetected,
        RpcEndpointSwitched,
        SourceHealthChanged,
        DataQualityAnomalyDetected,
        FeaturesComputed,
        # Security Engine events.
        SecurityAnalysisStarted,
        SecurityScoreComputed,
        TokenApproved,
        TokenRejected,
        ContractRiskDetected,
        LiquidityWarning,
        ClusterFound,
        DeveloperRisk,
        # Wallet Intelligence events.
        WalletRegistered,
        WalletUpdated,
        WalletScoreUpdated,
        ReputationUpdated,
        BehaviorChanged,
        SmartMoneyDetected,
        FundingRelationshipFound,
        ClusterCreated,
        WalletIntelligenceComputed,
        # Knowledge events (permanent memory; never a trade instruction).
        # LessonLearned crosses the transport because the AI Committee consumes
        # it to write ground truth into its outcome ledger — the return leg of
        # the learning loop, which must survive a multi-process deployment.
        KnowledgeRecorded,
        KnowledgeRejected,
        DecisionRecorded,
        LessonLearned,
        # Exploration events (the cold-start programme's public record). None
        # of these is a trade instruction and none can become one: the strongest
        # thing an ExplorationGranted says is that the Risk Manager was allowed
        # to *consider* a candidate under exploration rules.
        ExplorationGranted,
        ExplorationSpent,
        ExplorationBudgetExhausted,
        ExplorationCompleted,
        # AI Committee (Learning) events.
        InferenceCompleted,
        ConfidenceCalculated,
        CommitteeFinished,
        PredictionGenerated,
        CommitteePredictionGenerated,
        ModelTrained,
        ModelValidated,
        ModelRejected,
        ModelPromotionProposed,
        ModelPromoted,
        ModelDriftDetected,
        # Portfolio events (the Position stream + capital + portfolio updates).
        PositionOpened,
        PositionUpdated,
        TrailingStopAdjusted,
        PositionClosed,
        CapitalCommitted,
        CapitalReleased,
        PortfolioUpdated,
        # Risk Manager events (the guardian's decisions + defence layer).
        TradeApproved,
        TradeRejected,
        RiskReduced,
        KillSwitchLevelChanged,
        KillSwitchEngaged,
        KillSwitchReset,
        CircuitBreakerTripped,
        CircuitBreakerReset,
        EmergencyModeEntered,
        EmergencyModeExited,
        DrawdownLimitBreached,
        ExposureLimitBreached,
        RiskControlCommandIssued,
        # Research Lab events (knowledge only — never a trade instruction).
        ExperimentStarted,
        ExperimentFinished,
        BacktestCompleted,
        WalkForwardCompleted,
        MonteCarloCompleted,
        ReplayCompleted,
        ShadowStrategyUpdated,
        ModelCompared,
        StrategyCompared,
        FeatureProposed,
        CandidateProposed,
        ResearchStrategyPromoted,
        PromotionRejected,
        ResearchReportGenerated,
        # Strategy Engine events (signals + ensemble; never a trade instruction).
        StrategyLoaded,
        StrategyDisabled,
        StrategyError,
        ShadowActivated,
        StrategyPromoted,
        SignalGenerated,
        SignalRejected,
        WeightUpdated,
        EnsembleSignalGenerated,
    )
    return registry


def _build_event_bus(
    settings: Settings, role: str, redis: RedisProvider, registry: EventRegistry
) -> EventBus:
    """Select the event-bus transport from configuration.

    Redis Streams in real deployments (durable, multi-service; each service is
    its own consumer group so all services receive every event); in-memory for
    tests and single-process dev.
    """
    logger = get_logger("bootstrap")
    if settings.event_bus.transport is EventBusTransport.REDIS:
        logger.info("event_bus_selected", transport="redis", group=role)
        return RedisEventBus(
            redis,
            registry,
            stream_prefix=settings.event_bus.stream_prefix,
            group=role,
            consumer=settings.instance_id,
            max_len=settings.event_bus.stream_max_len,
            lag_warn_threshold=settings.event_bus.lag_warn_threshold,
            lag_check_interval_seconds=settings.event_bus.lag_check_interval_seconds,
            reclaim_after_seconds=settings.event_bus.reclaim_after_seconds,
        )
    logger.info("event_bus_selected", transport="in_memory")
    return InMemoryEventBus()


def build_container(settings: Settings | None = None, *, role: str = "app") -> Container:
    """Assemble the full object graph. Call once per process at startup."""
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        to_file=settings.logging.to_file,
        directory=settings.logging.directory,
        rotation_max_bytes=settings.logging.rotation_max_bytes,
        rotation_backups=settings.logging.rotation_backups,
        ring_buffer_size=settings.logging.ring_buffer_size,
    )
    logger = get_logger("bootstrap")

    metrics = get_metrics_registry()
    redis = RedisProvider(settings.redis.dsn(), timeout_seconds=settings.timeouts.redis_seconds)
    clickhouse = ClickHouseProvider(settings.clickhouse)
    registry = _build_registry()
    event_bus = _build_event_bus(settings, role, redis, registry)
    event_store: EventStore = InMemoryEventStore()  # Postgres-backed store: later phase
    command_bus = CommandBus()
    query_bus = QueryBus()
    notification = NotificationPublisher(event_bus)

    # The async engine is created eagerly (no connection is opened until a session
    # is used), so repositories/probes can share one Database.
    database = Database(
        settings.postgres.dsn(),
        pool_size=settings.postgres.pool_size,
        max_overflow=settings.postgres.max_overflow,
    )

    container = Container(
        settings=settings,
        role=role,
        metrics=metrics,
        event_bus=event_bus,
        event_store=event_store,
        event_registry=registry,
        command_bus=command_bus,
        query_bus=query_bus,
        redis=redis,
        clickhouse=clickhouse,
        notification=notification,
        http=HttpClientProvider(timeout_seconds=settings.timeouts.http_seconds),
        database=database,
    )

    logger.info(
        "container_built",
        role=role,
        env=settings.env,
        trading_mode=settings.trading_mode,
        is_live=settings.is_live,
        instance_id=settings.instance_id,
    )
    # Safety announcement: make the paper/live posture unmistakable in the logs.
    if not settings.is_live:
        logger.info("live_trading_disabled", note="running in PAPER mode (or gate closed)")
    else:
        logger.warning("live_trading_enabled", note="REAL orders will be placed")

    return container
