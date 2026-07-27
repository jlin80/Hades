"""Portfolio Manager - the live book of record and the risk read model.

Maintains the full portfolio state in real time - balance, equity, cash,
invested capital, realised and unrealised PnL, fees, drawdown, ROI and exposure -
by reacting to the Position event stream (``PositionOpened`` / ``PositionUpdated``
/ ``PositionClosed``). It is the single source of truth every dashboard and the
Risk Manager read from.

It also *is* the ``PortfolioReadPort``: :meth:`risk_state` assembles the exact,
immutable :class:`PortfolioRiskState` the Risk Manager evaluates against
(capital after reserve, tagged open positions, rolling drawdown, trade rate), so
the risk policies stay pure. The manager never decides or executes - it observes
fills and keeps the numbers honest.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from hades.contexts.portfolio.domain.analytics import PortfolioMetrics, compute_metrics
from hades.contexts.portfolio.domain.events import (
    PortfolioUpdated,
    PositionClosed,
    PositionOpened,
    PositionUpdated,
)
from hades.contexts.portfolio.domain.models import (
    ClosedTrade,
    PersistedPortfolio,
    PersistedPosition,
    PortfolioState,
)
from hades.contexts.portfolio.domain.ports import PortfolioHistoryStore, PortfolioStateStore
from hades.contexts.risk.domain.models import (
    CapitalSnapshot,
    DrawdownSnapshot,
    OpenPositionView,
    PortfolioRiskState,
    to_money,
)
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("portfolio.manager")
_EQUITY_CURVE_MAX = 10_000
#: How many closed trades the persisted snapshot carries. Analytics, the loss
#: streak and the rolling PnL windows all read from the recent tail; keeping the
#: whole history here would grow one JSONB row without bound.
_CLOSED_TRADES_MAX = 1_000


class PortfolioManager:
    """Owns live portfolio state, fed by the Position event stream."""

    def __init__(
        self,
        *,
        starting_balance_usd: float,
        reserve_pct: float = 35.0,
        mode: str = "paper",
        event_bus: EventBus | None = None,
        history: PortfolioHistoryStore | None = None,
        state_store: PortfolioStateStore | None = None,
    ) -> None:
        self._start = starting_balance_usd
        self._reserve_pct = reserve_pct
        self._mode = mode
        self._bus = event_bus
        self._history = history
        self._state_store = state_store

        self._cash = starting_balance_usd
        self._realized = 0.0
        self._fees = 0.0
        self._slippage = 0.0
        self._peak = starting_balance_usd
        self._positions: dict[str, OpenPositionView] = {}
        self._closed: list[ClosedTrade] = []
        self._opens: deque[tuple[datetime, str]] = deque(maxlen=1000)
        self._equity_curve: deque[float] = deque([starting_balance_usd], maxlen=_EQUITY_CURVE_MAX)

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(PositionOpened.__name__, self._on_opened)
        event_bus.subscribe(PositionUpdated.__name__, self._on_updated)
        event_bus.subscribe(PositionClosed.__name__, self._on_closed)

    # -- event handlers -------------------------------------------------------

    async def _on_opened(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionOpened):
            return
        notional = float(event.notional.amount)
        tags = event.tags
        self._positions[str(event.aggregate_id)] = OpenPositionView(
            token=event.token,
            notional_usd=notional,
            strategy=tags.get("strategy", "default"),
            developer=tags.get("developer"),
            cluster=tags.get("cluster"),
            narrative=tags.get("narrative"),
            regime=tags.get("regime", "unknown"),
            opened_at=event.occurred_at,
        )
        self._cash -= notional
        self._opens.append((event.occurred_at, tags.get("strategy", "default")))
        await self._recompute()

    async def _on_updated(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionUpdated):
            return
        key = str(event.aggregate_id)
        pos = self._positions.get(key)
        if pos is None:
            return
        self._positions[key] = pos.model_copy(
            update={"unrealized_pnl_usd": float(event.unrealized_pnl.amount)}
        )
        await self._recompute()

    async def _on_closed(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionClosed):
            return
        key = str(event.aggregate_id)
        pos = self._positions.pop(key, None)
        pnl = float(event.realized_pnl.amount)
        self._realized += pnl
        # Return the freed principal plus the realised PnL to cash.
        if pos is not None:
            self._cash += pos.notional_usd + pnl
            # ``PositionClosed`` does not carry the token; take it from the open
            # view we tracked at open time (the authoritative attribution).
            self._closed.append(
                ClosedTrade(
                    token=pos.token,
                    pnl_usd=pnl,
                    strategy=pos.strategy,
                    reason=event.reason,
                    at=event.occurred_at,
                )
            )
        else:
            self._cash += pnl
        if self._history is not None:
            try:
                await self._history.record_pnl(pnl, kind="realized", mode=self._mode)
            except Exception as exc:  # history best-effort
                _logger.debug("pnl_history_failed", error=str(exc))
        await self._recompute()

    # -- state ---------------------------------------------------------------

    def state(self) -> PortfolioState:
        invested = sum(p.notional_usd for p in self._positions.values())
        unrealized = sum(p.unrealized_pnl_usd for p in self._positions.values())
        return PortfolioState(
            at=datetime.now(UTC),
            starting_balance_usd=self._start,
            cash_usd=round(self._cash, 6),
            invested_usd=round(invested, 6),
            realized_pnl_usd=round(self._realized, 6),
            unrealized_pnl_usd=round(unrealized, 6),
            fees_usd=round(self._fees, 6),
            slippage_usd=round(self._slippage, 6),
            peak_equity_usd=round(self._peak, 6),
            open_positions=len(self._positions),
        )

    async def risk_state(self) -> PortfolioRiskState:
        """Implements ``PortfolioReadPort`` - the Risk Manager's read model."""
        st = self.state()
        equity = st.equity_usd
        capital = CapitalSnapshot(
            equity_usd=equity,
            cash_usd=st.cash_usd,
            invested_usd=st.invested_usd,
            reserve_pct=self._reserve_pct,
        )
        drawdown = DrawdownSnapshot(
            peak_equity_usd=max(self._peak, equity),
            current_equity_usd=equity,
            daily_pnl_usd=self._window_pnl(days=1),
            weekly_pnl_usd=self._window_pnl(days=7),
            monthly_pnl_usd=self._window_pnl(days=30),
            daily_stop_losses=self._stop_losses_today(),
        )
        strategy_notional: dict[str, float] = {}
        for p in self._positions.values():
            strategy_notional[p.strategy] = strategy_notional.get(p.strategy, 0.0) + p.notional_usd
        return PortfolioRiskState(
            capital=capital,
            drawdown=drawdown,
            open_positions=tuple(self._positions.values()),
            consecutive_losses=self._consecutive_losses(),
            trades_last_hour=self._trades_last_hour(),
            trades_last_hour_by_strategy=self._trades_last_hour_by_strategy(),
            strategy_notional_usd=strategy_notional,
        )

    def metrics(self) -> PortfolioMetrics:
        return compute_metrics(
            trade_pnls=[t.pnl_usd for t in self._closed],
            equity_curve=list(self._equity_curve),
            starting_equity=self._start,
        )

    def closed_trades(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._closed)

    # -- rolling helpers ------------------------------------------------------

    def _window_pnl(self, *, days: int) -> float:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        return round(sum(t.pnl_usd for t in self._closed if t.at is not None and t.at >= cutoff), 6)

    def _stop_losses_today(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=1)
        return sum(
            1
            for t in self._closed
            if t.at is not None and t.at >= cutoff and t.reason in ("stop_loss", "trailing")
        )

    def _consecutive_losses(self) -> int:
        streak = 0
        for trade in reversed(self._closed):
            if trade.is_win:
                break
            streak += 1
        return streak

    def _trades_last_hour(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        return sum(1 for at, _ in self._opens if at >= cutoff)

    def _trades_last_hour_by_strategy(self) -> dict[str, int]:
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        out: dict[str, int] = {}
        for at, strategy in self._opens:
            if at >= cutoff:
                out[strategy] = out.get(strategy, 0) + 1
        return out

    # -- durability -----------------------------------------------------------

    def snapshot(self) -> PersistedPortfolio:
        """The book as it must be written to storage."""
        return PersistedPortfolio(
            starting_balance_usd=self._start,
            cash_usd=self._cash,
            realized_pnl_usd=self._realized,
            fees_usd=self._fees,
            slippage_usd=self._slippage,
            peak_equity_usd=self._peak,
            positions={
                key: PersistedPosition(
                    token=p.token,
                    notional_usd=p.notional_usd,
                    unrealized_pnl_usd=p.unrealized_pnl_usd,
                    strategy=p.strategy,
                    developer=p.developer,
                    cluster=p.cluster,
                    narrative=p.narrative,
                    regime=p.regime,
                    opened_at=p.opened_at,
                )
                for key, p in self._positions.items()
            },
            closed=tuple(self._closed[-_CLOSED_TRADES_MAX:]),
            opens=tuple(self._opens),
            equity_curve=tuple(self._equity_curve),
            saved_at=datetime.now(UTC),
        )

    def restore(self, state: PersistedPortfolio) -> None:
        """Rehydrate the book from storage (on startup, before any event).

        The starting balance is *not* restored: it is configuration, and letting
        a stored value win would make ``PAPER_STARTING_BALANCE_USD`` silently
        inert. ROI is measured against the configured balance either way.
        """
        self._cash = state.cash_usd
        self._realized = state.realized_pnl_usd
        self._fees = state.fees_usd
        self._slippage = state.slippage_usd
        self._peak = max(state.peak_equity_usd, self._start)
        self._positions = {
            key: OpenPositionView(
                token=p.token,
                notional_usd=p.notional_usd,
                unrealized_pnl_usd=p.unrealized_pnl_usd,
                strategy=p.strategy,
                developer=p.developer,
                cluster=p.cluster,
                narrative=p.narrative,
                regime=p.regime,
                opened_at=p.opened_at,
            )
            for key, p in state.positions.items()
        }
        self._closed = list(state.closed)
        self._opens = deque(state.opens, maxlen=1000)
        if state.equity_curve:
            self._equity_curve = deque(state.equity_curve, maxlen=_EQUITY_CURVE_MAX)

    async def _recompute(self) -> None:
        st = self.state()
        equity = st.equity_usd
        self._peak = max(self._peak, equity)
        self._equity_curve.append(equity)
        if self._state_store is not None:
            try:
                await self._state_store.save(self.snapshot(), mode=self._mode)
            except Exception as exc:  # durability is best-effort, never blocking
                _logger.warning("portfolio_state_save_failed", error=str(exc))
        if self._history is not None:
            try:
                await self._history.record_equity(equity, mode=self._mode)
                await self._history.record_snapshot(st, mode=self._mode)
            except Exception as exc:  # history best-effort
                _logger.debug("history_failed", error=str(exc))
        if self._bus is not None:
            await self._bus.publish(
                PortfolioUpdated(
                    aggregate_id=new_id(),
                    equity_usd=to_money(equity),
                    cash_usd=to_money(st.cash_usd),
                    invested_usd=to_money(st.invested_usd),
                    realized_pnl_usd=to_money(st.realized_pnl_usd),
                    unrealized_pnl_usd=to_money(st.unrealized_pnl_usd),
                    open_positions=st.open_positions,
                    exposure_pct=st.exposure_pct,
                )
            )
