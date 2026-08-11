"""Domain events emitted by the Portfolio context (the Position event stream)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from hades.contexts.common.domain.value_objects import Money, TokenRef
from hades.shared_kernel.domain.events import DomainEvent


class PositionOpened(DomainEvent):
    """A position was opened following a confirmed fill.

    ``tags`` carries the risk attribution captured at approval time
    (strategy / developer / cluster / narrative / regime) so the Portfolio
    Manager can track exposure and correlation without re-deriving it.
    """

    aggregate_type: str = "position"
    token: TokenRef
    entry_price: Money
    quantity: Decimal
    notional: Money
    tags: dict[str, str] = Field(default_factory=dict)


class PositionUpdated(DomainEvent):
    """Mark-to-market update (new price -> new unrealised PnL)."""

    aggregate_type: str = "position"
    mark_price: Money
    unrealized_pnl: Money


class TrailingStopAdjusted(DomainEvent):
    """The trailing stop level moved in the favourable direction."""

    aggregate_type: str = "position"
    new_stop_price: Money


class PositionClosed(DomainEvent):
    """A position was fully closed.

    ``token`` is the *reconciliation* key, and it exists because the
    ``aggregate_id`` alone proved not to be one. The Execution Engine tracked
    open positions in a process-local dict, so a restart lost the mint →
    position_id mapping and every later close carried a freshly minted id that
    no ``PositionOpened`` had ever used. The Portfolio Manager looked that id up,
    missed, and took the unattributed branch — 2,420 closes in production, not
    one of them matched, and the book drifted to a negative cash balance while
    every container reported healthy.

    An id that is regenerated when it is not known is not an identity. The mint
    is one: the engine models a single position per mint, so a close can always
    be attributed by token even when the id is lost. It is optional so envelopes
    published before this field existed still rebuild; such an event cannot be
    attributed and is refused rather than applied to the book.
    """

    aggregate_type: str = "position"
    exit_price: Money
    realized_pnl: Money
    reason: str  # "take_profit" | "stop_loss" | "trailing" | "manual" | ...
    token: TokenRef | None = None
    #: Gross proceeds of the closing fill. Carried so the book of record can
    #: recompute realised PnL for itself when the engine closed a position whose
    #: entry it had lost: the old code substituted the exit notional for the
    #: unknown entry, which makes ``realized_pnl`` collapse to minus the exit fee
    #: — a plausible-looking number that is pure fiction. ``None`` means the
    #: engine could attribute the entry and its own figure stands.
    exit_notional: Money | None = None


class CapitalCommitted(DomainEvent):
    """Capital was reserved for a newly-opened position."""

    aggregate_type: str = "portfolio"
    amount: Money
    available_after: Money


class CapitalReleased(DomainEvent):
    """Capital was freed when a position closed (principal + realised PnL)."""

    aggregate_type: str = "portfolio"
    amount: Money
    available_after: Money


class PortfolioUpdated(DomainEvent):
    """The portfolio read model changed (equity / PnL / exposure recomputed)."""

    aggregate_type: str = "portfolio"
    equity_usd: Money
    cash_usd: Money
    invested_usd: Money
    realized_pnl_usd: Money
    unrealized_pnl_usd: Money
    open_positions: int
    exposure_pct: float
