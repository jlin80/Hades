"""Portfolio read-model time series: history, equity curve and PnL log."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hades.shared_kernel.persistence.database import Base
from hades.shared_kernel.persistence.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin

_AMOUNT = Numeric(38, 18)


class PortfolioStateRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The latest live book — one logical row per trading mode.

    ``portfolio_history`` is an append-only series for charts; this is the state
    the Portfolio Manager reloads on startup so cash, open positions and realised
    PnL are not reset to the starting balance by every restart.
    """

    __tablename__ = "portfolio_state"

    mode: Mapped[str] = mapped_column(String(10), unique=True, index=True, nullable=False)
    cash_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    peak_equity_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    open_positions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class PortfolioHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A full portfolio snapshot (exposure, PnL, position count) over time."""

    __tablename__ = "portfolio_history"

    mode: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    total_equity_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    cash_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    positions_value_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    open_positions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exposure_pct: Mapped[float | None] = mapped_column(Numeric(9, 4))
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    unrealized_pnl_usd: Mapped[Decimal | None] = mapped_column(_AMOUNT)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class EquityCurve(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single equity sample — the raw series behind the equity chart."""

    __tablename__ = "equity_curve"

    mode: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    equity_usd: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)


class PnLHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A realized/unrealized PnL entry, optionally tied to a position/token."""

    __tablename__ = "pnl_history"

    mode: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), index=True
    )
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tokens.id", ondelete="SET NULL"), index=True
    )
    amount_usd: Mapped[Decimal] = mapped_column(_AMOUNT, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
