"""Knowledge tables — the platform's permanent memory.

Three tables, with deliberately different lifecycles:

* ``knowledge_observations`` — append-only. Everything the platform has ever
  learned, tagged with provenance and verification strength. Never updated,
  never deleted; that is what makes it usable as evidence after the fact.
* ``knowledge_lessons`` — append-only. Completed decision→outcome pairs: the
  ground-truth training samples. Each row carries the feature vector *as it
  stood when the decision was taken*, which is what keeps the training signal
  free of the future.
* ``knowledge_decisions`` — the only mutable one, and only in the sense that a
  row is deleted when its outcome arrives. It is the durable parking space for
  decisions awaiting settlement, so a worker restart cannot orphan the positions
  held longest.

All follow the platform rule (UUIDv7 pk + timestamps via mixins). Nothing here
represents an order, a position or a balance — Knowledge has no such concepts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hades.shared_kernel.persistence.database import Base
from hades.shared_kernel.persistence.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeObservationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable thing the platform knows."""

    __tablename__ = "knowledge_observations"
    __table_args__ = (
        # The dashboard and the read API both slice by "what do we know about
        # this subject, newest first"; the composite keeps that a single index
        # scan instead of a sort over the subject's whole history.
        Index("ix_knowledge_obs_subject_time", "subject", "occurred_at"),
        Index("ix_knowledge_obs_source_time", "source", "occurred_at"),
    )

    source: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    verification: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    features: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


class KnowledgeLessonRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A decision married to its realised outcome: a ground-truth sample."""

    __tablename__ = "knowledge_lessons"
    __table_args__ = (
        # At-least-once delivery means the same settlement can arrive twice. The
        # journal's take-once semantics is the first defence; this constraint is
        # the one that holds even if a redelivery races a restart, because a
        # duplicated lesson silently doubles that trade's weight in every future
        # dataset — a corruption that would never surface as an error.
        UniqueConstraint("ref", name="uq_knowledge_lessons_ref"),
    )

    ref: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict, nullable=False)
    beliefs: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict, nullable=False)
    tags: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)
    realized_roi: Mapped[float] = mapped_column(Float, index=True, nullable=False)
    label_roi_positive: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False)
    label_hit_tp: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    label_hit_sl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    verification: Mapped[str] = mapped_column(String(16), default="realised", nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


class KnowledgeDecisionRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A decision's frozen evidence, awaiting its outcome.

    Deleted on settlement — this table's size is the count of open decisions,
    which makes an unbounded growth here a visible, alertable symptom of the
    learning loop breaking rather than a silent one.
    """

    __tablename__ = "knowledge_decisions"
    __table_args__ = (UniqueConstraint("ref", name="uq_knowledge_decisions_ref"),)

    ref: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    features: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict, nullable=False)
    beliefs: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict, nullable=False)
    tags: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


__all__ = [
    "KnowledgeDecisionRecord",
    "KnowledgeLessonRecord",
    "KnowledgeObservationRecord",
]
