"""Market snapshots and computed feature values.

High-frequency analytical series may later live in ClickHouse; these Postgres
tables are the operational record (latest snapshots, feature store rows).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hades.shared_kernel.persistence.database import Base
from hades.shared_kernel.persistence.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MarketData(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A price/liquidity/volume snapshot for one token at a point in time."""

    __tablename__ = "market_data"

    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"), index=True, nullable=False
    )
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    holders: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(String(50), index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class Feature(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One computed feature value for a token (the feature store).

    ``ix_features_token_id_computed_at`` exists for one query: the funnel's
    ``count(distinct token_id) where computed_at >= window``. The single-column
    indexes cannot serve it. ``computed_at`` is not selective — the window covers
    essentially the whole table, since this store has no retention — so the
    planner scans ``ix_features_token_id`` for its ordering and then fetches
    every matching row from the heap to test ``computed_at``. Measured on the CT:
    **36.2 s over 618,082 rows**, with the endpoint polled faster than that.

    Both columns in one index makes the same plan index-only: the ordering that
    made ``token_id`` attractive is still there, ``computed_at`` is now available
    to filter without touching the heap, and the distinct needs no sort.
    """

    __tablename__ = "features"
    __table_args__ = (Index("ix_features_token_id_computed_at", "token_id", "computed_at"),)

    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    feature_set_version: Mapped[str | None] = mapped_column(String(50), index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
