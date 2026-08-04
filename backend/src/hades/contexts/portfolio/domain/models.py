"""Portfolio domain model, including the ``Position`` aggregate root.

``Position`` is the reference example of the event-sourced aggregate pattern in
Hades: every state transition is expressed as a method that enforces invariants
and records a domain event. The trailing-stop / take-profit *policy* (when to
close) lives in a later phase; here we model the state machine and its
guarantees only.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from hades.contexts.common.domain.value_objects import Money, TokenRef
from hades.contexts.portfolio.domain.events import (
    PositionClosed,
    PositionOpened,
    PositionUpdated,
    TrailingStopAdjusted,
)
from hades.shared_kernel.domain.base import AggregateRoot, ValueObject
from hades.shared_kernel.errors import DomainError


class PositionStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"


class Position(AggregateRoot):
    """A single tracked position in one token. Consistency boundary for trades."""

    token: TokenRef
    status: PositionStatus = PositionStatus.PENDING
    quantity: Decimal = Decimal(0)
    entry_price: Money | None = None
    mark_price: Money | None = None
    stop_price: Money | None = None
    realized_pnl: Money | None = None

    def open(
        self,
        *,
        entry_price: Money,
        quantity: Decimal,
        notional: Money,
        tags: dict[str, str] | None = None,
    ) -> None:
        if self.status is not PositionStatus.PENDING:
            raise DomainError("position already opened", context={"id": str(self.id)})
        if quantity <= 0:
            raise DomainError("quantity must be positive")
        self.status = PositionStatus.OPEN
        self.entry_price = entry_price
        self.mark_price = entry_price
        self.quantity = quantity
        self._record(
            PositionOpened(
                aggregate_id=self.id,
                token=self.token,
                entry_price=entry_price,
                quantity=quantity,
                notional=notional,
                tags=tags or {},
            )
        )

    def mark(self, *, price: Money, unrealized_pnl: Money) -> None:
        self._require_open()
        self.mark_price = price
        self._record(
            PositionUpdated(aggregate_id=self.id, mark_price=price, unrealized_pnl=unrealized_pnl)
        )

    def adjust_trailing_stop(self, *, new_stop_price: Money) -> None:
        self._require_open()
        self.stop_price = new_stop_price
        self._record(TrailingStopAdjusted(aggregate_id=self.id, new_stop_price=new_stop_price))

    def close(self, *, exit_price: Money, realized_pnl: Money, reason: str) -> None:
        self._require_open()
        self.status = PositionStatus.CLOSED
        self.realized_pnl = realized_pnl
        self._record(
            PositionClosed(
                aggregate_id=self.id,
                exit_price=exit_price,
                realized_pnl=realized_pnl,
                reason=reason,
            )
        )

    def _require_open(self) -> None:
        if self.status is not PositionStatus.OPEN:
            raise DomainError(
                "position is not open", context={"id": str(self.id), "status": self.status}
            )


# =============================================================================
# Portfolio read model (maintained by the Portfolio Manager)
# =============================================================================


class ClosedTrade(ValueObject):
    """A realised trade result - the input row for Portfolio Analytics."""

    token: TokenRef
    pnl_usd: float
    strategy: str = "default"
    reason: str = ""
    at: datetime | None = None

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0


class PersistedPosition(ValueObject):
    """One open position as it is written to storage.

    Deliberately a portfolio-owned shape rather than the risk context's
    ``OpenPositionView``: the portfolio owns what it persists, and the domain
    layer here depends on nothing but ``common``. The manager converts at the
    boundary, which is also where a schema change would have to be handled.

    ``entry_price``, ``quantity`` and ``tags`` are not used by the portfolio's own
    accounting — notional is enough for that — but without them a restart could
    not rebuild the Position Monitor's view of the book. The portfolio survived a
    restart and the monitor did not, so a recovered position was marked by
    nobody: its unrealised PnL sat at exactly zero forever and no take-profit or
    stop-loss could ever fire again. They are optional so snapshots written
    before this field existed still load; such a position cannot be re-monitored
    and is reported rather than silently stranded.
    """

    token: TokenRef
    notional_usd: float
    unrealized_pnl_usd: float = 0.0
    strategy: str = "default"
    developer: str | None = None
    cluster: str | None = None
    narrative: str | None = None
    regime: str = "unknown"
    opened_at: datetime | None = None
    #: Entry facts + the exit envelope approved at entry, for monitor recovery.
    entry_price: Decimal | None = None
    quantity: Decimal | None = None
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_monitorable(self) -> bool:
        """Can a restarted Position Monitor take this position back over?"""
        return (
            self.entry_price is not None
            and self.quantity is not None
            and self.entry_price > 0
            and self.quantity > 0
        )


class PersistedPortfolio(ValueObject):
    """Everything the Portfolio Manager must recover to survive a restart.

    The manager kept its book purely in memory, so every worker restart silently
    reset cash to the starting balance and dropped every open position — the book
    of record was the least durable thing in the platform. This snapshot closes
    that hole (deferred item H3).

    Only *state* lives here, never derived figures: equity, exposure, ROI and
    drawdown are recomputed from these on load, so a stale formula can never be
    resurrected from storage.
    """

    starting_balance_usd: float
    cash_usd: float
    realized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    peak_equity_usd: float = 0.0
    #: position_id -> the open position.
    positions: dict[str, PersistedPosition] = Field(default_factory=dict)
    #: Recent closed trades — the input to analytics and the win/loss streak.
    closed: tuple[ClosedTrade, ...] = ()
    #: Recent (opened_at, strategy) pairs behind the trade-rate limits.
    opens: tuple[tuple[datetime, str], ...] = ()
    equity_curve: tuple[float, ...] = ()
    saved_at: datetime | None = None


class PortfolioState(ValueObject):
    """The live portfolio picture - balance, equity, PnL and exposure.

    This is the canonical state the Portfolio Manager keeps up to date in real
    time and the read model every dashboard and the Risk Manager query. All
    figures are USD floats at the read boundary (the aggregates keep Decimals).
    """

    at: datetime
    starting_balance_usd: float
    cash_usd: float
    invested_usd: float
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    peak_equity_usd: float = 0.0
    open_positions: int = 0

    @property
    def equity_usd(self) -> float:
        """Cash plus the marked value of open positions."""
        return round(self.cash_usd + self.invested_usd + self.unrealized_pnl_usd, 6)

    @property
    def exposure_pct(self) -> float:
        eq = self.equity_usd
        if eq <= 0:
            return 0.0
        return round(self.invested_usd / eq * 100.0, 4)

    @property
    def roi_pct(self) -> float:
        if self.starting_balance_usd <= 0:
            return 0.0
        return round(
            (self.equity_usd - self.starting_balance_usd) / self.starting_balance_usd * 100.0, 4
        )

    @property
    def drawdown_pct(self) -> float:
        peak = max(self.peak_equity_usd, self.equity_usd)
        if peak <= 0:
            return 0.0
        return round(max(0.0, (peak - self.equity_usd) / peak) * 100.0, 4)
