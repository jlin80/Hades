"""features (token_id, computed_at) — the index the funnel needed and never had

The funnel endpoint counts ``distinct token_id`` in ``features`` over a window.
Neither single-column index can serve that: ``computed_at`` is not selective,
because the feature store has no retention and a 24 h window covers essentially
every row, so the planner scans ``ix_features_token_id`` for its ordering and
then visits the heap for all of them to test ``computed_at``.

Measured on CT 203 before this migration:

    Aggregate  (actual time=36204.228..36204.232)
      ->  Index Scan using ix_features_token_id on features
            (actual time=107.864..28810.133 rows=618082)
            Filter: (computed_at >= now() - '24:00:00')
            Buffers: shared hit=17052 read=731

36.2 s, against a dashboard that polls the endpoint far more often than that, so
the queries stacked: six concurrent copies were observed, ageing to 60 s, holding
connections the Security Engine's own reads then queued behind.

Written as raw SQL rather than built from ``Base.metadata`` like the migrations
before it, for one reason: ``CONCURRENTLY``. ``features`` takes continuous
inserts from the Scanner, and an ordinary ``CREATE INDEX`` holds a lock that
blocks them for the whole build. An index added to make the platform stop
starving itself should not stall the write path on its way in. ``CONCURRENTLY``
cannot run inside a transaction, hence the AUTOCOMMIT execution option.

Revision ID: 0012_features_funnel_index
Revises: 0011_exploration_ledger
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0012_features_funnel_index"
down_revision = "0011_exploration_ledger"
branch_labels = None
depends_on = None

_INDEX = "ix_features_token_id_computed_at"


def upgrade() -> None:
    bind = op.get_bind().execution_options(isolation_level="AUTOCOMMIT")
    bind.exec_driver_sql(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON features (token_id, computed_at)"
    )


def downgrade() -> None:
    bind = op.get_bind().execution_options(isolation_level="AUTOCOMMIT")
    bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
