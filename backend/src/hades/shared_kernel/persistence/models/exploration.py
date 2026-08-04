"""Exploration ledger — the append-only accounting of the cold-start programme.

One table, and it is the *only* record of what exploration has spent. Spend is
always derived from it by aggregation, never accumulated in a process variable:
an in-memory total resets on restart, which would silently re-authorise the day's
budget on every deploy — a bug that presents as "the programme is spending more
than its ceiling" long after the deploy that caused it.

The row is written when the Risk Manager *approves* an exploration grant, not
when the grant is issued, so a candidate that clears exploration and is then
vetoed by an allocation rule costs the budget nothing.

Nothing here names an order, a fill or a wallet. A ledger row is a statement
about a budget, not about a trade: the trade itself is recorded by Execution and
Portfolio like any other, and by permanent memory as a lesson.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from hades.shared_kernel.persistence.database import Base
from hades.shared_kernel.persistence.models._mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ExplorationGrantRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One exploration trade, charged to the budget."""

    __tablename__ = "exploration_grants"
    __table_args__ = (
        # Every budget question is "how much since <time>", over the day, the
        # week and all of history. The index makes each of those a range scan
        # rather than a sequential read that grows with the programme.
        Index("ix_exploration_grants_time", "granted_at"),
    )

    subject: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notional_usd: Mapped[float] = mapped_column(Float, nullable=False)
    #: The under-sampled cohort that justified the trade, when there was one.
    #: Kept so the programme can be audited on its own terms afterwards: did it
    #: actually spread the budget across cohorts, or pour it into one developer?
    cohort_key: Mapped[str | None] = mapped_column(String(160), index=True)
    reason: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


__all__ = ["ExplorationGrantRecord"]
