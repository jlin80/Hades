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
starving itself should not stall the write path on its way in.

``CONCURRENTLY`` cannot run inside a transaction, and the way *not* to arrange
that is ``op.get_bind().execution_options(isolation_level="AUTOCOMMIT")`` — this
migration shipped that on its first attempt and failed the deploy, because
Alembic has already begun a transaction on the connection by the time
``upgrade()`` runs and SQLAlchemy refuses to change the isolation level of a
connection with one open. ``autocommit_block()`` is Alembic's own answer: it
commits the migration transaction, runs the body outside one, and opens a fresh
transaction afterwards for the version-table bookkeeping.

The invalid-index sweep before the create is not defensive padding. A
``CONCURRENTLY`` build that fails part-way leaves the index behind marked
``indisvalid = false``, and it is a real index as far as ``IF NOT EXISTS`` is
concerned — so without this, one failed build would make every later run skip
the create and report success while the query stayed unindexed.

Revision ID: 0012_features_funnel_index
Revises: 0011_exploration_ledger
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_features_funnel_index"
down_revision = "0011_exploration_ledger"
branch_labels = None
depends_on = None

_INDEX = "ix_features_token_id_computed_at"


_IS_INVALID = sa.text(
    "select not i.indisvalid from pg_class c "
    "join pg_index i on i.indexrelid = c.oid where c.relname = :name"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        if bind.scalar(_IS_INVALID, {"name": _INDEX}):
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        bind.exec_driver_sql(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON features (token_id, computed_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.get_bind().exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
