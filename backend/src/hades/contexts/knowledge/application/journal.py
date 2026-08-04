"""The Decision Journal — where the learning loop is actually closed.

This is the smallest component in the context and the reason it exists.

The platform's defect was never that it lacked a training pipeline; it had a
good one, with a dataset builder, a training engine and a validation gauntlet.
What it lacked was a **write path for ground truth**. Trades opened, ran and
closed, and their realised results reached no ledger, so the only samples that
ever accumulated were weak negatives from security rejections — a single-class
dataset, on which AUC is undefined and no validation gate can be met. Training
was structurally impossible, and no amount of threshold tuning could change it.

The journal fixes that with two rules and one join:

**Freeze early.** :meth:`record_decision` stores the feature vector *at the
moment the decision is taken*. The tempting alternative — wait for the close,
then ask the feature store what the token looks like — trains on the state of
the world at the moment of sale, labelled with the outcome of the trade. That is
temporal leakage. It yields beautiful offline metrics and a model that cannot
work, because at decision time those features do not exist. The journal makes
the leaking version impossible to write: there is nowhere to fetch features from
at settle time, because settling takes an :class:`Outcome` and nothing else.

**Resolve once.** :meth:`settle` *takes* the decision out of the journal. The
bus guarantees at-least-once delivery, so a redelivered outcome must find an
empty slot and produce nothing, rather than a second copy of the same lesson
quietly doubling that trade's weight in every future dataset.
"""

from __future__ import annotations

from hades.contexts.knowledge.application.metrics import KnowledgeMetrics
from hades.contexts.knowledge.domain.events import DecisionRecorded, LessonLearned
from hades.contexts.knowledge.domain.models import Decision, Lesson, Outcome
from hades.contexts.knowledge.domain.ports import DecisionJournalStore, LessonStore
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("knowledge.journal")


class DecisionJournal:
    """Pairs a decision's frozen evidence with its eventual real outcome."""

    def __init__(
        self,
        journal: DecisionJournalStore,
        lessons: LessonStore,
        *,
        event_bus: EventBus | None = None,
        metrics: KnowledgeMetrics | None = None,
    ) -> None:
        self._journal = journal
        self._lessons = lessons
        self._bus = event_bus
        self._metrics = metrics

    async def record_decision(self, decision: Decision) -> None:
        """Freeze the evidence behind a decision, pending its outcome."""
        if not decision.features:
            # Worth recording anyway — the decision still happened and its
            # absence would misreport the open count — but a featureless
            # decision yields a lesson that teaches nothing, so it is called out
            # rather than accumulating invisibly.
            _logger.warning("decision_without_features", ref=decision.ref, subject=decision.subject)
        await self._journal.save(decision)
        if self._metrics is not None:
            self._metrics.decisions_recorded.inc()
        if self._bus is not None:
            await self._bus.publish(
                DecisionRecorded(
                    aggregate_id=new_id(),
                    ref=decision.ref,
                    subject=decision.subject,
                    feature_count=len(decision.features),
                )
            )
        _logger.info(
            "decision_recorded",
            ref=decision.ref,
            subject=decision.subject,
            features=len(decision.features),
        )

    async def settle(self, outcome: Outcome) -> Lesson | None:
        """Resolve a decision with what happened. Returns the lesson, if any.

        ``None`` means there was nothing to resolve — either the outcome belongs
        to a decision this journal never saw, or it is a redelivery of one
        already settled. Both are normal; neither is an error.
        """
        decision = await self._journal.take(outcome.ref)
        if decision is None:
            # An orphan is not a crash, but it *is* a signal: a steady stream of
            # them means the reference the producer quotes at settle time has
            # drifted from the one it quoted at decision time, and every lesson
            # is being silently lost.
            if self._metrics is not None:
                self._metrics.orphan_outcomes.inc()
            _logger.info("outcome_without_decision", ref=outcome.ref)
            return None

        lesson = Lesson.join(decision, outcome)
        await self._lessons.append([lesson])
        if self._metrics is not None:
            self._metrics.lessons_learned.inc()
        if self._bus is not None:
            await self._bus.publish(LessonLearned(aggregate_id=new_id(), lesson=lesson))
        _logger.info(
            "lesson_learned",
            ref=lesson.ref,
            subject=lesson.subject,
            realized_roi=round(lesson.realized_roi, 6),
            positive=lesson.label_roi_positive,
            held_seconds=round(lesson.holding_seconds, 1),
            features=len(lesson.features),
        )
        return lesson

    async def open_count(self) -> int:
        return await self._journal.count_open()


__all__ = ["DecisionJournal"]
