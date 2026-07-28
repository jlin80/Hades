"""Scanner-owned tables: token metadata, data-quality anomalies, snapshots.

These extend the schema for the data-acquisition phase. They follow the platform
rule (UUIDv7 pk + timestamps via mixins) and reference ``tokens``. They store
*facts the Scanner learns*, never judgements.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hades.shared_kernel.persistence.database import Base
from hades.shared_kernel.persistence.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class TokenMetadataRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The complete metadata gathered for a token (one current row per token)."""

    __tablename__ = "token_metadata"

    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(200))
    symbol: Mapped[str | None] = mapped_column(String(64))
    decimals: Mapped[int | None] = mapped_column(Integer)
    total_supply: Mapped[Decimal | None] = mapped_column(Numeric(40, 0))
    logo_uri: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    twitter: Mapped[str | None] = mapped_column(Text)
    telegram: Mapped[str | None] = mapped_column(Text)
    discord: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    program: Mapped[str | None] = mapped_column(String(64))
    update_authority: Mapped[str | None] = mapped_column(String(64))
    mint_authority: Mapped[str | None] = mapped_column(String(64))
    freeze_authority: Mapped[str | None] = mapped_column(String(64))
    is_mutable: Mapped[bool | None] = mapped_column(Boolean)
    created_on_chain_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )


class DataAnomaly(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A datum the Quality Validator rejected or flagged (audit of data health).

    One row per distinct *problem* — ``(subject, kind, field)`` — not per
    sighting. The Scanner re-processes the same mint on every rediscovery, so an
    append-only log turned one persistent issue into thousands of identical rows
    and made the total say more about scan frequency than about data health.
    ``occurrences`` carries the count that the extra rows used to carry, and
    ``detected_at`` tracks the most recent sighting.
    """

    __tablename__ = "data_anomalies"
    __table_args__ = (
        UniqueConstraint("subject", "kind", "field", name="uq_data_anomalies_problem"),
    )

    subject: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    field: Mapped[str] = mapped_column(String(120), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )


class TokenSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A point-in-time snapshot of a token's full observed state (History Builder).

    Stored every interval so any historical moment can be reconstructed. The
    ``state`` blob holds the merged market + feature view; ``schema_version``
    lets it evolve without breaking older rows.
    """

    __tablename__ = "token_snapshots"

    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tokens.id", ondelete="CASCADE"), index=True, nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
