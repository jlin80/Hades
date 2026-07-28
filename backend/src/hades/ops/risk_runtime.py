"""Risk & Portfolio runtime composition - wires and runs the guardian.

Process-level wiring (the ops layer may import application + infrastructure). It
assembles the Portfolio Manager and the Risk Manager from the container and
subscribes them to the end of the pipeline:

    committee ─CommitteePredictionGenerated─► [context builder → Risk Manager]
        ─► TradeApproved / TradeRejected
    positions ─PositionOpened/Updated/Closed─► [Portfolio Manager] ─► PortfolioUpdated

It restores the durable defence-layer state on startup (a halt survives a
restart), feeds closed-trade outcomes to the Kill Switch, and publishes Redis
status snapshots for the dashboard. Nothing here executes a trade: the Risk
Manager only approves/rejects, and no Execution Engine is wired yet.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.portfolio.application.metrics import PortfolioMetrics
from hades.contexts.portfolio.application.portfolio_manager import PortfolioManager
from hades.contexts.portfolio.domain.events import PositionClosed
from hades.contexts.portfolio.domain.ports import PortfolioHistoryStore, PortfolioStateStore
from hades.contexts.portfolio.infrastructure.stores import (
    InMemoryPortfolioHistoryStore,
    InMemoryPortfolioStateStore,
    PostgresPortfolioHistoryStore,
    PostgresPortfolioStateStore,
)
from hades.contexts.risk.application.factory import build_risk_manager, risk_config_from_settings
from hades.contexts.risk.application.manager import RiskManager
from hades.contexts.risk.application.metrics import RiskMetrics
from hades.contexts.risk.application.subscriber import RiskHandler
from hades.contexts.risk.domain.events import (
    ACTION_ENTER_EMERGENCY,
    ACTION_EXIT_EMERGENCY,
    ACTION_RESET_CIRCUIT_BREAKER,
    ACTION_RESET_KILL_SWITCH,
    ACTION_TRIP_CIRCUIT_BREAKER,
    RiskControlCommandIssued,
)
from hades.contexts.risk.domain.models import CircuitBreakerReason
from hades.contexts.risk.domain.ports import RiskAuditStore, RiskStateStore
from hades.contexts.risk.infrastructure.context_builder import RiskContextBuilder
from hades.contexts.risk.infrastructure.facts_cache import EventDrivenRiskFacts
from hades.contexts.risk.infrastructure.stores import (
    InMemoryRiskAuditStore,
    InMemoryRiskStateStore,
    PostgresRiskAuditStore,
    PostgresRiskStateStore,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

_logger = get_logger("risk.runtime")

#: Redis namespaces/keys the Worker publishes live status under (dashboard reads).
RISK_STATUS_NAMESPACE = "risk"
PORTFOLIO_STATUS_NAMESPACE = "portfolio"
STATUS_KEY = "status"
_STATUS_INTERVAL_SECONDS = 5.0
_STATUS_TTL_SECONDS = 30


class RiskRuntime:
    """Owns the wired Portfolio + Risk subsystem and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._risk_metrics = RiskMetrics(container.metrics)
        self._pf_metrics = PortfolioMetrics(container.metrics)
        self._risk_cache = CacheService(container.redis, namespace=RISK_STATUS_NAMESPACE)
        self._pf_cache = CacheService(container.redis, namespace=PORTFOLIO_STATUS_NAMESPACE)

        self._history: PortfolioHistoryStore = self._build_history()
        self._audit: RiskAuditStore = self._build_audit()
        self._state_store: RiskStateStore = self._build_state_store()
        self._portfolio_store: PortfolioStateStore = self._build_portfolio_store()

        self._portfolio = PortfolioManager(
            starting_balance_usd=container.settings.paper.starting_balance_usd,
            reserve_pct=container.settings.risk.liquidity_reserve_pct,
            mode=container.settings.trading_mode.value,
            event_bus=container.event_bus,
            history=self._history,
            state_store=self._portfolio_store,
        )
        self._manager: RiskManager = build_risk_manager(
            risk_config_from_settings(container.settings),
            event_bus=container.event_bus,
            audit=self._audit,
            state_store=self._state_store,
            notifier=container.notification,
            metrics=self._risk_metrics,
        )
        # Without a facts port the builder joins nothing, and the Risk Manager
        # ends up judging a token on the committee's own probabilities — which
        # is how a security-rejected token used to arrive looking approved.
        self._facts = EventDrivenRiskFacts()
        self._builder = RiskContextBuilder(self._facts)
        self._register()

    # -- construction ---------------------------------------------------------

    def _build_history(self) -> PortfolioHistoryStore:
        if self._c.database is not None:
            return PostgresPortfolioHistoryStore(self._c.database)
        return InMemoryPortfolioHistoryStore()

    def _build_audit(self) -> RiskAuditStore:
        if self._c.database is not None:
            return PostgresRiskAuditStore(self._c.database)
        return InMemoryRiskAuditStore()

    def _build_state_store(self) -> RiskStateStore:
        if self._c.database is not None:
            return PostgresRiskStateStore(self._c.database)
        return InMemoryRiskStateStore()

    def _build_portfolio_store(self) -> PortfolioStateStore:
        if self._c.database is not None:
            return PostgresPortfolioStateStore(self._c.database)
        return InMemoryPortfolioStateStore()

    def _register(self) -> None:
        # Subscribe the facts cache first: it must have recorded a token's
        # security/wallet verdict before the committee prediction for that same
        # token arrives and the Risk Manager asks for it.
        self._facts.register(self._c.event_bus)
        self._portfolio.register(self._c.event_bus)
        RiskHandler(self._manager, self._builder, self._portfolio).register(self._c.event_bus)
        # Feed closed-trade outcomes to the Kill Switch (win resets the streak).
        self._c.event_bus.subscribe(PositionClosed.__name__, self._on_position_closed)
        # Operator commands issued from the API travel here as events.
        self._c.event_bus.subscribe(RiskControlCommandIssued.__name__, self._on_control_command)

    async def _on_control_command(self, event: DomainEvent) -> None:
        if not isinstance(event, RiskControlCommandIssued):
            return
        try:
            if event.action == ACTION_RESET_KILL_SWITCH:
                await self._manager.reset_kill_switch()
            elif event.action == ACTION_RESET_CIRCUIT_BREAKER:
                await self._manager.reset_circuit_breaker()
            elif event.action == ACTION_ENTER_EMERGENCY:
                await self._manager.enter_emergency(event.reason or "operator request")
            elif event.action == ACTION_EXIT_EMERGENCY:
                await self._manager.exit_emergency(event.reason or "operator")
            elif event.action == ACTION_TRIP_CIRCUIT_BREAKER:
                await self._manager.trip_circuit_breaker(
                    CircuitBreakerReason.MANUAL, event.reason or "operator request"
                )
            else:
                _logger.warning("risk_unknown_control_command", action=event.action)
        except Exception as exc:  # operator command must not crash the runtime
            _logger.warning("risk_control_command_failed", action=event.action, error=str(exc))

    async def _on_position_closed(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionClosed):
            return
        try:
            await self._manager.record_trade_result(is_win=float(event.realized_pnl.amount) > 0)
        except Exception as exc:  # never let one close break the subscriber
            _logger.warning("risk_result_failed", error=str(exc))

    # -- lifecycle ------------------------------------------------------------

    async def _restore_control_state(self) -> None:
        try:
            control = await self._state_store.load()
        except Exception as exc:  # a cold store must not stop startup
            _logger.warning("risk_state_load_failed", error=str(exc))
            return
        if control is not None:
            self._manager.restore(control)
            _logger.info(
                "risk_control_restored",
                kill_switch=control.kill_switch.level.label,
                emergency=control.emergency_mode,
            )

    async def _restore_portfolio(self) -> None:
        try:
            book = await self._portfolio_store.load(mode=self._c.settings.trading_mode.value)
        except Exception as exc:  # a cold store must not stop startup
            _logger.warning("portfolio_state_load_failed", error=str(exc))
            return
        if book is None:
            return
        self._portfolio.restore(book)
        _logger.info(
            "portfolio_restored",
            cash_usd=round(book.cash_usd, 4),
            realized_pnl_usd=round(book.realized_pnl_usd, 4),
            open_positions=len(book.positions),
            saved_at=book.saved_at.isoformat() if book.saved_at else None,
        )

    async def start(self) -> list[asyncio.Task[None]]:
        await self._restore_control_state()
        await self._restore_portfolio()
        tasks = [asyncio.create_task(self._publish_status_loop(), name="risk-status")]
        _logger.info("risk_runtime_started", enabled=self._c.settings.risk.enabled)
        return tasks

    async def stop(self) -> None:
        self._stop.set()
        _logger.info("risk_runtime_stopped")

    # -- dashboard status -----------------------------------------------------

    async def _snapshots(self) -> tuple[dict[str, Any], dict[str, Any]]:
        state = await self._portfolio.risk_state()
        pf = self._portfolio.state()
        risk = self._manager.snapshot(state)
        self._pf_metrics.equity_usd.set(pf.equity_usd)
        self._pf_metrics.cash_usd.set(pf.cash_usd)
        self._pf_metrics.invested_usd.set(pf.invested_usd)
        self._pf_metrics.exposure_pct.set(pf.exposure_pct)
        self._pf_metrics.realized_pnl_usd.set(pf.realized_pnl_usd)
        self._pf_metrics.unrealized_pnl_usd.set(pf.unrealized_pnl_usd)
        self._pf_metrics.open_positions.set(pf.open_positions)
        self._pf_metrics.drawdown_pct.set(pf.drawdown_pct)

        risk_snapshot = {
            "enabled": self._c.settings.risk.enabled,
            "kill_switch_level": int(risk.kill_switch.level),
            "kill_switch_label": risk.kill_switch.level.label,
            "kill_switch_reason": risk.kill_switch.reason,
            "circuit_breaker_open": risk.circuit_breaker.open,
            "circuit_breaker_reasons": [r.value for r in risk.circuit_breaker.reasons],
            "emergency_mode": risk.emergency_mode,
            "open_positions": risk.open_positions,
            "portfolio_exposure_pct": risk.portfolio_exposure_pct,
            "daily_loss_pct": risk.daily_loss_pct,
            "drawdown_pct": risk.drawdown_pct,
            "consecutive_losses": risk.consecutive_losses,
            "approvals_session": risk.approvals_session,
            "rejections_session": risk.rejections_session,
            "updated_at": time.time(),
        }
        metrics = self._portfolio.metrics()
        pf_snapshot = {
            "starting_balance_usd": pf.starting_balance_usd,
            "equity_usd": pf.equity_usd,
            "cash_usd": pf.cash_usd,
            "invested_usd": pf.invested_usd,
            "realized_pnl_usd": pf.realized_pnl_usd,
            "unrealized_pnl_usd": pf.unrealized_pnl_usd,
            "roi_pct": pf.roi_pct,
            "drawdown_pct": pf.drawdown_pct,
            "exposure_pct": pf.exposure_pct,
            "open_positions": pf.open_positions,
            # The count alone told an operator that capital had left cash without
            # telling them where it went: "1 open position" and no way to learn
            # which token holds it, at what size, or whether it is up or down.
            # The book already knows all of it; only the snapshot was lossy.
            "positions": [
                {
                    "mint": str(p.token.mint),
                    "symbol": p.token.symbol,
                    "name": p.token.name,
                    "notional_usd": p.notional_usd,
                    "unrealized_pnl_usd": p.unrealized_pnl_usd,
                    "strategy": p.strategy,
                    "regime": p.regime,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                }
                for p in state.open_positions
            ],
            "reserve_pct": self._c.settings.risk.liquidity_reserve_pct,
            "available_usd": state.capital.available_usd,
            "metrics": metrics.model_dump(),
            "updated_at": time.time(),
        }
        return risk_snapshot, pf_snapshot

    async def _publish_status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                risk_snapshot, pf_snapshot = await self._snapshots()
                await self._risk_cache.set(
                    STATUS_KEY, risk_snapshot, ttl_seconds=_STATUS_TTL_SECONDS
                )
                await self._pf_cache.set(STATUS_KEY, pf_snapshot, ttl_seconds=_STATUS_TTL_SECONDS)
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("risk_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STATUS_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # -- accessors for the API ------------------------------------------------

    @property
    def manager(self) -> RiskManager:
        return self._manager

    @property
    def portfolio(self) -> PortfolioManager:
        return self._portfolio
