"""AI Committee endpoints — the brain's read API for the dashboard.

Read-only except for the explicit, human-gated model promotion. Exposes the live
status (from the Redis snapshot the Worker publishes) and the durable, auditable
record from PostgreSQL: the registered models and their versions, recent committee
predictions (probabilities + confidence + explanation + every opinion), feature
importance, and detected drift.

It surfaces probabilities and evidence only — never a trade decision. Promotion
activates a *model*, not a trade, and is the sanctioned human/policy-gated step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.contexts.learning.application.candidate_import import CandidateImportService
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.registry import ModelRegistryService
from hades.contexts.learning.infrastructure.model_registry import PostgresModelRegistry
from hades.ops.committee_runtime import COMMITTEE_STATUS_KEY, COMMITTEE_STATUS_NAMESPACE
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.persistence.models.learning import (
    CommitteeDriftRecord,
    CommitteeFeatureImportanceRecord,
    CommitteeModelRecord,
    CommitteePredictionRecord,
)

router = APIRouter(prefix="/api/v1/committee", tags=["committee"])


@router.get("/status", summary="Live AI Committee status")
async def committee_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live_status(container)
    counts = await _counts(container)
    return {
        "enabled": container.settings.learning.committee_enabled,
        "running": live is not None,
        **counts,
        "live": live or {},
    }


@router.get("/models", summary="Registered models (latest version per name)")
async def models(container: Container = Depends(get_container)) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(CommitteeModelRecord).order_by(
            CommitteeModelRecord.name, desc(CommitteeModelRecord.version)
        ),
    )
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.name not in latest:
            latest[r.name] = _model_view(r)
    return {"models": list(latest.values())}


@router.get("/models/{name}/versions", summary="All versions of a model")
async def model_versions(
    name: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(CommitteeModelRecord)
        .where(CommitteeModelRecord.name == name)
        .order_by(desc(CommitteeModelRecord.version)),
    )
    return {"name": name, "versions": [_model_view(r) for r in rows]}


@router.get("/models/{model_id}/feature-importance", summary="Latest feature importance")
async def feature_importance(
    model_id: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"model_id": model_id, "importance": None}
    try:
        async with container.database.session() as session:
            row = await session.scalar(
                select(CommitteeFeatureImportanceRecord)
                .where(CommitteeFeatureImportanceRecord.model_id == model_id)
                .order_by(desc(CommitteeFeatureImportanceRecord.computed_at))
                .limit(1)
            )
    except Exception:
        return {"model_id": model_id, "importance": None}
    if row is None:
        return {"model_id": model_id, "importance": None}
    return {
        "model_id": model_id,
        "importance": {
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
            "importances": row.importances,
            "useless": row.useless,
            "redundant": row.redundant,
            "stale": row.stale,
        },
    }


@router.get("/predictions", summary="Recent committee predictions")
async def predictions(
    limit: int = 50, include_shadow: bool = False, container: Container = Depends(get_container)
) -> dict[str, Any]:
    stmt = select(CommitteePredictionRecord)
    if not include_shadow:
        stmt = stmt.where(CommitteePredictionRecord.shadow.is_(False))
    stmt = stmt.order_by(desc(CommitteePredictionRecord.at)).limit(min(limit, 200))
    rows = await _rows(container, stmt)
    return {"predictions": [_prediction_summary(r) for r in rows]}


@router.get("/predictions/{mint}", summary="Latest full prediction for a token")
async def prediction_detail(
    mint: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    row = await _one(
        container,
        select(CommitteePredictionRecord)
        .where(
            CommitteePredictionRecord.mint == mint,
            CommitteePredictionRecord.shadow.is_(False),
        )
        .order_by(desc(CommitteePredictionRecord.at))
        .limit(1),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no prediction for token")
    return row.prediction or {}


@router.get("/drift", summary="Recent model-drift alerts")
async def drift(limit: int = 50, container: Container = Depends(get_container)) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(CommitteeDriftRecord)
        .order_by(desc(CommitteeDriftRecord.created_at))
        .limit(min(limit, 200)),
    )
    return {
        "drift": [
            {
                "model_id": r.model_id,
                "kind": r.kind,
                "severity": r.severity,
                "triggered": r.triggered,
                "detail": r.detail,
                "feature_drifts": r.feature_drifts,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post("/models/{model_id}/promote", summary="Promote a model (human-gated)")
async def promote_model(
    model_id: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    registry = PostgresModelRegistry(container.database)
    service = ModelRegistryService(
        registry, container.event_bus, LearningMetrics(container.metrics)
    )
    promoted = await service.promote(model_id)
    if promoted is None:
        raise HTTPException(status_code=404, detail="model not found or not promotable")
    return {
        "promoted": True,
        "model_id": promoted.model_id,
        "name": promoted.name,
        "version": promoted.version,
        "status": promoted.status.value,
    }


@router.post(
    "/candidates/import",
    summary="Import Research Lab candidate bundles from the inbox (never promotes)",
)
async def import_candidates(container: Container = Depends(get_container)) -> dict[str, Any]:
    """Sweep the configured inbox and register valid candidates as TRAINED.

    This is the Core's only ingestion path for the external Research Lab. It is a
    *pull*: the lab writes bundles to disk, an operator places them in the inbox,
    and this endpoint reads them. It registers candidates and nothing more —
    promotion stays the separate, explicit, human-gated action it already was.
    """
    if container.database is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    registry = PostgresModelRegistry(container.database)
    service = CandidateImportService(
        registry,
        ModelRegistryService(registry, container.event_bus, LearningMetrics(container.metrics)),
        container.settings.research.candidate_inbox,
    )
    report = await service.import_all()
    return {
        "inbox": container.settings.research.candidate_inbox,
        "accepted": [
            {"model_id": o.model_id, "name": o.name, "version": o.version, "path": o.path}
            for o in report.accepted
        ],
        "rejected": [{"path": o.path, "reason": o.reason} for o in report.rejected],
        "promoted": False,
    }


# -- helpers ------------------------------------------------------------------


async def _live_status(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=COMMITTEE_STATUS_NAMESPACE)
    try:
        result = await cache.get(COMMITTEE_STATUS_KEY)
    except Exception:  # dashboard must render even if Redis is down
        return None
    return result if isinstance(result, dict) else None


async def _counts(container: Container) -> dict[str, Any]:
    empty = {"models_total": 0, "promoted_total": 0, "predictions_total": 0, "outcomes_total": 0}
    if container.database is None:
        return empty
    try:
        async with container.database.session() as session:
            models_total = await session.scalar(
                select(func.count()).select_from(CommitteeModelRecord)
            )
            promoted = await session.scalar(
                select(func.count())
                .select_from(CommitteeModelRecord)
                .where(CommitteeModelRecord.status == "promoted")
            )
            preds = await session.scalar(
                select(func.count())
                .select_from(CommitteePredictionRecord)
                .where(CommitteePredictionRecord.shadow.is_(False))
            )
    except Exception:  # never fail the endpoint on a DB hiccup
        return empty
    return {
        "models_total": int(models_total or 0),
        "promoted_total": int(promoted or 0),
        "predictions_total": int(preds or 0),
        "outcomes_total": 0,
    }


async def _rows(container: Container, stmt: Any) -> list[Any]:
    if container.database is None:
        return []
    try:
        async with container.database.session() as session:
            return list((await session.scalars(stmt)).all())
    except Exception:
        return []


async def _one(container: Container, stmt: Any) -> Any:
    if container.database is None:
        return None
    try:
        async with container.database.session() as session:
            return await session.scalar(stmt)
    except Exception:
        return None


def _model_view(record: CommitteeModelRecord) -> dict[str, Any]:
    return {
        "model_id": record.model_id,
        "name": record.name,
        "version": record.version,
        "kind": record.kind,
        "status": record.status,
        "dataset_id": record.dataset_id,
        "feature_names": record.feature_names,
        "metrics": record.metrics,
        "trained_on_samples": record.trained_on_samples,
        "notes": record.notes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "promoted_at": record.promoted_at.isoformat() if record.promoted_at else None,
    }


def _prediction_summary(record: CommitteePredictionRecord) -> dict[str, Any]:
    explanation = (record.prediction or {}).get("explanation") or {}
    return {
        "mint": record.mint,
        "symbol": record.symbol,
        "at": record.at.isoformat() if record.at else None,
        "prob_roi_positive": record.prob_roi_positive,
        "prob_hit_tp": record.prob_hit_tp,
        "prob_hit_sl": record.prob_hit_sl,
        "confidence": record.confidence,
        "regime": record.regime,
        "feature_coverage": record.feature_coverage,
        "shadow": record.shadow,
        "headline": explanation.get("headline", ""),
    }
