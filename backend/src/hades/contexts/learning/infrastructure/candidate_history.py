"""The Learning context's read window onto the platform's memory.

This adapter implements :class:`CandidateHistoryPort` on top of the Knowledge
context's stores. It is the *only* place in the committee that knows the memory
exists, and it depends solely on Knowledge's **domain** layer — its ports and
value objects — never on its application or infrastructure. That is the
sanctioned shape of cross-context reading on this platform: a narrow port the
consumer declares, satisfied by an adapter at the consumer's edge.

The direction of the dependency matters as much as its narrowness. Knowledge
still imports nothing (its isolation test is an allowlist, and this change does
not touch it), so the arrow points Learning → Knowledge and no cycle exists.

Two decisions here are correctness rather than optimisation:

* **The lesson set is cached with a TTL.** Enrichment happens on the Scanner's
  hot path — potentially thousands of tokens an hour — while lessons arrive at
  the rate trades settle, which is orders of magnitude slower. Re-reading the
  whole ledger per candidate would put the committee's throughput at the mercy
  of the ledger's size, and it would do so *gradually*, appearing as an
  unexplained slowdown months later.
* **Normalisation happens here, once per refresh.** A lesson's frozen vector
  must be compared to a live candidate in the models' normalised space. Doing it
  at cache-fill time costs one pass per TTL window; doing it in the enricher
  would cost one pass over the entire history per token.

Failures degrade to "nothing known", never to an exception: the enricher's
contract is that the memory being unavailable cannot stop a token from being
judged.
"""

from __future__ import annotations

import asyncio
import time

from hades.contexts.knowledge.domain.models import KnowledgeQuery, Lesson, SubjectType
from hades.contexts.knowledge.domain.ports import KnowledgeStore, LessonStore
from hades.contexts.learning.application.feature_catalog import FeatureNormalizer
from hades.contexts.learning.domain.models import HistoricalLesson, HistoricalRecord
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.history")


class KnowledgeCandidateHistory:
    """Reads settled lessons and subject observations out of permanent memory."""

    def __init__(
        self,
        lessons: LessonStore,
        observations: KnowledgeStore,
        normalizer: FeatureNormalizer,
        *,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._lessons = lessons
        self._observations = observations
        self._normalizer = normalizer
        self._ttl = max(0.0, cache_ttl_seconds)
        self._cached: tuple[HistoricalLesson, ...] = ()
        self._cached_at = 0.0
        self._cached_limit = 0
        # Concurrent candidates would otherwise all miss the cold cache at once
        # and each start a full read of the ledger.
        self._lock = asyncio.Lock()

    async def lessons(self, *, limit: int = 5_000) -> tuple[HistoricalLesson, ...]:
        now = time.monotonic()
        if self._fresh(now, limit):
            return self._cached
        async with self._lock:
            if self._fresh(time.monotonic(), limit):
                return self._cached
            rows = await self._lessons.load(limit=limit)
            self._cached = tuple(self._to_lesson(row) for row in rows)
            self._cached_at = time.monotonic()
            self._cached_limit = limit
            return self._cached

    async def observations(self, subject: str, *, limit: int = 50) -> tuple[HistoricalRecord, ...]:
        try:
            rows = await self._observations.query(
                KnowledgeQuery(subject=subject, subject_type=SubjectType.WALLET, limit=limit)
            )
        except Exception as exc:  # a missing subject is normal; a broken read is not
            _logger.warning("history_observations_failed", subject=subject, error=str(exc))
            return ()
        return tuple(
            HistoricalRecord(
                subject=row.subject,
                source=row.source.value,
                kind=row.kind.value,
                occurred_at=row.occurred_at,
                features=dict(row.features),
                payload={
                    key: value
                    for key, value in row.payload.items()
                    if isinstance(value, (str, bool, int, float))
                },
            )
            for row in rows
        )

    # -- internals ------------------------------------------------------------

    def _fresh(self, now: float, limit: int) -> bool:
        if not self._cached_at or limit > self._cached_limit:
            return False
        return (now - self._cached_at) <= self._ttl

    def _to_lesson(self, row: Lesson) -> HistoricalLesson:
        """Translate a memory Lesson into the committee's own shape.

        This function *is* the anti-corruption layer: past it, nothing in the
        Learning context refers to a Knowledge type.
        """
        features = dict(row.features)
        return HistoricalLesson(
            subject=row.subject,
            decided_at=row.decided_at,
            features=features,
            normalized=self._normalizer.normalize_values(features),
            tags={str(k): str(v) for k, v in row.tags.items()},
            realized_roi=row.realized_roi,
            label_roi_positive=row.label_roi_positive,
            label_hit_tp=row.label_hit_tp,
            label_hit_sl=row.label_hit_sl,
        )


__all__ = ["KnowledgeCandidateHistory"]
