"""portfolio state table

Adds ``portfolio_state`` — the single durable row (one per trading mode) holding
the live book: cash, realised PnL, peak equity and the open positions.

Until now the Portfolio Manager kept all of that purely in memory, so every
worker restart silently reset cash to the configured starting balance and
dropped every open position. ``portfolio_history`` already existed but is an
append-only time series for charts, never read back — this is the state the
manager rehydrates from on startup (deferred item H3).

Keyed by ``mode`` so a paper book and a live book can never be confused for one
another. Built from the ORM model so the migration never drifts from
``Base.metadata``.

Revision ID: 0008_portfolio_state
Revises: 0007_config_snapshots
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op

from hades.shared_kernel.persistence.models import PortfolioStateRecord

revision = "0008_portfolio_state"
down_revision = "0007_config_snapshots"
branch_labels = None
depends_on = None

_TABLES = (PortfolioStateRecord.__table__,)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=True)
