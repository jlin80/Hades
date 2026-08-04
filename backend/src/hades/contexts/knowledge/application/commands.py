"""CQRS messages for the Knowledge context.

Writes are commands, reads are queries, and the two never share a path. The
separation earns its keep here more than in most contexts: the write side is a
hot, fire-and-forget ingestion path fed by the event bus, while the read side
serves the dashboard and the training loop. They have opposite performance
shapes and opposite failure postures — an ingestion that fails must not take
down a producer, whereas a read that fails must say so loudly.
"""

from __future__ import annotations

from hades.contexts.knowledge.domain.models import (
    Decision,
    KnowledgeEnvelope,
    KnowledgeQuery,
    Outcome,
)
from hades.shared_kernel.cqrs.command import Command
from hades.shared_kernel.cqrs.query import Query


class RecordKnowledgeCommand(Command):
    """Accept a batch of facts into permanent memory."""

    envelopes: tuple[KnowledgeEnvelope, ...]


class RecordDecisionCommand(Command):
    """Freeze the evidence behind a decision, pending its outcome."""

    decision: Decision


class SettleOutcomeCommand(Command):
    """Resolve a previously-recorded decision with what actually happened."""

    outcome: Outcome


class GetKnowledgeQuery(Query):
    """Read observations back out of the memory."""

    spec: KnowledgeQuery


class GetKnowledgeStatsQuery(Query):
    """Census of the memory: totals, provenance mix and training readiness."""


class GetLessonsQuery(Query):
    """Completed decision→outcome pairs, oldest-first."""

    limit: int = 1_000
    since_iso: str | None = None


__all__ = [
    "GetKnowledgeQuery",
    "GetKnowledgeStatsQuery",
    "GetLessonsQuery",
    "RecordDecisionCommand",
    "RecordKnowledgeCommand",
    "SettleOutcomeCommand",
]
