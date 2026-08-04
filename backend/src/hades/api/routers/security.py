"""Security endpoint — the guardrail's verdicts for the dashboard.

Read-only. Exposes live analysis status (from the transient Redis snapshot the
Worker publishes), cumulative counts and averages (from PostgreSQL), and the full
explainable assessment for any token: security score, sub-scores, the risk flags
and positive signals behind it, detected clusters, authorities, liquidity, top
holders and the human-readable explanation. It reports risk only — never a trade
decision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.ops.security_runtime import SECURITY_STATUS_KEY, SECURITY_STATUS_NAMESPACE
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.logging import describe, get_logger
from hades.shared_kernel.persistence.models import (
    BlacklistEntry,
    SecurityAssessmentRecord,
    WhitelistEntry,
)

_logger = get_logger("api.security")

router = APIRouter(prefix="/api/v1/security", tags=["security"])


@router.get("/status", summary="Live Security Engine status")
async def security_status(
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    live = await _live_status(container)
    counts = await _counts(container)
    return {
        "enabled": container.settings.security.enabled,
        "running": live is not None,
        "min_security_score": container.settings.security.min_security_score,
        **counts,
        "live": live or {},
    }


@router.get("/token/{mint}", summary="Full security assessment for a token")
async def security_token(
    mint: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    try:
        async with container.database.session() as session:
            record = await session.scalar(
                select(SecurityAssessmentRecord)
                .where(SecurityAssessmentRecord.mint == mint)
                .order_by(desc(SecurityAssessmentRecord.analyzed_at))
                .limit(1)
            )
    except Exception as exc:  # DB unreachable — surface as unavailable, not 500
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="no assessment for token")
    return _assessment_view(record)


@router.get("/rejections", summary="Recent rejected tokens and their reasons")
async def security_rejections(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"rejections": []}
    try:
        async with container.database.session() as session:
            rows = (
                await session.scalars(
                    select(SecurityAssessmentRecord)
                    .where(SecurityAssessmentRecord.approved.is_(False))
                    .order_by(desc(SecurityAssessmentRecord.analyzed_at))
                    .limit(min(limit, 200))
                )
            ).all()
    except Exception as exc:  # never fail the dashboard on a DB hiccup
        _logger.warning("api_query_failed", endpoint="security_rejections", error=describe(exc))
        return {"rejections": []}
    return {
        "rejections": [
            {
                "mint": r.mint,
                "score": r.score,
                "vetoed": r.vetoed,
                "reasons": (r.explanation or {}).get("negatives", []),
                "analyzed_at": r.analyzed_at.isoformat() if r.analyzed_at else None,
            }
            for r in rows
        ]
    }


@router.get("/lists", summary="Recent black/white list entries")
async def security_lists(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"blacklist": [], "whitelist": []}
    try:
        async with container.database.session() as session:
            black = (
                await session.scalars(
                    select(BlacklistEntry)
                    .order_by(desc(BlacklistEntry.created_at))
                    .limit(min(limit, 200))
                )
            ).all()
            white = (
                await session.scalars(
                    select(WhitelistEntry)
                    .order_by(desc(WhitelistEntry.created_at))
                    .limit(min(limit, 200))
                )
            ).all()
    except Exception as exc:  # never fail the dashboard on a DB hiccup
        _logger.warning("api_query_failed", endpoint="security_lists", error=describe(exc))
        return {"blacklist": [], "whitelist": []}
    return {
        "blacklist": [_list_view(b) for b in black],
        "whitelist": [_list_view(w) for w in white],
    }


# -- helpers ------------------------------------------------------------------


async def _live_status(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=SECURITY_STATUS_NAMESPACE)
    try:
        result = await cache.get(SECURITY_STATUS_KEY)
    except Exception as exc:  # dashboard must render even if Redis is down
        _logger.warning("api_query_failed", endpoint="_live_status", error=describe(exc))
        return None
    return result if isinstance(result, dict) else None


async def _counts(container: Container) -> dict[str, Any]:
    empty = {
        "assessments_total": 0,
        "approved_total": 0,
        "rejected_total": 0,
        "avg_security_score": 0.0,
    }
    if container.database is None:
        return empty
    try:
        async with container.database.session() as session:
            total = await session.scalar(select(func.count()).select_from(SecurityAssessmentRecord))
            approved = await session.scalar(
                select(func.count())
                .select_from(SecurityAssessmentRecord)
                .where(SecurityAssessmentRecord.approved.is_(True))
            )
            avg = await session.scalar(select(func.avg(SecurityAssessmentRecord.score)))
    except Exception as exc:  # never fail the endpoint on a DB hiccup
        _logger.warning("api_query_failed", endpoint="_counts", error=describe(exc))
        return empty
    total_i = int(total or 0)
    approved_i = int(approved or 0)
    return {
        "assessments_total": total_i,
        "approved_total": approved_i,
        "rejected_total": total_i - approved_i,
        "avg_security_score": round(float(avg or 0.0), 2),
    }


def _assessment_view(record: SecurityAssessmentRecord) -> dict[str, Any]:
    return {
        "mint": record.mint,
        "decision": record.decision,
        "approved": record.approved,
        "vetoed": record.vetoed,
        "security_score": record.score,
        "sub_scores": record.sub_scores,
        "flags": record.flags,
        "positives": record.positives,
        "explanation": record.explanation,
        "facts": record.facts,
        "analyzed_at": record.analyzed_at.isoformat() if record.analyzed_at else None,
        "duration_ms": record.duration_ms,
    }


def _list_view(entry: BlacklistEntry | WhitelistEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "value": entry.value,
        "reason": entry.reason,
        "source": entry.source,
        "active": entry.active,
        "added_at": entry.created_at.isoformat() if entry.created_at else None,
    }
