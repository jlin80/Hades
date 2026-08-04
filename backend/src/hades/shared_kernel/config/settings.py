"""Typed, validated configuration loaded exclusively from the environment.

Every knob the platform reads is declared here and documented in ``.env.example``.
No module ever hardcodes a secret or a threshold: it asks :func:`get_settings`.
Settings are grouped into nested models per concern so contexts depend only on
the slice they need (e.g. the Risk Manager takes ``settings.risk``).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, Field, PostgresDsn, RedisDsn, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class EventBusTransport(StrEnum):
    IN_MEMORY = "in_memory"
    REDIS = "redis"


# --- nested config models ----------------------------------------------------
# Each uses an env_prefix so `.env` keys map 1:1 without manual aliasing.


class _Section(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_file=".env")


class ApiSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="API_", extra="ignore", env_file=".env")

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    cors_origins: str = "http://localhost:5173"
    enable_docs: bool = True
    # Authentication scaffolding — disabled by default; wired for a later phase.
    auth_enabled: bool = False
    auth_api_key: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class PostgresSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore", env_file=".env")

    host: str = "postgres"
    port: int = 5432
    db: str = "hades"
    user: str = "hades"
    password: str = "change_me_in_env"
    pool_size: int = 10
    max_overflow: int = 20

    def dsn(self) -> str:
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                path=self.db,
            )
        )


class RedisSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore", env_file=".env")

    host: str = "redis"
    port: int = 6379
    db: int = 0
    password: str = ""

    def dsn(self) -> str:
        return str(
            RedisDsn.build(
                scheme="redis",
                host=self.host,
                port=self.port,
                path=str(self.db),
                password=self.password or None,
            )
        )


class EventBusSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="EVENT_BUS_", extra="ignore", env_file=".env")

    transport: EventBusTransport = EventBusTransport.REDIS
    stream_prefix: str = "hades.events"
    #: Approximate cap on the event stream. Without it the stream grows forever:
    #: a live deployment reached 172k entries and 155 MB of Redis with no
    #: `maxmemory` set, which ends as an eviction or an OOM rather than as a
    #: warning. ``~`` trimming lets Redis drop whole nodes, so it is cheap.
    stream_max_len: int = 250_000
    #: Consumer lag (undelivered entries) above which the bus logs a warning.
    #: A backlogged consumer is the most dangerous silent state this platform has:
    #: every component reports healthy while the decision path judges tokens from
    #: hours ago. One deployment ran 59 hours behind and nothing said so.
    lag_warn_threshold: int = 5_000
    #: How often the consumer checks its own lag, in seconds.
    lag_check_interval_seconds: float = 60.0
    #: How long a delivered-but-unacked message may sit before another consumer
    #: takes it over. A container killed mid-dispatch leaves its in-flight
    #: messages assigned to a consumer name that will never ack them, and nothing
    #: redelivers those on its own — `XREADGROUP >` returns only new entries.
    #: One deployment had accumulated 765 such orphans across restarts.
    reclaim_after_seconds: float = 300.0


class ClickHouseSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="CLICKHOUSE_", extra="ignore", env_file=".env")

    enabled: bool = False
    host: str = "clickhouse"
    port: int = 9000
    db: str = "hades_analytics"
    user: str = "hades"
    password: str = ""


class ObservabilitySettings(_Section):
    model_config = SettingsConfigDict(extra="ignore", env_file=".env")

    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")
    metrics_port: int = Field(default=9100, alias="METRICS_PORT")
    tracing_enabled: bool = Field(default=False, alias="TRACING_ENABLED")
    tracing_otlp_endpoint: str = Field(default="", alias="TRACING_OTLP_ENDPOINT")
    # Ship each process's log lines through Redis so the dashboard terminal shows
    # the whole platform and not just the API's own process. Fire-and-forget: a
    # Redis outage degrades to per-process logs, never to a stalled service.
    log_shipping_enabled: bool = Field(default=True, alias="LOG_SHIPPING_ENABLED")


class SolanaSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="SOLANA_", extra="ignore", env_file=".env")

    rpc_http_url: str = "https://api.mainnet-beta.solana.com"
    rpc_ws_url: str = "wss://api.mainnet-beta.solana.com"
    rpc_fallback_urls: str = ""
    commitment: str = "confirmed"
    request_timeout_seconds: int = 15


class RpcEndpointConfig(BaseModel):
    """A single Solana RPC provider the RPC Manager may route through.

    Providers are declared in config (never hardcoded). ``priority`` orders them
    (lower = preferred when health is equal); ``rate_limit_per_second`` caps
    outbound calls so a provider's quota is never blown; ``weight`` biases the
    health-weighted selection. ``ws_url`` is optional (only some providers /
    modules need a subscription socket).
    """

    name: str
    http_url: str
    ws_url: str = ""
    priority: int = 100
    rate_limit_per_second: int = 20
    weight: float = 1.0
    enabled: bool = True


class RpcSettings(_Section):
    """RPC Manager configuration — multi-provider, failover, health scoring.

    ``endpoints`` is a JSON array of :class:`RpcEndpointConfig` supplied via the
    ``RPC_ENDPOINTS`` env var. When empty, the manager falls back to the single
    ``SOLANA_RPC_HTTP_URL`` so the platform always has at least one provider.
    """

    model_config = SettingsConfigDict(env_prefix="RPC_", extra="ignore", env_file=".env")

    endpoints: list[RpcEndpointConfig] = Field(default_factory=list)

    @field_validator("endpoints", mode="before")
    @classmethod
    def _empty_env_value_means_no_endpoints(cls, value: object) -> object:
        # RPC_ENDPOINTS="" (unset in .env.example) is not valid JSON; pydantic-settings
        # only applies the field default when the env var is absent entirely, not when
        # it is present but blank. Treat blank the same as absent.
        if isinstance(value, str) and value.strip() == "":
            return []
        return value

    # Health scoring / failover tuning.
    request_timeout_seconds: float = 12.0
    max_attempts: int = 4  # across providers, per logical call
    latency_ewma_alpha: float = 0.3  # smoothing for the latency estimate
    unhealthy_after_failures: int = 3  # consecutive failures -> mark unhealthy
    recovery_probe_seconds: float = 30.0  # how often to re-probe an unhealthy one
    degraded_latency_ms: float = 1500.0  # above this latency a provider is "degraded"


class WalletSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="WALLET_", extra="ignore", env_file=".env")

    public_key: str = ""
    keypair_path: str = "/run/secrets/hades_wallet.json"
    max_sol_per_tx: float = 0.5

    @property
    def is_configured(self) -> bool:
        return bool(self.public_key)


class ScannerSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="SCANNER_", extra="ignore", env_file=".env")

    enabled: bool = True
    poll_interval_seconds: int = 5
    sources: str = "pumpfun,raydium,dexscreener"
    min_liquidity_usd: float = 5000
    max_token_age_minutes: int = 1440
    # Dedup: how long a mint stays "seen" so it never re-enters the pipeline.
    dedup_ttl_seconds: int = 86_400
    # Coarse gating lists (mints / deployer wallets). Comma-separated.
    blacklist_tokens: str = Field(default="", alias="BLACKLIST_TOKENS")
    blacklist_deployers: str = Field(default="", alias="BLACKLIST_DEPLOYERS")
    whitelist_tokens: str = Field(default="", alias="WHITELIST_TOKENS")
    # Per-source HTTP request timeout.
    source_timeout_seconds: float = 12.0
    # Per-source endpoint overrides, e.g.
    # ``SCANNER_SOURCE_URLS={"dexscreener": "https://my-bridge/dex"}``.
    # Each adapter ships a sensible default; this exists so pointing a source at a
    # mirror, a proxy or a replacement API is a config change instead of a code
    # edit. Unknown source names are ignored (the source simply is not built).
    source_urls: dict[str, str] = Field(default_factory=dict)

    @property
    def source_list(self) -> list[str]:
        return [s.strip() for s in self.sources.split(",") if s.strip()]

    @property
    def blacklisted_tokens(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.blacklist_tokens.split(",") if t.strip())

    @property
    def blacklisted_deployers(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.blacklist_deployers.split(",") if t.strip())

    @property
    def whitelisted_tokens(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.whitelist_tokens.split(",") if t.strip())


class PipelineSettings(_Section):
    """Acquisition-pipeline concurrency, queueing and backpressure.

    The pipeline processes each discovered token through ordered stages
    (metadata -> market -> features -> validation -> storage -> events). Bounded
    queues + a worker pool give parallelism without unbounded memory growth;
    when the queue is full the pipeline applies backpressure (drops the *oldest*
    pending, counted in a metric) so discovery never stalls the process.
    """

    model_config = SettingsConfigDict(env_prefix="PIPELINE_", extra="ignore", env_file=".env")

    workers: int = 4
    queue_max_size: int = 2000
    stage_timeout_seconds: float = 30.0
    # Periodic snapshots (History Builder) — one row per token per interval.
    snapshot_interval_seconds: int = 60
    snapshot_enabled: bool = True


class ScoringSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="SCORING_", extra="ignore", env_file=".env")

    min_security_score: float = 60
    min_wallet_score: float = 50
    min_final_score: float = 70
    model_registry_path: str = "/app/models"


class SecuritySettings(_Section):
    """Security Engine — the conservative rug/scam guardrail before scoring.

    The engine analyses risk only; it never trades. ``min_security_score`` is the
    approval gate (a token below it never reaches the AI Committee). The RPC-heavy
    features (live cluster lookups, exact holder count) are bounded and can be
    disabled to protect the RPC budget; when disabled the engine degrades to doubt
    rather than pretending the data is clean.
    """

    model_config = SettingsConfigDict(env_prefix="SECURITY_", extra="ignore", env_file=".env")

    enabled: bool = True
    min_security_score: float = 60.0
    # Analyzer thresholds.
    min_liquidity_usd: float = 5_000.0
    thin_liquidity_usd: float = 15_000.0
    min_secured_lp_pct: float = 80.0
    max_top1_pct: float = 25.0
    max_top10_pct: float = 60.0
    min_holders: int = 50
    max_rug_rate: float = 0.25
    # Aggregation / doubt handling.
    max_insufficient_analyzers: int = 3
    doubt_buffer: float = 15.0
    auto_blacklist_honeypots: bool = True
    # I/O budget (per analysed token).
    top_holders: int = 20
    fetch_holder_count: bool = False  # getProgramAccounts scan — off by default
    honeypot_enabled: bool = True
    honeypot_probe_usd: float = 50.0
    # The honeypot probe's quote route. Configurable because a third party can
    # retire an endpoint at any time, and when this one dies *every* token fails
    # its sellability check and nothing can ever be approved — that must be
    # fixable by an operator editing .env, not by cutting a release. The literal
    # is repeated from ``security.infrastructure.swap_simulator`` rather than
    # imported: shared_kernel must not depend on a context.
    honeypot_quote_url: str = "https://lite-api.jup.ag/swap/v1/quote"
    cluster_live_lookups: bool = True  # bounded funding-graph RPC lookups
    cluster_max_holders: int = 12
    cluster_min_members: int = 3


class RiskSettings(_Section):
    """The Risk Manager - the only component authorised to approve a trade.

    Conservative by default (survive first, earn second). Every limit is
    overridable via ``RISK_*`` env vars. The Risk Manager builds its immutable
    ``RiskConfig`` from this section plus the position/security sections.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", extra="ignore", env_file=".env")

    enabled: bool = True
    # Position count / capital / sizing.
    max_concurrent_positions: int = 5
    max_portfolio_exposure_pct: float = 50
    max_position_size_usd: float = 100
    min_position_size_usd: float = 0.05
    liquidity_reserve_pct: float = 35.0  # never commit the whole balance
    risk_per_trade_pct: float = 2.0  # % of equity risked at maximum conviction
    # Signal-quality gates.
    min_prob_roi_positive: float = 0.55
    min_confidence: float = 0.40
    min_developer_score: float = 40.0
    max_wallet_risk: float = 70.0
    min_liquidity_usd: float = 5_000.0
    # Exposure caps (percentage of equity).
    max_exposure_per_token_pct: float = 20.0
    max_exposure_per_developer_pct: float = 25.0
    max_exposure_per_cluster_pct: float = 30.0
    max_exposure_per_strategy_pct: float = 40.0
    max_exposure_per_narrative_pct: float = 35.0
    max_exposure_per_regime_pct: float = 60.0
    max_correlated_positions: int = 3
    # Trade-rate caps.
    max_trades_per_hour: int = 20
    max_trades_per_hour_per_strategy: int = 8
    # Drawdown ceilings.
    daily_max_loss_usd: float = 200
    daily_max_loss_pct: float = 10.0
    weekly_max_loss_pct: float = 20.0
    monthly_max_loss_pct: float = 35.0
    daily_max_stop_losses: int = 8
    # Kill Switch (graduated).
    kill_switch_enabled: bool = True
    consecutive_losses_reduce: int = 3
    consecutive_losses_halt: int = 5
    halt_minutes: int = 60
    size_reduction_factor: float = 0.5
    drawdown_observation_pct: float = 10.0
    drawdown_paper_only_pct: float = 20.0
    drawdown_emergency_pct: float = 30.0
    # Circuit Breaker.
    circuit_breaker_enabled: bool = True
    max_consecutive_errors: int = 5
    max_rpc_latency_ms: int = 3000
    circuit_breaker_cooldown_seconds: int = 300
    # Per-strategy risk budget as "name:pct,name:pct" (empty -> no per-strategy cap).
    risk_budget_map: str = ""

    @property
    def budget_allocations(self) -> dict[str, float]:
        """Parse ``risk_budget_map`` into a strategy -> percentage mapping."""
        out: dict[str, float] = {}
        for pair in self.risk_budget_map.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, pct = pair.partition(":")
            try:
                out[name.strip().lower()] = float(pct)
            except ValueError:
                continue
        return out


class PositionSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="POSITION_", extra="ignore", env_file=".env")

    default_take_profit_pct: float = 40
    default_stop_loss_pct: float = 20
    trailing_enabled: bool = True
    trailing_activation_pct: float = 25
    trailing_distance_pct: float = 10


class MarketSettings(_Section):
    """The price feed that marks open positions to market.

    Without a price oracle the Paper Executor falls back to a unit price
    (quantity == USD), which keeps *costs* honest but makes every entry price
    1.0 — and a take-profit measured against 1.0 is meaningless. This section
    wires a real one.

    ``price_source_url`` is the batch token endpoint a mint list is appended to;
    the parser expects DexScreener's payload shape. Point it at a mirror or a
    proxy freely, but a substitute must return that shape or the oracle simply
    reports no price (and the platform degrades, it does not break).
    """

    model_config = SettingsConfigDict(env_prefix="MARKET_", extra="ignore", env_file=".env")

    price_oracle_enabled: bool = True
    price_source_url: str = "https://api.dexscreener.com/latest/dex/tokens/"
    # A quote is reused for this long before the oracle re-fetches. Position
    # monitoring polls far more often than prices meaningfully move, and the
    # upstream endpoint is unauthenticated and rate-limited.
    price_cache_ttl_seconds: float = 15.0
    price_timeout_seconds: float = 8.0
    # DexScreener accepts up to 30 comma-separated mints per request.
    price_batch_size: int = 30


class ExecutionSettings(_Section):
    """Execution Engine tuning — shared by the paper and live executors.

    ``enabled`` gates the whole engine; ``slippage_bps`` is the base tolerance the
    dynamic Slippage Engine widens from; ``max_slippage_bps`` is the per-order
    budget an order is cancelled for exceeding. Retry/confirmation knobs bound the
    live path. Live execution additionally requires the hard ``live_trading``
    gate — these settings never enable live on their own.
    """

    model_config = SettingsConfigDict(env_prefix="EXECUTION_", extra="ignore", env_file=".env")

    enabled: bool = True
    slippage_bps: int = 100  # base/floor tolerance on a healthy pool
    max_slippage_bps: int = 300  # per-order budget; cancel if exceeded
    hard_max_slippage_bps: int = 500  # absolute ceiling regardless of order
    priority_fee_microlamports: int = 50000
    confirmation_timeout_seconds: int = 30
    confirmation_poll_seconds: float = 1.0
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.5
    dex_fee_bps: int = 25
    sol_price_usd: float = 150.0
    jito_enabled: bool = False
    jito_tip_microlamports: int = 0
    # Quote mint the live path trades the token against (USDC by default).
    quote_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    # How often the runtime publishes its execution status snapshot to Redis.
    status_interval_seconds: float = 5.0
    # The Position Monitor: marks open positions to market and fires the
    # approved take-profit / stop-loss / trailing exits. Disabling it means
    # positions are opened and never closed — leave it on.
    position_monitor_enabled: bool = True
    position_monitor_interval_seconds: float = 5.0
    # The instrumented fast-path adapter (docs/EXECUTION_FAST_PATH_2026-08-04.md).
    # OFF by default and, even when on, only built if the hard live gate is
    # already open — it is a live adapter, not a second paper mode. Turning this
    # on swaps which live adapter the engine routes to; it never enables live.
    fast_path_enabled: bool = False
    # Shadow mode: run a candidate adapter alongside the real one and record the
    # comparison. OFF by default, and inert without a candidate adapter to
    # compare against. A live adapter can never be the candidate — see
    # ShadowExecutor, which refuses it outright rather than behind a flag.
    shadow_enabled: bool = False
    shadow_timeout_seconds: float = 30.0


class PaperSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="PAPER_", extra="ignore", env_file=".env")

    starting_balance_usd: float = 1000
    simulated_latency_ms: int = 250
    simulated_slippage_bps: int = 80


class NotificationSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="NOTIFY_", extra="ignore", env_file=".env")

    discord_enabled: bool = False
    discord_webhook_url: str = ""
    min_severity: str = "info"
    # Optional dedicated webhooks per severity/topic (fall back to the main one).
    discord_alerts_webhook_url: str = ""
    discord_trades_webhook_url: str = ""
    # Delivery robustness.
    retry_attempts: int = 3
    rate_limit_per_minute: int = 30
    timeout_seconds: int = 10
    #: How long an identical alert is suppressed after being delivered. The same
    #: situation re-detected on every probe tick is one situation, not news
    #: repeated every few seconds — and an operator who learns the channel is
    #: noise stops reading the alert that matters. Set to 0 to disable.
    dedup_window_seconds: float = 300.0


class WatchdogSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="WATCHDOG_", extra="ignore", env_file=".env")

    enabled: bool = True
    heartbeat_interval_seconds: int = 10
    unhealthy_after_missed_beats: int = 3
    # Health-probe cadence and resource ceilings the watchdog alerts on.
    probe_interval_seconds: int = 15
    cpu_max_pct: float = 90.0
    memory_max_pct: float = 90.0
    disk_max_pct: float = 90.0
    rpc_latency_max_ms: float = 2000.0
    # Endpoints the watchdog probes over HTTP (Docker service DNS names).
    api_health_url: str = "http://api:8000/health"
    dashboard_url: str = "http://dashboard:5173/"
    # Background services whose liveness files the watchdog verifies.
    watched_roles: str = "worker,engine,scheduler,notification"
    # Liveness files written by background services; the healthcheck reads them.
    liveness_dir: str = "/var/run/hades"
    liveness_max_age_seconds: int = 60
    #: How long a consumer group may go without touching the event stream before
    #: the Watchdog calls it unhealthy. A live consumer blocks for at most a
    #: couple of seconds per read, so anything past a few minutes means the loop
    #: is gone — the Worker's once sat idle for four days while every other probe
    #: stayed green.
    event_bus_max_idle_seconds: float = 300.0

    @property
    def watched_role_list(self) -> list[str]:
        return [r.strip() for r in self.watched_roles.split(",") if r.strip()]


class DashboardSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="DASHBOARD_", extra="ignore", env_file=".env")

    host: str = "0.0.0.0"
    port: int = 5173
    title: str = "Hades Control Center"
    ws_path: str = "/ws"
    refresh_interval_ms: int = 5000


class SchedulerSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", extra="ignore", env_file=".env")

    enabled: bool = True
    tick_interval_seconds: int = 5
    health_probe_interval_seconds: int = 15
    backup_interval_hours: int = 24
    retrain_interval_hours: int = 24
    cleanup_interval_hours: int = 6


class BackupSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="BACKUP_", extra="ignore", env_file=".env")

    enabled: bool = True
    directory: str = "/app/backups"
    interval_hours: int = 24
    retention_count: int = 14
    include_database: bool = True
    include_config: bool = True
    include_models: bool = True
    include_research: bool = True
    include_docs: bool = True
    include_logs: bool = False


class LoggingSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="LOG_", extra="ignore", env_file=".env")

    directory: str = "/var/log/hades"
    to_file: bool = True
    rotation_max_bytes: int = 50_000_000
    rotation_backups: int = 10
    # In-memory ring buffer size feeding the real-time terminal over WebSocket.
    ring_buffer_size: int = 2000


class FeatureSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="FEATURE_", extra="ignore", env_file=".env")

    enabled: bool = True
    store_path: str = "/app/datasets/features"
    batch_size: int = 256
    cache_ttl_seconds: int = 300
    # Feature-set schema version. Bumped when a feature's definition changes so
    # older vectors stay reconcilable (never break compatibility, keep versions).
    schema_version: int = 1
    # Rolling history window (number of snapshots) technical features look back on.
    history_window: int = 64


class LearningSettings(_Section):
    """AI Committee — the explainable quantitative brain.

    Runs a committee of transparent specialist models whose opinions a meta-model
    fuses into three calibrated probabilities (ROI+/TP/SL) plus a multi-factor
    confidence. It only *quantifies*; it never trades, sizes or enables live — the
    Risk Manager (a later context) is the sole decision-maker. Training is
    human/policy-gated and runs off the hot path; ``auto_train`` only builds
    *candidate* models, never promotes them.
    """

    model_config = SettingsConfigDict(env_prefix="LEARNING_", extra="ignore", env_file=".env")

    enabled: bool = True
    retrain_interval_hours: int = 24
    models_path: str = "/app/models"
    datasets_path: str = "/app/datasets"
    # AI Committee runtime.
    committee_enabled: bool = True
    shadow_enabled: bool = True
    # A shadow committee runs alongside the active one for comparison only.
    shadow_label: str = "shadow_baseline"
    # Scheduled training builds candidate models (never auto-promotes).
    auto_train: bool = False
    min_outcomes_to_train: int = 60
    # Confidence priors used before enough history accrues (0..1).
    default_dataset_quality: float = 0.5
    default_sample_support: float = 0.35
    # --- Candidate Enricher --------------------------------------------------
    # Every candidate is enriched from the Knowledge Engine before the committee
    # sees it. These knobs bound the work and the influence; none of them can
    # relax a threshold — with an empty memory the enrichment is exactly neutral
    # and the committee produces the number it would have produced anyway.
    #: Settled lessons considered per enrichment pass.
    enrichment_lesson_window: int = 5_000
    #: Seconds the lesson set is cached. Lessons arrive as trades settle; tokens
    #: arrive as the Scanner discovers them, which is orders of magnitude faster.
    enrichment_cache_seconds: float = 60.0
    #: Hard cap on how far history may shift the fused probabilities, in logits.
    #: History informs the committee; it must never overrule the token in front
    #: of it, because what a memory cannot know is what has changed since.
    enrichment_max_prior_log_odds: float = 1.0
    #: Pseudo-count pulling every cohort rate toward 0.5 (higher = more sceptical).
    enrichment_shrinkage: float = 8.0
    #: Cohort size below which a prior is reported but contributes nothing.
    enrichment_min_cohort: int = 3
    #: Comparable examples at which per-candidate ``sample_support`` saturates.
    enrichment_support_target: int = 60
    #: Size of the "similar past patterns" neighbourhood.
    enrichment_neighbours: int = 25


class KnowledgeSettings(_Section):
    """Knowledge — the platform's permanent, verifiable memory.

    It records what every other context learns and, crucially, pairs each
    decision with its realised outcome so the AI Committee finally has
    ground-truth training samples. It takes no decision, sizes nothing and has no
    concept of an order or a position.

    ``feed_committee`` is the switch that closes the learning loop: with it on,
    a completed lesson is written to the committee's outcome ledger. It defaults
    **on** because a memory that never reaches the brain is the defect this
    context was built to fix, and an operator who wants the memory without the
    learning can still say so.
    """

    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_", extra="ignore", env_file=".env")

    enabled: bool = True
    #: Forward completed lessons to the AI Committee's outcome ledger.
    feed_committee: bool = True
    #: Announce every accepted observation on the bus. Off: the Scanner is a
    #: firehose and nothing subscribes, so this is thousands of messages an hour
    #: consumed by no one.
    announce_observations: bool = False
    #: Retention floor for the read API's default query window, in days. The
    #: store itself is append-only and never trimmed here.
    default_query_days: int = 30
    #: Inbox for ``hades.knowledge/v1`` bundles produced by the external Hades
    #: Research Lab. Ingestion is a **pull**: with nobody sweeping, a bundle on
    #: disk does nothing. See docs/RESEARCH_LAB_BRIDGE.md.
    inbox: str = "/app/research/knowledge-inbox"
    #: Sweep the inbox automatically on the research runtime's schedule. Off by
    #: default — importing external material should be a deliberate act until an
    #: operator has decided they trust the producer feeding that directory.
    auto_ingest: bool = False


class ExplorationSettings(_Section):
    """Exploration — the budgeted, self-terminating answer to the cold start.

    This section does not exist to make money. It exists to buy the platform its
    first ground-truth samples, at a price fixed here in advance, during the only
    period in which the memory has none. Everything about it is a ceiling.

    It is **off by default** and stays off until an operator sets a budget they
    are content to lose, because that is what the programme spends it on: the
    trades are chosen for what they will teach, not for their expected return.

    Three properties are worth knowing before turning it on:

    * It waives **only** the AI Committee's conviction gates (probability and
      confidence). Every safety rule — security, developer, wallet, liquidity,
      kill switch, circuit breaker, drawdown, exposure, capital — applies in full
      and unchanged, and the Risk Manager remains the sole authoriser.
    * It **switches itself off**. When the memory holds ``target_lessons``
      settled trades with at least ``target_per_class`` on each side of zero, the
      programme latches off and announces it. No operator action is required for
      it to end.
    * Its size is **fixed**, not conviction-weighted: every exploration trade
      costs ``per_trade_usd``, so ``total_budget_usd`` states exactly how many
      trades the programme can ever fund.
    """

    model_config = SettingsConfigDict(env_prefix="EXPLORATION_", extra="ignore", env_file=".env")

    enabled: bool = False
    # -- the budget (four independent ceilings) --------------------------------
    #: Fixed size of every exploration trade. Deliberately not a maximum: a size
    #: that grew with conviction would reintroduce the belief the programme
    #: exists to test.
    per_trade_usd: float = 1.0
    daily_budget_usd: float = 10.0
    weekly_budget_usd: float = 40.0
    #: Lifetime ceiling. This one never resets, and it is the number that bounds
    #: what the whole programme can cost.
    total_budget_usd: float = 250.0
    max_trades_per_day: int = 10
    max_trades_per_week: int = 40
    # -- when it stops ---------------------------------------------------------
    target_lessons: int = 60
    #: Minimum settled trades on *each* side of zero. Not redundant with the
    #: total: 60 lessons that are all losses are a single-class dataset, whose
    #: AUC is undefined and against which no model can be validated.
    target_per_class: int = 15
    #: Settled lessons at which a cohort (developer / launchpad / narrative /
    #: cluster) stops being worth sampling. Drives selection, not shutdown.
    cohort_target: int = 8
    # -- which candidates it samples -------------------------------------------
    #: Floor: below this the candidate is not an open question, it is a bad one.
    min_prob_roi_positive: float = 0.35
    #: Ceiling: above this the production path judges the candidate on its own,
    #: and spending exploration budget there would flatter the programme with a
    #: trade the platform was going to make anyway.
    max_prob_roi_positive: float = 0.55
    min_confidence: float = 0.15
    # -- the risk envelope of an exploration trade -----------------------------
    #: Tighter than production by default: the point is to learn what happens,
    #: and a wide stop turns a cheap question into an expensive one.
    stop_loss_pct: float = 12.0
    take_profit_pct: float = 30.0
    status_interval_seconds: float = 10.0


class ResearchSettings(_Section):
    """Research Lab — the independent, offline R&D environment.

    The lab investigates on *copies* of history and only ever *proposes*: it can
    never execute a live trade, mutate a production strategy, or deploy a model.
    ``lab_enabled`` turns the worker runtime on; ``auto_research`` lets the
    scheduler run recurring studies/reports. None of these knobs can enable live
    trading — that gate lives elsewhere and the lab has no path to it.
    """

    model_config = SettingsConfigDict(env_prefix="RESEARCH_", extra="ignore", env_file=".env")

    # Research NEVER promotes to live automatically; this only enables the lab.
    lab_enabled: bool = Field(default=False, alias="RESEARCH_LAB_ENABLED")
    directory: str = Field(default="/app/research", alias="RESEARCH_DIR")
    datasets_path: str = Field(default="/app/datasets", alias="RESEARCH_DATASETS_PATH")
    # Shadow strategies run against the live feature stream (virtual, zero-capital).
    shadow_enabled: bool = True
    # The Auto Research Scheduler runs recurring studies/comparisons/reports.
    auto_research: bool = False
    min_samples: int = 200  # below this the lab defers heavy studies
    status_interval_seconds: float = 10.0
    shadow_publish_interval_seconds: float = 60.0
    discord_enabled: bool = True  # only important events; never minor churn
    # Promotion bar (a recommendation only — deployment is always a human action).
    promo_min_trades: int = 500
    promo_min_sharpe: float = 1.0
    promo_max_drawdown: float = 0.25
    promo_min_profit_factor: float = 1.3
    # Inbox for candidate bundles produced by the *external* Hades Research Lab
    # (its own repository and stack). An operator drops bundles here and runs the
    # importer; the lab itself has no write path into this process. Imported
    # candidates land as TRAINED — never promoted. See
    # ``hades.contexts.learning.domain.candidates``.
    candidate_inbox: str = Field(default="/app/research/inbox", alias="RESEARCH_CANDIDATE_INBOX")


class StrategySettings(_Section):
    """Strategy Engine — the modular set of quantitative strategies.

    Strategies only *detect opportunities*: they emit signals, never orders, and
    never bypass the Risk Manager. This section toggles the engine, tunes the
    ensemble fusion and lets operators enable/disable strategies and pass
    per-strategy parameters entirely from configuration (never by editing code).

    ``gate_risk`` is no longer forward-looking: it is the single switch that
    makes the Strategy Engine *decide*. With it on, the ensemble can

      * **veto an entry** the Risk Manager would otherwise approve
        (``EnsembleConsensusPolicy``, in the conviction tier), and
      * **request an exit** on a token already held, honoured by the Position
        Monitor on its next priced tick.

    Both directions only ever reduce exposure. The ensemble can never create an
    approval or open a position: the Risk Manager remains the sole authoriser and
    ``TradeApproved`` is still constructed in exactly one place.

    It defaults off. Turning it on changes what the platform trades, so it is a
    deliberate act — and note that a silent roster is not a dissenting one: with
    no strategy participating the policy passes, because reading silence as a
    veto would halt the platform at cold start while looking like caution.
    """

    model_config = SettingsConfigDict(env_prefix="STRATEGY_", extra="ignore", env_file=".env")

    enabled: bool = True
    shadow_enabled: bool = True
    # Ensemble fusion tuning.
    decision_deadband: float = 0.12  # |net conviction| must exceed this to act
    min_notify_confidence: float = 0.5  # only alert on ensembles at least this sure
    # Dynamic weighting.
    weight_floor: float = 0.05  # a weak strategy is muted this low, never to zero
    research_boost: float = 1.0  # Research-Lab multiplier applied to every weight
    # Failsafe.
    error_threshold: int = 3  # consecutive errors before a strategy is muted
    # Global default sensitivity dial (per-strategy params can override).
    sensitivity: float = 1.0
    # Comma-separated strategy names to force-disable at load.
    disabled: str = ""
    # JSON object of per-strategy parameter overrides:
    #   {"momentum_breakout": {"sensitivity": 1.2}, "launch_detection": {...}}
    params_json: str = Field(default="", alias="STRATEGY_PARAMS")
    # The single switch that lets the ensemble veto entries and request exits.
    gate_risk: bool = False
    #: Minimum ensemble conviction in [-1, 1] for a BUY consensus to stand when
    #: ``gate_risk`` is on. 0.0 means "any positive net conviction will do"; the
    #: deadband above already filters noise around zero.
    gate_min_ensemble_score: float = 0.0
    status_interval_seconds: float = 5.0

    @property
    def disabled_list(self) -> frozenset[str]:
        return frozenset(s.strip() for s in self.disabled.split(",") if s.strip())

    @property
    def per_strategy(self) -> dict[str, dict[str, object]]:
        """Parse ``STRATEGY_PARAMS`` into a name -> params mapping (empty on error)."""
        import json

        if not self.params_json.strip():
            return {}
        try:
            parsed = json.loads(self.params_json)
        except (ValueError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, dict[str, object]] = {}
        for name, params in parsed.items():
            if isinstance(name, str) and isinstance(params, dict):
                out[name] = dict(params)
        return out


class IntelligenceSettings(_Section):
    """Wallet Intelligence — the permanent on-chain knowledge base.

    Builds and grows a wallet-centric reputation graph from every token the
    Security Engine analyses. It only *knows*; it never trades, never enables
    live, and runs no ML. ``live_lookups`` gates the bounded funder RPC reads used
    for cluster detection; when off the engine still registers and profiles
    wallets, just without funding-based clustering.
    """

    model_config = SettingsConfigDict(env_prefix="INTELLIGENCE_", extra="ignore", env_file=".env")

    enabled: bool = True
    live_lookups: bool = True
    top_holders: int = 20
    max_funder_lookups: int = 8
    funders_per_wallet: int = 6
    # EWMA learning rate for evolving reputation (0..1; higher = faster to move).
    reputation_alpha: float = 0.25
    min_cluster_members: int = 3
    smart_money_threshold: float = 68.0
    min_score_for_smart: float = 75.0


class TimeoutSettings(_Section):
    model_config = SettingsConfigDict(env_prefix="TIMEOUT_", extra="ignore", env_file=".env")

    http_seconds: float = 15.0
    database_seconds: float = 10.0
    redis_seconds: float = 5.0
    rpc_seconds: float = 15.0
    websocket_seconds: float = 30.0
    shutdown_grace_seconds: float = 20.0


class PathsSettings(_Section):
    """Filesystem locations backed by Docker volumes (all survive restarts)."""

    model_config = SettingsConfigDict(env_prefix="PATH_", extra="ignore", env_file=".env")

    logs: str = "/var/log/hades"
    config: str = "/app/config"
    models: str = "/app/models"
    backups: str = "/app/backups"
    docs: str = "/app/docs"
    datasets: str = "/app/datasets"
    research: str = "/app/research"


# --- root settings -----------------------------------------------------------


class Settings(BaseSettings):
    """Root configuration aggregate. Access nested slices via attributes."""

    model_config = SettingsConfigDict(
        env_prefix="HADES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_format: str = "json"
    instance_id: str = "hades-local-1"
    timezone: str = "UTC"
    trading_mode: TradingMode = TradingMode.PAPER
    # Hard safety gate: live execution requires BOTH this flag AND mode == live.
    live_trading_enabled: bool = False

    # Nested sections are constructed from the environment by default_factory so
    # every slice is independently overridable.
    api: ApiSettings = Field(default_factory=ApiSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    event_bus: EventBusSettings = Field(default_factory=EventBusSettings)
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    solana: SolanaSettings = Field(default_factory=SolanaSettings)
    rpc: RpcSettings = Field(default_factory=RpcSettings)
    wallet: WalletSettings = Field(default_factory=WalletSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    position: PositionSettings = Field(default_factory=PositionSettings)
    market: MarketSettings = Field(default_factory=MarketSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    paper: PaperSettings = Field(default_factory=PaperSettings)
    notification: NotificationSettings = Field(default_factory=NotificationSettings)
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    feature: FeatureSettings = Field(default_factory=FeatureSettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    exploration: ExplorationSettings = Field(default_factory=ExplorationSettings)
    intelligence: IntelligenceSettings = Field(default_factory=IntelligenceSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        """Single authoritative predicate for "are we really trading live?".

        Both conditions must hold. The Execution Engine is the only component
        allowed to branch on this; everything upstream is mode-agnostic.
        """
        return self.trading_mode is TradingMode.LIVE and self.live_trading_enabled


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()
