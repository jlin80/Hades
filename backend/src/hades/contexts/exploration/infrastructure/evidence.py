"""Exploration's read window onto permanent memory.

This adapter implements :class:`EvidencePort` on top of the Knowledge context's
lesson store. It is the only file in this context that knows the memory exists,
and it depends solely on Knowledge's **domain** layer — the same sanctioned shape
of cross-context reading the AI Committee's enricher uses. The arrow points
Exploration → Knowledge; Knowledge imports nothing, so there is no cycle.

Two choices here are correctness rather than convenience.

**Only settled lessons are counted.** Not observations, not simulations, not the
Research Lab's backtests however numerous. The premise of the whole programme is
that the platform lacks *ground truth*; a backtest is a true statement about a
model, not about the market, and letting simulations count towards sufficiency
would let the lab talk the platform out of gathering the one kind of evidence it
cannot produce. The lesson store is the right source precisely because a row only
exists there when a trade opened, ran and settled.

**A failed read is reported, not smoothed.** It returns ``available=False``
rather than an empty census, because those two states must lead to opposite
behaviour: an empty memory means "keep exploring", an unreadable one means "stop
until you can tell". Collapsing them is how a broken platform and a young one
came to look identical for weeks.
"""

from __future__ import annotations

from collections.abc import Sequence

from hades.contexts.exploration.domain.models import EvidenceStatus, EvidenceTarget
from hades.contexts.knowledge.domain.models import Lesson
from hades.contexts.knowledge.domain.ports import LessonStore
from hades.shared_kernel.logging import get_logger

_logger = get_logger("exploration.evidence")

#: Ceiling on the ledger read. Sufficiency is a threshold in the tens, so a
#: window far above any plausible target is ample, and an unbounded read on the
#: hot path is not.
_MAX_LESSONS = 20_000


class KnowledgeEvidence:
    """Counts the platform's ground truth, globally and per cohort."""

    def __init__(self, lessons: LessonStore, target: EvidenceTarget) -> None:
        self._lessons = lessons
        self._target = target

    async def status(self, *, cohort_dimensions: Sequence[str]) -> EvidenceStatus:
        try:
            rows = await self._lessons.load(limit=_MAX_LESSONS)
        except Exception as exc:
            # Loud, and fail-closed: the caller stops granting on this.
            _logger.warning("exploration_evidence_unavailable", error=str(exc))
            return EvidenceStatus(target=self._target, available=False)

        positive = sum(1 for row in rows if row.label_roi_positive)
        return EvidenceStatus(
            lessons=len(rows),
            positive=positive,
            negative=len(rows) - positive,
            cohorts=_cohort_counts(rows, cohort_dimensions),
            target=self._target,
            available=True,
        )


def _cohort_counts(rows: Sequence[Lesson], dimensions: Sequence[str]) -> dict[str, int]:
    """How many settled lessons each cohort key has.

    Keys are ``"<dimension>=<value>"`` — the same construction the policy uses to
    look one up, kept in step by being the only two places that build it. Tags
    absent from a lesson simply contribute nothing: a trade the platform could
    not attribute is not evidence *about a developer*, and counting it as such
    would inflate exactly the cohorts the programme is trying to sample.
    """
    counts: dict[str, int] = {}
    for row in rows:
        for dimension in dimensions:
            value = row.tags.get(dimension)
            if not value:
                continue
            key = f"{dimension}={value}"
            counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["KnowledgeEvidence"]
