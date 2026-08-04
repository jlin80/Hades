"""CQRS handlers — the bus-facing façade over the recorder, journal and stores.

Thin by design. All behaviour lives in :mod:`..recorder` and :mod:`..journal`;
these adapt it to the command/query buses so the API layer can stay ignorant of
how Knowledge is assembled. Read handlers touch no writer and emit no events,
which is the property that makes the read side safe to expose over HTTP.
"""

from __future__ import annotations

from hades.contexts.knowledge.application.commands import (
    GetKnowledgeQuery,
    GetKnowledgeStatsQuery,
    GetLessonsQuery,
    RecordDecisionCommand,
    RecordKnowledgeCommand,
    SettleOutcomeCommand,
)
from hades.contexts.knowledge.application.journal import DecisionJournal
from hades.contexts.knowledge.application.recorder import KnowledgeRecorder
from hades.contexts.knowledge.domain.models import (
    KnowledgeStats,
    Lesson,
    Observation,
)
from hades.contexts.knowledge.domain.ports import KnowledgeStore, LessonStore
from hades.shared_kernel.cqrs.command import CommandHandler
from hades.shared_kernel.cqrs.query import QueryHandler


class RecordKnowledgeHandler(CommandHandler[RecordKnowledgeCommand, list[Observation]]):
    """Accept facts into permanent memory."""

    def __init__(self, recorder: KnowledgeRecorder) -> None:
        self._recorder = recorder

    async def handle(self, command: RecordKnowledgeCommand) -> list[Observation]:
        return await self._recorder.record(command.envelopes)


class RecordDecisionHandler(CommandHandler[RecordDecisionCommand, None]):
    """Freeze the evidence behind a decision."""

    def __init__(self, journal: DecisionJournal) -> None:
        self._journal = journal

    async def handle(self, command: RecordDecisionCommand) -> None:
        await self._journal.record_decision(command.decision)


class SettleOutcomeHandler(CommandHandler[SettleOutcomeCommand, Lesson | None]):
    """Resolve a decision with its realised outcome."""

    def __init__(self, journal: DecisionJournal) -> None:
        self._journal = journal

    async def handle(self, command: SettleOutcomeCommand) -> Lesson | None:
        return await self._journal.settle(command.outcome)


class GetKnowledgeHandler(QueryHandler[GetKnowledgeQuery, list[Observation]]):
    """Read observations back out of the memory."""

    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    async def handle(self, query: GetKnowledgeQuery) -> list[Observation]:
        return await self._store.query(query.spec)


class GetKnowledgeStatsHandler(QueryHandler[GetKnowledgeStatsQuery, KnowledgeStats]):
    """Census of the memory, including whether it can support training."""

    def __init__(
        self, store: KnowledgeStore, lessons: LessonStore, journal: DecisionJournal
    ) -> None:
        self._store = store
        self._lessons = lessons
        self._journal = journal

    async def handle(self, query: GetKnowledgeStatsQuery) -> KnowledgeStats:
        base = await self._store.stats()
        # The store counts observations; lessons and open decisions live in their
        # own stores, so the census is completed here rather than forcing every
        # KnowledgeStore adapter to know about the other two.
        recent = await self._lessons.load(limit=10_000)
        positives = sum(1 for lesson in recent if lesson.label_roi_positive)
        realised = sum(1 for lesson in recent if lesson.verification.value == "realised")
        return base.model_copy(
            update={
                "lessons": await self._lessons.count(),
                "realised_lessons": realised,
                "positive_lessons": positives,
                "open_decisions": await self._journal.open_count(),
            }
        )


class GetLessonsHandler(QueryHandler[GetLessonsQuery, list[Lesson]]):
    """Completed decision→outcome pairs, oldest-first."""

    def __init__(self, lessons: LessonStore) -> None:
        self._lessons = lessons

    async def handle(self, query: GetLessonsQuery) -> list[Lesson]:
        return await self._lessons.load(limit=query.limit, since_iso=query.since_iso)


__all__ = [
    "GetKnowledgeHandler",
    "GetKnowledgeStatsHandler",
    "GetLessonsHandler",
    "RecordDecisionHandler",
    "RecordKnowledgeHandler",
    "SettleOutcomeHandler",
]
