"""Shared helpers for exercising the Candidate Enricher in tests.

Every committee test now has to enrich before it can evaluate — that is the
point of the phase — so the helper here builds the *real* enricher over an
in-memory memory. Tests that care about nothing but the committee get an empty
memory, which is exactly the assertion that matters most: with nothing recorded,
enrichment must leave every existing behaviour untouched.
"""

from __future__ import annotations

from hades.contexts.learning.application.enricher import (
    EnrichmentPolicy,
    KnowledgeCandidateEnricher,
)
from hades.contexts.learning.application.feature_catalog import FeatureCatalog, FeatureNormalizer
from hades.contexts.learning.domain.models import (
    DecisionContext,
    EnrichedCandidate,
    HistoricalLesson,
    HistoricalRecord,
)


class FakeHistory:
    """An in-memory :class:`CandidateHistoryPort`. Empty unless told otherwise."""

    def __init__(
        self,
        lessons: tuple[HistoricalLesson, ...] = (),
        observations: dict[str, tuple[HistoricalRecord, ...]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self._lessons = lessons
        self._observations = observations or {}
        self._fail = fail
        self.lesson_calls = 0

    async def lessons(self, *, limit: int = 5_000) -> tuple[HistoricalLesson, ...]:
        if self._fail:
            raise RuntimeError("memory unavailable")
        self.lesson_calls += 1
        return self._lessons[:limit]

    async def observations(self, subject: str, *, limit: int = 50) -> tuple[HistoricalRecord, ...]:
        if self._fail:
            raise RuntimeError("memory unavailable")
        return self._observations.get(subject, ())[:limit]


def enricher(
    history: FakeHistory | None = None, policy: EnrichmentPolicy | None = None
) -> KnowledgeCandidateEnricher:
    return KnowledgeCandidateEnricher(
        history or FakeHistory(), FeatureNormalizer(FeatureCatalog()), policy
    )


async def enrich(context: DecisionContext, history: FakeHistory | None = None) -> EnrichedCandidate:
    """Run a candidate through the real enricher over (by default) an empty memory."""
    return await enricher(history).enrich(context)
