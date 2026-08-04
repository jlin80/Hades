"""StrategyEngine - the multi-strategy orchestrator (failsafe, weighted, explained).

For each token (a :class:`MarketContext` assembled from the committee verdict) the
engine runs every enabled strategy behind the failsafe, collects their signals,
recomputes each strategy's dynamic weight, and asks the
:class:`EnsembleBuilder` for a single fused :class:`EnsembleSignal`. It emits the
per-strategy signals and the headline ``EnsembleSignalGenerated`` on the bus.

It never executes, sizes or approves anything - the ensemble is *evidence* the
Risk Manager may consume. The engine is also where the platform invariants live in
code: one strategy raising is caught, counted and muted (never fatal); shadow
strategies are recorded but excluded from the production decision; a degrading
strategy is tapered by the weighting engine, never deleted.
"""

from __future__ import annotations

import time

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.strategy.application.base import BaseStrategy
from hades.contexts.strategy.application.ensemble import EnsembleBuilder
from hades.contexts.strategy.application.evaluation import SelfEvaluator
from hades.contexts.strategy.application.lifecycle import ShadowLifecycle
from hades.contexts.strategy.application.metrics import StrategyMetrics
from hades.contexts.strategy.application.registry import StrategyRegistry
from hades.contexts.strategy.application.weighting import DynamicWeightEngine
from hades.contexts.strategy.domain.events import (
    EnsembleSignalGenerated,
    ShadowActivated,
    SignalGenerated,
    SignalRejected,
    StrategyDisabled,
    StrategyError,
    StrategyLoaded,
    WeightUpdated,
)
from hades.contexts.strategy.domain.models import (
    EnsembleSignal,
    MarketContext,
    StrategyOutcome,
    StrategyPerformance,
    StrategySignal,
    StrategyWeight,
)
from hades.contexts.strategy.domain.ports import PerformanceStore, SignalStore
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("strategy.engine")

#: A weight change smaller than this (relative) is not worth an event.
_WEIGHT_CHANGE_EPS = 0.15


class StrategyEngine:
    """Runs the strategy roster over a token and fuses their signals."""

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        weighting: DynamicWeightEngine,
        ensemble: EnsembleBuilder,
        evaluator: SelfEvaluator,
        lifecycle: ShadowLifecycle | None = None,
        event_bus: EventBus | None = None,
        notifier: NotificationPublisher | None = None,
        metrics: StrategyMetrics | None = None,
        performance_store: PerformanceStore | None = None,
        signal_store: SignalStore | None = None,
        min_notify_confidence: float = 0.5,
        research_boost: float = 1.0,
    ) -> None:
        self._registry = registry
        self._weighting = weighting
        self._ensemble = ensemble
        self._evaluator = evaluator
        self._lifecycle = lifecycle or ShadowLifecycle()
        self._bus = event_bus
        self._notifier = notifier
        self._metrics = metrics
        self._perf = performance_store
        self._signals = signal_store
        self._min_notify_confidence = min_notify_confidence
        self._research_boost = research_boost
        # Session telemetry for the dashboard.
        self._signals_session = 0
        self._ensembles_session = 0
        self._signal_counts: dict[str, int] = {}
        self._last_signal: dict[str, dict[str, object]] = {}
        self._last_ensemble: dict[str, object] = {}

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    # -- lifecycle ------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialise every registered strategy and announce it on the bus."""
        for strategy in self._registry.all():
            try:
                await strategy.initialize()
            except Exception as exc:  # a strategy that can't start is muted, not fatal
                _logger.warning("strategy_init_failed", strategy=strategy.name, error=str(exc))
                self._registry.record_error(strategy.name)
                continue
            meta = strategy.metadata
            await self._publish(
                StrategyLoaded(
                    aggregate_id=new_id(),
                    strategy=meta.name,
                    strategy_version=meta.version,
                    lifecycle_stage=meta.lifecycle_stage.value,
                    priority=meta.priority,
                )
            )
            if self._lifecycle.is_shadow(meta.lifecycle_stage):
                await self._publish(
                    ShadowActivated(
                        aggregate_id=new_id(),
                        strategy=meta.name,
                        lifecycle_stage=meta.lifecycle_stage.value,
                    )
                )

    async def shutdown(self) -> None:
        for strategy in self._registry.all():
            try:
                await strategy.shutdown()
            except Exception as exc:  # shutdown must never raise out
                _logger.warning("strategy_shutdown_failed", strategy=strategy.name, error=str(exc))

    # -- evaluation -----------------------------------------------------------

    async def evaluate(self, context: MarketContext) -> EnsembleSignal:
        """Run all strategies over one token and emit the fused ensemble."""
        started = time.monotonic()
        performance = await self._all_performance()
        signals: list[StrategySignal] = []

        for strategy in self._registry.active():
            signal = await self._run_one(strategy, context, performance)
            if signal is not None:
                signals.append(signal)

        weights = await self._recompute_weights(context, performance)
        ensemble = self._ensemble.build(
            context, signals, weights, correlation_id=_context_correlation(context)
        )

        self._ensembles_session += 1
        self._record_ensemble(ensemble)
        await self._publish(
            EnsembleSignalGenerated(aggregate_id=new_id(), token=context.token, ensemble=ensemble)
        )
        if self._metrics is not None:
            self._metrics.ensembles.labels(decision=ensemble.decision.value).inc()
            self._metrics.ensemble_confidence.observe(ensemble.confidence)
            self._metrics.evaluate_seconds.observe(time.monotonic() - started)
        await self._persist_ensemble(ensemble)
        await self._maybe_notify(ensemble)
        return ensemble

    async def _run_one(
        self,
        strategy: BaseStrategy,
        context: MarketContext,
        performance: dict[str, StrategyPerformance],
    ) -> StrategySignal | None:
        name = strategy.name
        try:
            validation = strategy.validate_market(context)
            if not validation.ok:
                if self._metrics is not None:
                    self._metrics.rejected.labels(strategy=name).inc()
                await self._publish(
                    SignalRejected(
                        aggregate_id=new_id(),
                        token=context.token,
                        strategy=name,
                        reason=validation.reason,
                    )
                )
                return None
            signal = strategy.generate_signal(context)
        except Exception as exc:  # one strategy must never break the others
            muted = self._registry.record_error(name)
            if self._metrics is not None:
                self._metrics.errors.labels(strategy=name).inc()
            _logger.warning("strategy_evaluate_failed", strategy=name, error=str(exc), muted=muted)
            await self._publish(StrategyError(aggregate_id=new_id(), strategy=name, error=str(exc)))
            if muted:
                await self._publish(
                    StrategyDisabled(
                        aggregate_id=new_id(),
                        strategy=name,
                        reason=f"failsafe: {self._registry.error_count(name)} consecutive errors",
                    )
                )
            return None

        self._record_signal(signal)
        if signal.is_actionable:
            if self._metrics is not None:
                self._metrics.signals.labels(
                    strategy=name, signal_type=signal.signal_type.value
                ).inc()
            await self._publish(
                SignalGenerated(aggregate_id=new_id(), token=context.token, signal=signal)
            )
            await self._persist_signal(signal)
        return signal

    async def _recompute_weights(
        self, context: MarketContext, performance: dict[str, StrategyPerformance]
    ) -> dict[str, StrategyWeight]:
        weights: dict[str, StrategyWeight] = {}
        for strategy in self._registry.all():
            weight = self._weighting.compute(
                strategy,
                context,
                performance.get(strategy.name),
                research_boost=self._research_boost,
            )
            previous = self._registry.set_weight(weight)
            weights[strategy.name] = weight
            if _weight_changed(previous.weight, weight.weight):
                await self._publish(
                    WeightUpdated(
                        aggregate_id=new_id(),
                        strategy=strategy.name,
                        weight=weight.weight,
                        previous_weight=previous.weight,
                        reason=weight.reason,
                    )
                )
        return weights

    # -- self-evaluation ------------------------------------------------------

    async def record_outcome(self, outcome: StrategyOutcome) -> None:
        """Attribute a realised result to a strategy (from the Position stream)."""
        if self._perf is None or outcome.strategy not in self._registry:
            return
        await self._perf.record_outcome(outcome)

    async def _all_performance(self) -> dict[str, StrategyPerformance]:
        if self._perf is None:
            return {}
        try:
            return await self._perf.all_performance()
        except Exception as exc:  # a cold store must not stop evaluation
            _logger.warning("performance_read_failed", error=str(exc))
            return {}

    # -- telemetry ------------------------------------------------------------

    def _record_signal(self, signal: StrategySignal) -> None:
        self._signals_session += 1
        self._signal_counts[signal.strategy] = self._signal_counts.get(signal.strategy, 0) + 1
        self._last_signal[signal.strategy] = {
            "signal_type": signal.signal_type.value,
            "confidence": signal.confidence,
            "internal_score": signal.internal_score,
            "reasoning": signal.reasoning,
            "regime": signal.regime.value,
            "shadow": signal.shadow,
            "at": signal.at.isoformat(),
        }

    def _record_ensemble(self, ensemble: EnsembleSignal) -> None:
        self._last_ensemble = {
            "mint": str(ensemble.token.mint),
            "decision": ensemble.decision.value,
            "score": ensemble.score,
            "confidence": ensemble.confidence,
            "agreement": ensemble.agreement,
            "participating": ensemble.participating,
            "reasoning": ensemble.reasoning,
            "regime": ensemble.regime.value,
            "at": ensemble.at.isoformat(),
        }

    def snapshot(
        self, performance: dict[str, StrategyPerformance] | None = None
    ) -> dict[str, object]:
        """Live status for the dashboard (never any trade/order information)."""
        performance = performance or {}
        strategies = []
        for strategy in self._registry.all():
            meta = strategy.metadata
            weight = self._registry.weight_of(meta.name)
            perf = performance.get(meta.name)
            strategies.append(
                {
                    "name": meta.name,
                    "version": meta.version,
                    "status": meta.status.value,
                    "lifecycle_stage": meta.lifecycle_stage.value,
                    "shadow": self._lifecycle.is_shadow(meta.lifecycle_stage),
                    "priority": meta.priority,
                    "weight": weight.weight,
                    "sharpe": perf.sharpe if perf else meta.historical_sharpe,
                    "profit_factor": perf.profit_factor if perf else meta.profit_factor,
                    "win_rate": perf.win_rate if perf else meta.win_rate,
                    "expectancy": perf.expectancy_usd if perf else meta.expectancy,
                    "trades": perf.trades if perf else 0,
                    "degraded": perf.degraded if perf else False,
                    "signals": self._signal_counts.get(meta.name, 0),
                    "last_signal": self._last_signal.get(meta.name),
                }
            )
        ranking = sorted(strategies, key=lambda s: float(s["weight"]), reverse=True)  # type: ignore[arg-type]
        return {
            "count": len(strategies),
            "active": len(self._registry.active()),
            "signals_session": self._signals_session,
            "ensembles_session": self._ensembles_session,
            "strategies": strategies,
            "ranking": [s["name"] for s in ranking],
            "last_ensemble": self._last_ensemble,
        }

    # -- helpers --------------------------------------------------------------

    async def _publish(self, event: object) -> None:
        if self._bus is None:
            return
        from hades.shared_kernel.domain.events import DomainEvent

        if isinstance(event, DomainEvent):
            await self._bus.publish(event)

    async def _persist_signal(self, signal: StrategySignal) -> None:
        if self._signals is None:
            return
        try:
            await self._signals.save_signal(signal.model_dump(mode="json"))
        except Exception as exc:  # persistence is best-effort
            _logger.warning("signal_persist_failed", strategy=signal.strategy, error=str(exc))

    async def _persist_ensemble(self, ensemble: EnsembleSignal) -> None:
        if self._signals is None:
            return
        try:
            await self._signals.save_ensemble(ensemble.model_dump(mode="json"))
        except Exception as exc:  # persistence is best-effort
            _logger.warning("ensemble_persist_failed", error=str(exc))

    async def _maybe_notify(self, ensemble: EnsembleSignal) -> None:
        if self._notifier is None or not ensemble.is_actionable:
            return
        if ensemble.confidence < self._min_notify_confidence:
            return
        symbol = ensemble.token.symbol or str(ensemble.token.mint)[:8]
        await self._notifier.notify(
            title=f"Strategy ensemble - {ensemble.decision.value.upper()} - {symbol}",
            body=(
                f"{ensemble.participating} strategies - conf {ensemble.confidence:.0%} - "
                f"{ensemble.reasoning}"
            ),
            severity=Severity.INFO,
            tags={"topic": "strategy", "mint": str(ensemble.token.mint)},
        )


def _weight_changed(previous: float, current: float) -> bool:
    base = max(1e-6, previous)
    return abs(current - previous) / base >= _WEIGHT_CHANGE_EPS


def _context_correlation(context: MarketContext) -> str | None:
    corr = context.features.get("correlation_id") if context.features else None
    return str(corr) if isinstance(corr, str) else None
