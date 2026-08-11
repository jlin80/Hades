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

from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
#: How many applied event ids the idempotency guard remembers. The bus is
#: at-least-once and ``XAUTOCLAIM`` re-dispatches anything a killed container
#: left unacked, so a money-mutating handler that is not idempotent will
#: double-count on the next restart.
#:
#: The bound is deliberately small, and the reason is the write path rather than
#: memory: this whole snapshot is re-serialised into one JSONB row on *every*
#: recompute, so anything kept here is paid again on each mark-to-market tick.
#: Only opens and closes are guarded — a few thousand events over the platform's
#: lifetime so far — and a reclaim only reaches messages idle beyond 300 seconds,
#: so the window that must be covered is minutes, not weeks.
_APPLIED_EVENTS_MAX = 2_000


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
        #: Entry facts the *monitor* needs to resume after a restart, kept beside
        #: the risk view rather than inside it: ``OpenPositionView`` is the risk
        #: context's shape and exposure analysis has no use for a fill price.
        self._entries: dict[str, tuple[Decimal, Decimal, dict[str, str]]] = {}
        self._closed: list[ClosedTrade] = []
        self._opens: deque[tuple[datetime, str]] = deque(maxlen=1000)
        self._equity_curve: deque[float] = deque([starting_balance_usd], maxlen=_EQUITY_CURVE_MAX)
        #: Ids of the money-mutating events already folded into the book above.
        self._applied: OrderedDict[str, None] = OrderedDict()

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(PositionOpened.__name__, self._on_opened)
        event_bus.subscribe(PositionUpdated.__name__, self._on_updated)
        event_bus.subscribe(PositionClosed.__name__, self._on_closed)

    # -- event handlers -------------------------------------------------------

    async def _on_opened(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionOpened):
            return
        if self._already_applied(event):
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
        self._entries[str(event.aggregate_id)] = (
            Decimal(str(event.entry_price.amount)),
            Decimal(str(event.quantity)),
            dict(tags),
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
        """Fold a close into the book — but only one the book can attribute.

        The previous version applied ``realized_pnl`` to the running total
        *before* checking whether the position was known, and credited cash in
        both branches. An unattributable close therefore moved money, recorded no
        ``ClosedTrade`` to explain it, and left the position open forever with its
        principal never returned. In production that ran 2,420 times and put cash
        at -37,713 USD against a 1,000 USD paper balance.

        Attribution is now a precondition rather than a detail: by position id,
        then by token, and if neither names a position the event is refused. A
        book that declines to record what it cannot explain is the only kind
        whose totals mean anything.
        """
        if not isinstance(event, PositionClosed):
            return
        if self._already_applied(event):
            return
        key, pos = self._attribute(event)
        if key is None or pos is None:
            _logger.error(
                "position_close_unattributed",
                position_id=str(event.aggregate_id),
                mint=str(event.token.mint) if event.token else None,
                reason=event.reason,
                open_positions=len(self._positions),
                note="close names no position in the book; refused, nothing applied",
            )
            return
        self._positions.pop(key, None)
        self._entries.pop(key, None)
        pnl = self._realized_pnl(event, pos)
        self._realized += pnl
        # Return the freed principal plus the realised PnL to cash.
        self._cash += pos.notional_usd + pnl
        self._closed.append(
            ClosedTrade(
                token=pos.token,
                pnl_usd=pnl,
                strategy=pos.strategy,
                reason=event.reason,
                at=event.occurred_at,
            )
        )
        if self._history is not None:
            try:
                await self._history.record_pnl(pnl, kind="realized", mode=self._mode)
            except Exception as exc:  # history best-effort
                _logger.debug("pnl_history_failed", error=str(exc))
        await self._recompute()

    # -- attribution & idempotency -------------------------------------------

    def _already_applied(self, event: DomainEvent) -> bool:
        """Has this exact event already been folded into the book?

        ``event_id`` survives the envelope, so a redelivered message rebuilds
        with the same one. Only the accumulating handlers consult this: marking
        is what makes ``+=`` safe on a bus that has always promised at-least-once
        and, since the orphan reclaim landed, actually exercises it. Mark-to-
        market is deliberately *not* guarded — it assigns rather than accumulates,
        so it is idempotent by construction, and it is also the highest-volume
        event on the stream; admitting it here would evict the open/close ids
        that need the protection.
        """
        key = str(event.event_id)
        if key in self._applied:
            _logger.warning(
                "portfolio_event_redelivered",
                event_type=type(event).__name__,
                event_id=key,
                note="already applied to the book; ignored",
            )
            return True
        self._applied[key] = None
        while len(self._applied) > _APPLIED_EVENTS_MAX:
            self._applied.popitem(last=False)
        return False

    def _attribute(self, event: PositionClosed) -> tuple[str | None, OpenPositionView | None]:
        """Name the position a close belongs to: by id, then by token."""
        key = str(event.aggregate_id)
        pos = self._positions.get(key)
        if pos is not None:
            return key, pos
        if event.token is None:
            return None, None
        # The engine documents one open position per mint, which makes the token
        # a sufficient key whenever the id was lost with the process that minted
        # it. Production does not always honour that: the live book holds mints
        # carrying two open positions at once, so the match can be ambiguous.
        #
        # Resolve it oldest-first — dict order is insertion order — because a
        # close is far more likely to belong to the position that has been open
        # longest, and because an arbitrary choice would make the book depend on
        # hash ordering. Ambiguity is reported rather than absorbed: it means the
        # engine's own single-position-per-mint assumption is being violated
        # upstream, which is a defect in its own right and not this method's to
        # paper over.
        mint = str(event.token.mint)
        matches = [
            (candidate_key, candidate)
            for candidate_key, candidate in self._positions.items()
            if str(candidate.token.mint) == mint
        ]
        if not matches:
            return None, None
        if len(matches) > 1:
            _logger.warning(
                "position_close_attribution_ambiguous",
                mint=mint,
                candidates=len(matches),
                chosen=matches[0][0],
                note="several open positions share this mint; took the oldest",
            )
        chosen_key, chosen = matches[0]
        _logger.info(
            "position_close_attributed_by_token",
            mint=mint,
            position_id=chosen_key,
            event_position_id=key,
            note="close carried an id the book does not hold; matched on mint",
        )
        return chosen_key, chosen

    @staticmethod
    def _realized_pnl(event: PositionClosed, pos: OpenPositionView) -> float:
        """The realised PnL to record, recomputed when the engine lost the entry.

        ``exit_notional`` is set only when the Execution Engine could not name the
        entry it was closing against. Its own ``realized_pnl`` is then worthless —
        it substituted the exit notional for the unknown entry, so the figure
        collapses to minus the exit fee regardless of how the trade actually went.
        The book holds the real entry notional, so it finishes the sum itself.

        The result is net of the exit fee only: the entry fee was paid by a
        process that no longer exists and the book never carried it. That is a
        stated approximation on a recovery path, not a silent one — and it is
        bounded by one fee, where the figure it replaces was unbounded fiction.
        """
        if event.exit_notional is None:
            return float(event.realized_pnl.amount)
        exit_notional = float(event.exit_notional.amount)
        exit_fees = -float(event.realized_pnl.amount)
        return exit_notional - pos.notional_usd - exit_fees

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
                    entry_price=self._entries.get(key, (None, None, {}))[0],
                    quantity=self._entries.get(key, (None, None, {}))[1],
                    tags=self._entries.get(key, (None, None, {}))[2],
                )
                for key, p in self._positions.items()
            },
            closed=tuple(self._closed[-_CLOSED_TRADES_MAX:]),
            opens=tuple(self._opens),
            equity_curve=tuple(self._equity_curve),
            applied_events=tuple(self._applied),
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
        self._entries = {
            key: (p.entry_price, p.quantity, dict(p.tags))
            for key, p in state.positions.items()
            if p.entry_price is not None and p.quantity is not None
        }
        self._closed = list(state.closed)
        self._opens = deque(state.opens, maxlen=1000)
        self._applied = OrderedDict(
            (event_id, None) for event_id in state.applied_events[-_APPLIED_EVENTS_MAX:]
        )
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
