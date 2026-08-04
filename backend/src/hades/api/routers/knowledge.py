"""Knowledge endpoints — read the platform's permanent memory.

Read-only, in the strong sense: memory is written by the platform's own
ingestion path and there is no endpoint here that appends, edits or deletes. An
append-only store whose contents an HTTP caller could shape would not be usable
as evidence, which is the entire point of the context.

The endpoint that matters operationally is ``/knowledge/status``. Its
``is_trainable`` flag answers, in one boolean, the question that cost this
platform weeks: *can a model be validated against what we have?* A memory of
half a million observations with every lesson on the same side of zero is a
single-class dataset, its AUC is undefined, and no validation gate can ever pass
— however healthy every other panel looks.

Failures are reported, not swallowed. Twenty-nine handlers on this platform once
returned an empty result on any exception without logging it, and during a disk
incident every dashboard panel showed zeros while the database refused
connections, with nothing anywhere to distinguish an idle platform from a blind
one. These handlers say which it is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.contexts.knowledge.application.ingest_service import KnowledgeIngestService
from hades.contexts.knowledge.application.recorder import KnowledgeRecorder
from hades.contexts.knowledge.domain.models import (
    KnowledgeKind,
    KnowledgeQuery,
    KnowledgeSource,
    SubjectType,
    Verification,
)
from hades.contexts.knowledge.domain.ports import KnowledgeStore, LessonStore
from hades.contexts.knowledge.infrastructure.stores import (
    InMemoryKnowledgeStore,
    InMemoryLessonStore,
    PostgresKnowledgeStore,
    PostgresLessonStore,
)
from hades.ops.knowledge_runtime import KNOWLEDGE_STATUS_NAMESPACE, STATUS_KEY
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["knowledge"])

_logger = get_logger("api.knowledge")


def _store(container: Container) -> KnowledgeStore:
    if container.database is not None:
        return PostgresKnowledgeStore(container.database)
    return InMemoryKnowledgeStore()


def _lessons(container: Container) -> LessonStore:
    if container.database is not None:
        return PostgresLessonStore(container.database)
    return InMemoryLessonStore()


@router.get("/knowledge/status", summary="Census of permanent memory")
async def knowledge_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    """Live status published by the worker, with an honest fallback.

    The snapshot is computed in the worker (which owns the stores) and cached in
    Redis. If it is missing, that is reported as ``stale`` rather than dressed up
    as zeros: "the worker has not published in 30 seconds" and "the memory is
    empty" are different problems and must not look alike.
    """
    cache = CacheService(container.redis, namespace=KNOWLEDGE_STATUS_NAMESPACE)
    try:
        snapshot = await cache.get(STATUS_KEY)
    except Exception as exc:
        _logger.warning("knowledge_status_unavailable", error=str(exc))
        return {
            "enabled": container.settings.knowledge.enabled,
            "stale": True,
            "error": "status cache unavailable",
        }
    if not isinstance(snapshot, dict):
        return {
            "enabled": container.settings.knowledge.enabled,
            "stale": True,
            "note": "no snapshot published yet — is the worker running?",
        }
    return {**snapshot, "stale": False}


@router.get("/knowledge", summary="Query permanent memory")
async def knowledge(
    container: Container = Depends(get_container),
    source: KnowledgeSource | None = Query(default=None),
    kind: KnowledgeKind | None = Query(default=None),
    subject: str | None = Query(default=None),
    subject_type: SubjectType | None = Query(default=None),
    min_verification: Verification | None = Query(
        default=None, description="Only records at least this strongly verified"
    ),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    spec = KnowledgeQuery(
        source=source,
        kind=kind,
        subject=subject,
        subject_type=subject_type,
        min_verification=min_verification,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    try:
        rows = await _store(container).query(spec)
    except Exception as exc:
        _logger.warning("knowledge_query_failed", error=str(exc))
        return {"count": 0, "limit": limit, "offset": offset, "error": str(exc), "records": []}
    return {
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "records": [row.model_dump(mode="json") for row in rows],
    }


@router.post(
    "/knowledge/import",
    summary="Import knowledge bundles from the external Research Lab inbox",
)
async def import_knowledge(container: Container = Depends(get_container)) -> dict[str, Any]:
    """Sweep the inbox and record valid bundles into permanent memory.

    This is the Core's only ingestion path for the external Hades Research Lab,
    and it is a **pull**: the lab writes bundles to a directory it owns, an
    operator places them here, and this endpoint reads them. The two repositories
    share no library, no schema and no network call.

    What an external bundle **cannot** do, structurally:

    * claim `verification: realised` — the field does not exist in the format;
      the Core assigns it from the record's source, and every source an external
      producer may claim maps to `simulated`;
    * claim a platform source such as `paper_trading` or `executed_trade` — the
      allowlist rejects it, so a file cannot pose as the trading path;
    * express a *lesson*. Lessons are what the AI Committee trains on, and they
      are producible only by the platform settling a trade it actually took.

    Together those three mean the worst a hostile bundle achieves is inserting
    clearly-labelled simulated observations — never poisoning the training ledger.
    """
    recorder = KnowledgeRecorder(_store(container), event_bus=container.event_bus)
    service = KnowledgeIngestService(recorder, container.settings.knowledge.inbox)
    report = await service.import_all()
    return {
        "inbox": container.settings.knowledge.inbox,
        "records_imported": report.records_imported,
        "accepted": [
            {
                "path": outcome.path,
                "bundle_id": outcome.bundle_id,
                "produced_by": outcome.produced_by,
                "records": outcome.records,
            }
            for outcome in report.accepted
        ],
        "rejected": [
            {"path": outcome.path, "reason": outcome.reason} for outcome in report.rejected
        ],
        # Stated explicitly because it is the question a reader of this response
        # actually has: importing knowledge deploys nothing and trains nothing.
        "promoted": False,
        "verification": "simulated",
    }


@router.get("/knowledge/lessons", summary="Ground-truth decision→outcome pairs")
async def lessons(
    container: Container = Depends(get_container),
    limit: int = Query(default=100, ge=1, le=1000),
    since_iso: str | None = Query(default=None),
) -> dict[str, Any]:
    """The training samples themselves, oldest-first.

    Each carries the features as they stood when the decision was taken and the
    labels from how it actually settled — which is what makes them usable for
    training and what makes the pair auditable after the fact.
    """
    try:
        rows = await _lessons(container).load(limit=limit, since_iso=since_iso)
        total = await _lessons(container).count()
    except Exception as exc:
        _logger.warning("knowledge_lessons_failed", error=str(exc))
        return {"count": 0, "total": 0, "error": str(exc), "lessons": []}
    positives = sum(1 for row in rows if row.label_roi_positive)
    return {
        "count": len(rows),
        "total": total,
        "positive_in_page": positives,
        "lessons": [row.model_dump(mode="json") for row in rows],
    }
