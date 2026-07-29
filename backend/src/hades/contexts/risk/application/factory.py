"""Factory - assembles a fully-wired Risk Manager from a RiskConfig.

Keeps construction in one place so the manager stays free of wiring concerns and
tests can build a real manager in one call. The policy *order* is meaningful and
lives here: cheap, decisive quality gates first; sizing; then the allocation
rules that depend on the intended size.
"""

from __future__ import annotations

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.risk.application.circuit_breaker import CircuitBreaker
from hades.contexts.risk.application.kill_switch import KillSwitch
from hades.contexts.risk.application.manager import RiskManager
from hades.contexts.risk.application.metrics import RiskMetrics
from hades.contexts.risk.application.policies import (
    CapitalPolicy,
    CorrelationPolicy,
    DeveloperPolicy,
    DrawdownPolicy,
    EnsembleConsensusPolicy,
    ExposurePolicy,
    LiquidityPolicy,
    MaxPositionsPolicy,
    MinConfidencePolicy,
    MinProbabilityPolicy,
    RiskBudgetPolicy,
    SecurityPolicy,
    TradeRatePolicy,
    WalletPolicy,
)
from hades.contexts.risk.application.sizing import PositionSizingEngine
from hades.contexts.risk.domain.models import (
    CircuitBreakerConfig,
    DrawdownLimits,
    ExposureLimits,
    KillSwitchConfig,
    RateLimits,
    RiskBudget,
    RiskConfig,
    SizingConfig,
)
from hades.contexts.risk.domain.ports import (
    ExplorationPort,
    RiskAuditStore,
    RiskPolicy,
    RiskStateStore,
)
from hades.shared_kernel.config import Settings
from hades.shared_kernel.events import EventBus


def risk_config_from_settings(settings: Settings) -> RiskConfig:
    """Assemble the immutable :class:`RiskConfig` from the process settings."""
    r = settings.risk
    p = settings.position
    return RiskConfig(
        sizing=SizingConfig(
            risk_per_trade_pct=r.risk_per_trade_pct,
            max_position_size_usd=r.max_position_size_usd,
            min_position_size_usd=r.min_position_size_usd,
            stop_loss_pct=p.default_stop_loss_pct,
            take_profit_pct=p.default_take_profit_pct,
            trailing_enabled=p.trailing_enabled,
            trailing_activation_pct=p.trailing_activation_pct,
            trailing_distance_pct=p.trailing_distance_pct,
            min_prob_roi_positive=r.min_prob_roi_positive,
            min_confidence=r.min_confidence,
            min_security_score=settings.security.min_security_score,
        ),
        exposure=ExposureLimits(
            max_portfolio_pct=r.max_portfolio_exposure_pct,
            per_token_pct=r.max_exposure_per_token_pct,
            per_developer_pct=r.max_exposure_per_developer_pct,
            per_cluster_pct=r.max_exposure_per_cluster_pct,
            per_strategy_pct=r.max_exposure_per_strategy_pct,
            per_narrative_pct=r.max_exposure_per_narrative_pct,
            per_regime_pct=r.max_exposure_per_regime_pct,
        ),
        drawdown=DrawdownLimits(
            daily_pct=r.daily_max_loss_pct,
            weekly_pct=r.weekly_max_loss_pct,
            monthly_pct=r.monthly_max_loss_pct,
            daily_max_stop_losses=r.daily_max_stop_losses,
            daily_max_loss_usd=r.daily_max_loss_usd,
        ),
        rates=RateLimits(
            max_concurrent_positions=r.max_concurrent_positions,
            max_trades_per_hour=r.max_trades_per_hour,
            max_trades_per_hour_per_strategy=r.max_trades_per_hour_per_strategy,
            max_correlated_positions=r.max_correlated_positions,
        ),
        kill_switch=KillSwitchConfig(
            enabled=r.kill_switch_enabled,
            consecutive_losses_reduce=r.consecutive_losses_reduce,
            consecutive_losses_halt=r.consecutive_losses_halt,
            halt_minutes=r.halt_minutes,
            size_reduction_factor=r.size_reduction_factor,
            dd_observation_pct=r.drawdown_observation_pct,
            dd_paper_only_pct=r.drawdown_paper_only_pct,
            dd_emergency_pct=r.drawdown_emergency_pct,
        ),
        circuit_breaker=CircuitBreakerConfig(
            enabled=r.circuit_breaker_enabled,
            max_consecutive_errors=r.max_consecutive_errors,
            max_rpc_latency_ms=r.max_rpc_latency_ms,
            cooldown_seconds=r.circuit_breaker_cooldown_seconds,
        ),
        budget=RiskBudget(allocations=r.budget_allocations),
        min_developer_score=r.min_developer_score,
        max_wallet_risk=r.max_wallet_risk,
        min_liquidity_usd=r.min_liquidity_usd,
        gate_on_ensemble=settings.strategy.gate_risk,
        min_ensemble_score=settings.strategy.gate_min_ensemble_score,
    )


def build_risk_manager(
    config: RiskConfig,
    *,
    exploration: ExplorationPort | None = None,
    event_bus: EventBus | None = None,
    audit: RiskAuditStore | None = None,
    state_store: RiskStateStore | None = None,
    notifier: NotificationPublisher | None = None,
    metrics: RiskMetrics | None = None,
) -> RiskManager:
    """Wire every engine and policy into a ready-to-use Risk Manager.

    The membership of the two pre-sizing tuples is the security boundary of the
    exploration programme, and it is decided *here*, once, in a file whose whole
    job is composition:

    * **safety** — is this token fit to touch at any size at all? A rug check, a
      developer with a history, a wallet cluster that looks like a setup, a pool
      too thin to exit. No grant, no budget and no configuration can waive one of
      these, because the manager only consults the *other* tuple when deciding
      what an exploration grant may cover.
    * **conviction** — is the opportunity good enough to back with real size?
      Probability and confidence: the two gates that are, by construction,
      unpassable while the memory is empty, and therefore exactly the deadlock
      exploration exists to break.

    Splitting them costs nothing at runtime (the chain runs in the same order it
    always did) and buys the one property that matters: a rule added to the
    safety tuple next year is protected from exploration by default, without
    anyone having to remember that exploration exists.
    """
    safety: tuple[RiskPolicy, ...] = (
        SecurityPolicy(config.sizing),
        DeveloperPolicy(config.min_developer_score),
        WalletPolicy(config.max_wallet_risk),
        LiquidityPolicy(config.min_liquidity_usd),
    )
    conviction: tuple[RiskPolicy, ...] = (
        MinProbabilityPolicy(config.sizing),
        MinConfidencePolicy(config.sizing),
    )
    if config.gate_on_ensemble:
        # Appended rather than inserted: probability and confidence are the
        # cheaper checks and stay first, so the common rejection still costs one
        # comparison. Conviction tier, so exploration may waive it.
        conviction = (*conviction, EnsembleConsensusPolicy(config.min_ensemble_score))
    allocation: tuple[RiskPolicy, ...] = (
        MaxPositionsPolicy(config.rates),
        CapitalPolicy(),
        DrawdownPolicy(config.drawdown),
        ExposurePolicy(config.exposure),
        CorrelationPolicy(config.rates),
        RiskBudgetPolicy(config.budget),
        TradeRatePolicy(config.rates),
    )
    return RiskManager(
        config=config,
        sizing=PositionSizingEngine(config.sizing),
        quality_policies=safety,
        conviction_policies=conviction,
        allocation_policies=allocation,
        kill_switch=KillSwitch(config.kill_switch),
        circuit_breaker=CircuitBreaker(config.circuit_breaker),
        exploration=exploration,
        event_bus=event_bus,
        audit=audit,
        state_store=state_store,
        notifier=notifier,
        metrics=metrics,
    )
