"""exploration ledger — the append-only accounting of the cold-start programme

Adds ``exploration_grants``: one row per exploration trade the Risk Manager
approved, written when the budget is charged.

There is exactly one table and no counter anywhere else, deliberately. Spend is
always derived by aggregating these rows, because a total held in a process
resets on restart — which would silently re-authorise the day's budget on every
deploy, and would present weeks later as "the programme is spending past its
ceiling" with nothing in the logs to explain it.

Built from the ORM model so the migration cannot drift from ``Base.metadata``.

Revision ID: 0011_exploration_ledger
Revises: 0010_knowledge_tables
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

from hades.shared_kernel.persistence.models import ExplorationGrantRecord

revision = "0011_exploration_ledger"
down_revision = "0010_knowledge_tables"
branch_labels = None
depends_on = None

_TABLES = (ExplorationGrantRecord.__table__,)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=True)
