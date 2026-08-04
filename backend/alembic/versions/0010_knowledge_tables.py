"""knowledge tables — the platform's permanent memory

Adds the three tables of the Knowledge bounded context:

* ``knowledge_observations`` — append-only, everything the platform has learned,
  tagged with provenance and verification strength.
* ``knowledge_lessons`` — append-only, completed decision→outcome pairs. These
  are the ground-truth training samples the AI Committee never had: until now the
  outcome ledger only ever received weak negatives from security rejections, a
  single-class dataset on which no model could be validated.
* ``knowledge_decisions`` — durable parking for decisions awaiting settlement, so
  a worker restart cannot orphan the positions held longest.

Both ``knowledge_lessons`` and ``knowledge_decisions`` carry a unique constraint
on ``ref``. That is correctness, not tidiness: the bus is at-least-once, and a
duplicated lesson would silently double that trade's weight in every dataset
built afterwards — a corruption that never surfaces as an error.

Built from the ORM models so the migration cannot drift from ``Base.metadata``.

Revision ID: 0010_knowledge_tables
Revises: 0009_data_anomalies_dedup
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

from hades.shared_kernel.persistence.models import (
    KnowledgeDecisionRecord,
    KnowledgeLessonRecord,
    KnowledgeObservationRecord,
)

revision = "0010_knowledge_tables"
down_revision = "0009_data_anomalies_dedup"
branch_labels = None
depends_on = None

_TABLES = (
    KnowledgeObservationRecord.__table__,
    KnowledgeLessonRecord.__table__,
    KnowledgeDecisionRecord.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=True)
