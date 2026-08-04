"""collapse data_anomalies to one row per distinct problem

``record_anomaly`` was an unconditional INSERT, and the Scanner re-validates a
mint every time it rediscovers it. One persistent issue — an unreachable RPC
endpoint leaving ``decimals`` uncollected — therefore wrote a fresh row per
rescan, and the dashboard's "anomalies" total measured scan frequency rather
than data health (a real deployment reached 2582 rows that were, in substance,
one problem repeated).

This adds ``occurrences`` + ``first_detected_at``, folds the existing history
into one row per ``(subject, kind, field)`` keeping the earliest and latest
sightings and the newest detail, then enforces uniqueness so the new upsert path
can never re-inflate the table.

Revision ID: 0009_data_anomalies_dedup
Revises: 0008_portfolio_state
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_data_anomalies_dedup"
down_revision = "0008_portfolio_state"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_data_anomalies_problem"

# Keep the row that was seen most recently for each problem, giving it the whole
# group's count and the group's earliest sighting; delete the rest. Done in SQL
# so the collapse is one statement per step regardless of table size.
_COLLAPSE = """
WITH grouped AS (
    SELECT
        subject,
        kind,
        field,
        COUNT(*)         AS total,
        MIN(detected_at) AS first_at,
        MAX(detected_at) AS last_at
    FROM data_anomalies
    GROUP BY subject, kind, field
),
survivor AS (
    SELECT DISTINCT ON (subject, kind, field) id, subject, kind, field
    FROM data_anomalies
    ORDER BY subject, kind, field, detected_at DESC, id DESC
)
UPDATE data_anomalies AS a
SET occurrences = g.total,
    first_detected_at = g.first_at,
    detected_at = g.last_at
FROM survivor s
JOIN grouped g
  ON g.subject = s.subject AND g.kind = s.kind AND g.field = s.field
WHERE a.id = s.id
"""

# Keep only the ids that won their group; everything else was a repeat sighting
# whose count now lives on the survivor.
_PRUNE = """
DELETE FROM data_anomalies
WHERE id NOT IN (
    SELECT DISTINCT ON (subject, kind, field) id
    FROM data_anomalies
    ORDER BY subject, kind, field, detected_at DESC, id DESC
)
"""


def upgrade() -> None:
    op.add_column(
        "data_anomalies",
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "data_anomalies",
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill before the collapse so the NOT NULL below is satisfiable.
    op.execute("UPDATE data_anomalies SET first_detected_at = detected_at")
    op.execute(_COLLAPSE)
    op.execute(_PRUNE)
    op.alter_column("data_anomalies", "first_detected_at", nullable=False)
    op.create_unique_constraint(_CONSTRAINT, "data_anomalies", ["subject", "kind", "field"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "data_anomalies", type_="unique")
    op.drop_column("data_anomalies", "first_detected_at")
    op.drop_column("data_anomalies", "occurrences")
