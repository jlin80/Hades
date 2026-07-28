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
