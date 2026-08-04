"""The write side — the only way a fact enters permanent memory.

Everything that gets stored passes through :class:`KnowledgeRecorder`. Keeping
one door means the invariants have one place to live: validation, provenance
metrics, the ``KnowledgeRecorded`` announcement, and the rule that a malformed
record is *rejected loudly* rather than dropped quietly.

That last rule is a direct lesson from this platform's own history. Twenty-nine
API handlers once did ``except Exception: return empty``, and during a disk
incident every dashboard panel showed zeros while the database refused
connections — nothing distinguished an idle platform from a blind one. An
ingestion boundary is exactly where that mistake is cheapest to make and most
expensive to diagnose, so here a rejection is an event and a metric, never a
silence.
"""

from __future__ import annotations

from collections.abc import Sequence

from hades.contexts.knowledge.application.metrics import KnowledgeMetrics
from hades.contexts.knowledge.domain.events import KnowledgeRecorded, KnowledgeRejected
from hades.contexts.knowledge.domain.models import (
    KnowledgeEnvelope,
    Observation,
)
from hades.contexts.knowledge.domain.ports import KnowledgeStore
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("knowledge.recorder")


class KnowledgeRecorder:
    """Validates, stores and announces facts. Takes no decision."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        event_bus: EventBus | None = None,
        metrics: KnowledgeMetrics | None = None,
        announce: bool = False,
    ) -> None:
        self._store = store
        self._bus = event_bus
        self._metrics = metrics
        # Announcing every single observation would put one bus message on the
        # wire per scanned token per stage — several thousand an hour, consumed
        # by nobody. Off by default; the runtime turns it on only if something
        # actually subscribes.
        self._announce = announce

    async def record(self, envelopes: Sequence[KnowledgeEnvelope]) -> list[Observation]:
        """Store a batch, returning what was accepted.

        Partial acceptance is deliberate: one malformed record from a chatty
        producer must not cost the whole batch. Each rejection is reported
        individually so the culprit is identifiable.
        """
        accepted: list[Observation] = []
        for envelope in envelopes:
            try:
                accepted.append(Observation.from_envelope(envelope))
            except (ValueError, TypeError) as exc:
                await self._reject(envelope, str(exc))
        if not accepted:
            return []

        try:
            await self._store.append(accepted)
        except Exception as exc:
            # A store that is down is a platform fault, not a data fault. It is
            # logged and counted here and re-raised: the caller (the runtime's
            # event handler) decides whether to drop or retry, and it is the only
            # layer that knows which.
            if self._metrics is not None:
                self._metrics.store_errors.inc()
            _logger.warning("knowledge_store_failed", count=len(accepted), error=str(exc))
            raise

        if self._metrics is not None:
            for observation in accepted:
                self._metrics.recorded.labels(
                    source=observation.source.value, kind=observation.kind.value
                ).inc()

        if self._announce and self._bus is not None:
            for observation in accepted:
                await self._bus.publish(
                    KnowledgeRecorded(aggregate_id=new_id(), observation=observation)
                )
        return accepted

    async def _reject(self, envelope: KnowledgeEnvelope, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.rejected.labels(source=envelope.source.value).inc()
        _logger.warning(
            "knowledge_rejected",
            source=envelope.source.value,
            kind=envelope.kind.value,
            subject=envelope.subject,
            reason=reason,
        )
        if self._bus is not None:
            await self._bus.publish(
                KnowledgeRejected(
                    aggregate_id=new_id(),
                    source=envelope.source,
                    kind=envelope.kind,
                    subject=envelope.subject,
                    reason=reason,
                )
            )


__all__ = ["KnowledgeRecorder"]
