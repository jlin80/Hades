"""Knowledge store adapters — Postgres for real deployments, in-memory twins.

Both halves of each pair satisfy the same port and are exercised by the same
tests, which is what keeps the in-memory versions honest: they are used in
anger by the whole suite, not written once and left to rot.

Two behaviours are worth calling out because they are correctness, not
optimisation:

* **Idempotent appends.** The bus is at-least-once, so every writer here
  tolerates a redelivered batch. Lessons and decisions are keyed by ``ref`` with
  a unique constraint and an ``ON CONFLICT DO NOTHING`` insert; a duplicate is a
  no-op rather than a second row. A duplicated lesson would not raise anything —
  it would quietly double that trade's weight in every dataset built afterwards.

* **Take-once settlement.** :meth:`PostgresDecisionJournalStore.take` deletes and
  returns in a single statement (``DELETE ... RETURNING``), so two workers
  processing the same settlement cannot both come away with the decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hades.contexts.knowledge.domain.models import (
    VERIFICATION_RANK,
    Decision,
    KnowledgeKind,
    KnowledgeQuery,
    KnowledgeSource,
    KnowledgeStats,
    Lesson,
    Observation,
    SubjectType,
    Verification,
)
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models import (
    KnowledgeDecisionRecord,
    KnowledgeLessonRecord,
    KnowledgeObservationRecord,
)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --- observations ------------------------------------------------------------


class PostgresKnowledgeStore:
    """Append-only ``knowledge_observations`` adapter."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def append(self, observations: Sequence[Observation]) -> None:
        if not observations:
            return
        async with self._db.session() as session:
            session.add_all(
                [
                    KnowledgeObservationRecord(
                        source=o.source.value,
                        kind=o.kind.value,
                        verification=o.verification.value,
                        subject=o.subject,
                        subject_type=o.subject_type.value,
                        occurred_at=o.occurred_at,
                        recorded_at=o.recorded_at,
                        payload=o.payload,
                        features=o.features,
                        correlation_id=o.correlation_id,
                    )
                    for o in observations
                ]
            )

    async def query(self, spec: KnowledgeQuery) -> list[Observation]:
        stmt: Select[tuple[KnowledgeObservationRecord]] = select(KnowledgeObservationRecord)
        if spec.source is not None:
            stmt = stmt.where(KnowledgeObservationRecord.source == spec.source.value)
        if spec.kind is not None:
            stmt = stmt.where(KnowledgeObservationRecord.kind == spec.kind.value)
        if spec.subject is not None:
            stmt = stmt.where(KnowledgeObservationRecord.subject == spec.subject)
        if spec.subject_type is not None:
            stmt = stmt.where(KnowledgeObservationRecord.subject_type == spec.subject_type.value)
        if spec.min_verification is not None:
            # Verification is stored as its label, so a SQL ">=" would compare
            # strings alphabetically and quietly return the wrong set. The
            # ordering lives in VERIFICATION_RANK; the filter is expressed as
            # membership in the levels that satisfy the floor.
            floor = VERIFICATION_RANK[spec.min_verification]
            allowed = [v.value for v, rank in VERIFICATION_RANK.items() if rank >= floor]
            stmt = stmt.where(KnowledgeObservationRecord.verification.in_(allowed))
        if spec.since is not None:
            stmt = stmt.where(KnowledgeObservationRecord.occurred_at >= spec.since)
        if spec.until is not None:
            stmt = stmt.where(KnowledgeObservationRecord.occurred_at <= spec.until)
        stmt = (
            stmt.order_by(KnowledgeObservationRecord.occurred_at.desc())
            .limit(spec.limit)
            .offset(spec.offset)
        )
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_observation(row) for row in rows]

    async def stats(self) -> KnowledgeStats:
        async with self._db.session() as session:
            total = await session.scalar(
                select(func.count()).select_from(KnowledgeObservationRecord)
            )
            by_source = (
                await session.execute(
                    select(KnowledgeObservationRecord.source, func.count()).group_by(
                        KnowledgeObservationRecord.source
                    )
                )
            ).all()
            by_kind = (
                await session.execute(
                    select(KnowledgeObservationRecord.kind, func.count()).group_by(
                        KnowledgeObservationRecord.kind
                    )
                )
            ).all()
        return KnowledgeStats(
            total=int(total or 0),
            by_source={str(name): int(count) for name, count in by_source},
            by_kind={str(name): int(count) for name, count in by_kind},
        )


class InMemoryKnowledgeStore:
    """In-memory observation store for tests and single-process dev."""

    def __init__(self) -> None:
        self._rows: list[Observation] = []

    @property
    def rows(self) -> list[Observation]:
        return list(self._rows)

    async def append(self, observations: Sequence[Observation]) -> None:
        self._rows.extend(observations)

    async def query(self, spec: KnowledgeQuery) -> list[Observation]:
        matched = [o for o in self._rows if spec.accepts(o)]
        matched.sort(key=lambda o: o.occurred_at, reverse=True)
        return matched[spec.offset : spec.offset + spec.limit]

    async def stats(self) -> KnowledgeStats:
        by_source: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for row in self._rows:
            by_source[row.source.value] = by_source.get(row.source.value, 0) + 1
            by_kind[row.kind.value] = by_kind.get(row.kind.value, 0) + 1
        return KnowledgeStats(total=len(self._rows), by_source=by_source, by_kind=by_kind)


# --- lessons -----------------------------------------------------------------


class PostgresLessonStore:
    """Append-only ``knowledge_lessons`` adapter, idempotent on ``ref``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def append(self, lessons: Sequence[Lesson]) -> None:
        if not lessons:
            return
        rows = [
            {
                "ref": lesson.ref,
                "subject": lesson.subject,
                "decided_at": lesson.decided_at,
                "settled_at": lesson.settled_at,
                "features": lesson.features,
                "beliefs": lesson.beliefs,
                "tags": lesson.tags,
                "realized_roi": lesson.realized_roi,
                "label_roi_positive": lesson.label_roi_positive,
                "label_hit_tp": lesson.label_hit_tp,
                "label_hit_sl": lesson.label_hit_sl,
                "reason": lesson.reason,
                "verification": lesson.verification.value,
                "correlation_id": lesson.correlation_id,
            }
            for lesson in lessons
        ]
        stmt = (
            pg_insert(KnowledgeLessonRecord)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["ref"])
        )
        async with self._db.session() as session:
            await session.execute(stmt)

    async def load(self, *, limit: int = 10_000, since_iso: str | None = None) -> list[Lesson]:
        stmt: Select[tuple[KnowledgeLessonRecord]] = select(KnowledgeLessonRecord)
        since = _parse_iso(since_iso)
        if since is not None:
            stmt = stmt.where(KnowledgeLessonRecord.decided_at >= since)
        # Oldest-first: a training set must preserve chronology, or a
        # walk-forward split trains on the future.
        stmt = stmt.order_by(KnowledgeLessonRecord.decided_at.asc()).limit(limit)
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_lesson(row) for row in rows]

    async def count(self) -> int:
        async with self._db.session() as session:
            total = await session.scalar(select(func.count()).select_from(KnowledgeLessonRecord))
        return int(total or 0)


class InMemoryLessonStore:
    """In-memory lesson store, idempotent on ``ref`` like its Postgres twin."""

    def __init__(self) -> None:
        self._rows: list[Lesson] = []
        self._refs: set[str] = set()

    @property
    def rows(self) -> list[Lesson]:
        return list(self._rows)

    async def append(self, lessons: Sequence[Lesson]) -> None:
        for lesson in lessons:
            if lesson.ref in self._refs:
                continue
            self._refs.add(lesson.ref)
            self._rows.append(lesson)

    async def load(self, *, limit: int = 10_000, since_iso: str | None = None) -> list[Lesson]:
        since = _parse_iso(since_iso)
        rows = [r for r in self._rows if since is None or r.decided_at >= since]
        rows.sort(key=lambda r: r.decided_at)
        return rows[:limit]

    async def count(self) -> int:
        return len(self._rows)


# --- decision journal --------------------------------------------------------


class PostgresDecisionJournalStore:
    """Durable parking for decisions awaiting settlement."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def save(self, decision: Decision) -> None:
        stmt = (
            pg_insert(KnowledgeDecisionRecord)
            .values(
                ref=decision.ref,
                subject=decision.subject,
                decided_at=decision.decided_at,
                features=decision.features,
                beliefs=decision.beliefs,
                tags=decision.tags,
                correlation_id=decision.correlation_id,
            )
            .on_conflict_do_nothing(index_elements=["ref"])
        )
        async with self._db.session() as session:
            await session.execute(stmt)

    async def take(self, ref: str) -> Decision | None:
        # Delete-and-return in one statement: two workers handling the same
        # settlement cannot both come away holding the decision, so a lesson
        # cannot be recorded twice.
        stmt = (
            delete(KnowledgeDecisionRecord)
            .where(KnowledgeDecisionRecord.ref == ref)
            .returning(KnowledgeDecisionRecord)
        )
        async with self._db.session() as session:
            row = (await session.execute(stmt)).scalars().first()
        return _to_decision(row) if row is not None else None

    async def load_open(self, *, limit: int = 10_000) -> list[Decision]:
        stmt = (
            select(KnowledgeDecisionRecord)
            .order_by(KnowledgeDecisionRecord.decided_at.asc())
            .limit(limit)
        )
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_decision(row) for row in rows]

    async def count_open(self) -> int:
        async with self._db.session() as session:
            total = await session.scalar(select(func.count()).select_from(KnowledgeDecisionRecord))
        return int(total or 0)


class InMemoryDecisionJournalStore:
    """In-memory journal with the same take-once semantics."""

    def __init__(self) -> None:
        self._rows: dict[str, Decision] = {}

    async def save(self, decision: Decision) -> None:
        self._rows.setdefault(decision.ref, decision)

    async def take(self, ref: str) -> Decision | None:
        return self._rows.pop(ref, None)

    async def load_open(self, *, limit: int = 10_000) -> list[Decision]:
        rows = sorted(self._rows.values(), key=lambda d: d.decided_at)
        return rows[:limit]

    async def count_open(self) -> int:
        return len(self._rows)


# --- row → domain ------------------------------------------------------------


def _to_observation(row: KnowledgeObservationRecord) -> Observation:
    return Observation(
        source=KnowledgeSource(row.source),
        kind=KnowledgeKind(row.kind),
        verification=Verification(row.verification),
        subject=row.subject,
        subject_type=SubjectType(row.subject_type),
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        payload=dict(row.payload or {}),
        features=dict(row.features or {}),
        correlation_id=row.correlation_id,
    )


def _to_lesson(row: KnowledgeLessonRecord) -> Lesson:
    return Lesson(
        ref=row.ref,
        subject=row.subject,
        decided_at=row.decided_at,
        settled_at=row.settled_at,
        features=dict(row.features or {}),
        beliefs=dict(row.beliefs or {}),
        tags=dict(row.tags or {}),
        realized_roi=row.realized_roi,
        label_roi_positive=row.label_roi_positive,
        label_hit_tp=row.label_hit_tp,
        label_hit_sl=row.label_hit_sl,
        reason=row.reason,
        verification=Verification(row.verification),
        correlation_id=row.correlation_id,
    )


def _to_decision(row: KnowledgeDecisionRecord) -> Decision:
    return Decision(
        ref=row.ref,
        subject=row.subject,
        decided_at=row.decided_at,
        features=dict(row.features or {}),
        beliefs=dict(row.beliefs or {}),
        tags=dict(row.tags or {}),
        correlation_id=row.correlation_id,
    )


__all__ = [
    "InMemoryDecisionJournalStore",
    "InMemoryKnowledgeStore",
    "InMemoryLessonStore",
    "PostgresDecisionJournalStore",
    "PostgresKnowledgeStore",
    "PostgresLessonStore",
]
