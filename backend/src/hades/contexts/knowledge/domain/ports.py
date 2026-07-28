"""Knowledge ports — the abstractions the application layer depends on.

Clean Architecture: the domain declares what it needs, infrastructure supplies
it, and the dependency arrow points inwards. These are :class:`Protocol`s rather
than ABCs so an adapter satisfies them structurally, without importing this
module — the in-memory twins used throughout the tests are ordinary classes.

Every store here is **append-only**. There is no ``update`` and no ``delete``,
and that is a domain rule, not an oversight: a memory that can be rewritten
cannot be used as evidence, and the whole justification for this context is that
its contents are trustworthy after the fact. Compaction, if it is ever needed,
belongs in an explicit archival job that can be audited — not behind a store
method that any caller could reach.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hades.contexts.knowledge.domain.models import (
    Decision,
    KnowledgeQuery,
    KnowledgeStats,
    Lesson,
    Observation,
)


@runtime_checkable
class KnowledgeStore(Protocol):
    """Append-only persistence for observations."""

    async def append(self, observations: Sequence[Observation]) -> None:
        """Store a batch. Must tolerate re-delivery of the same batch."""
        ...

    async def query(self, spec: KnowledgeQuery) -> list[Observation]:
        """Read back, newest-first, honouring every set filter."""
        ...

    async def stats(self) -> KnowledgeStats:
        """Census of the store. Cheap enough to poll for the dashboard."""
        ...


@runtime_checkable
class LessonStore(Protocol):
    """Append-only persistence for completed decision→outcome pairs."""

    async def append(self, lessons: Sequence[Lesson]) -> None: ...

    async def load(self, *, limit: int = 10_000, since_iso: str | None = None) -> list[Lesson]:
        """Oldest-first, so a training set preserves chronology."""
        ...

    async def count(self) -> int: ...


@runtime_checkable
class DecisionJournalStore(Protocol):
    """Durable parking for decisions awaiting their outcome.

    This is the one piece of Knowledge state that must survive a restart and is
    not append-only in spirit: a decision is written when taken and resolved when
    its outcome lands. It is kept behind its own port precisely so the
    append-only guarantee of :class:`KnowledgeStore` stays absolute.

    Without durability here, every worker restart silently orphans the open
    decisions — their trades would close hours later against a journal that had
    forgotten why they were opened, and the lessons would be lost exactly for the
    positions held longest.
    """

    async def save(self, decision: Decision) -> None: ...

    async def take(self, ref: str) -> Decision | None:
        """Fetch and remove the decision for ``ref``, or ``None`` if unknown.

        Removal is part of the contract: an outcome resolves a decision exactly
        once, and at-least-once event delivery means the second attempt must find
        nothing rather than emit a duplicate lesson.
        """
        ...

    async def load_open(self, *, limit: int = 10_000) -> list[Decision]:
        """Every unresolved decision, for rehydration at startup."""
        ...

    async def count_open(self) -> int: ...


__all__ = ["DecisionJournalStore", "KnowledgeStore", "LessonStore"]
