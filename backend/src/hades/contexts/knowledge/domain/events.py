"""Domain events emitted by the Knowledge context.

All four are statements about *the memory*, never about what to do with it.
Knowledge announces that it learned something; who cares, and what they do about
it, is not its business.

:class:`LessonLearned` is the one that matters. It is the event the platform has
been missing: the moment a decision's real outcome becomes available as a
labelled training sample. Nothing downstream is obliged to consume it — but the
AI Committee's outcome ledger finally has something to subscribe to, and that is
what unlocks training.
"""

from __future__ import annotations

from hades.contexts.knowledge.domain.models import (
    KnowledgeKind,
    KnowledgeSource,
    Lesson,
    Observation,
)
from hades.shared_kernel.domain.events import DomainEvent


class KnowledgeRecorded(DomainEvent):
    """A fact was accepted into permanent memory."""

    aggregate_type: str = "knowledge"
    observation: Observation


class KnowledgeRejected(DomainEvent):
    """A record was refused at the boundary, and why.

    Emitted rather than silently dropped: a producer whose records stop being
    accepted is a defect, and the platform learned the hard way that a swallowed
    exception at an ingestion boundary is indistinguishable from an idle system.
    """

    aggregate_type: str = "knowledge"
    source: KnowledgeSource
    kind: KnowledgeKind
    subject: str
    reason: str


class DecisionRecorded(DomainEvent):
    """The evidence behind a decision was frozen, awaiting its outcome."""

    aggregate_type: str = "knowledge"
    ref: str
    subject: str
    feature_count: int


class LessonLearned(DomainEvent):
    """A decision met its outcome: ground truth is available for training.

    This carries the full :class:`~..domain.models.Lesson` — features captured at
    decision time, labels from the settled result — so a consumer needs nothing
    else to build a training sample, and in particular never has to go back to a
    feature store and accidentally read the present.
    """

    aggregate_type: str = "knowledge"
    lesson: Lesson


__all__ = [
    "DecisionRecorded",
    "KnowledgeRecorded",
    "KnowledgeRejected",
    "LessonLearned",
]
