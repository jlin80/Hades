"""Research Lab endpoints — the lab's read API for the dashboard.

Read-only surface over the lab's live status (the Redis snapshot the Worker
publishes) and its durable, append-only PostgreSQL memory: experiments, backtests,
candidate strategies, shadow strategies, promotion decisions, the Knowledge Base,
hypotheses and reports.

The single write endpoint, ``/candidates/{id}/promote``, records a **human**
promotion decision. It is fail-closed (requires an explicit ``approve=true``) and,
even when approved, only writes a governance record — it never deploys a strategy,
places an order, or enables live trading. The Research Lab has no path to
execution, and this API adds none.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.ops.research_runtime import RESEARCH_STATUS_KEY, RESEARCH_STATUS_NAMESPACE
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.persistence.models.research import (
    ResearchBacktestRecord,
    ResearchCandidateRecord,
    ResearchExperimentRecord,
    ResearchHypothesisRecord,
    ResearchKnowledgeRecord,
    ResearchPromotionRecord,
    ResearchReportRecord,
    ResearchShadowRecord,
)

router = APIRouter(prefix="/api/v1/research", tags=["research"])


@router.get("/status", summary="Live Research Lab status")
async def research_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live_status(container)
    counts = await _counts(container)
    return {
        "lab_enabled": container.settings.research.lab_enabled,
        "running": live is not None,
        **counts,
        "live": live or {},
    }


@router.get("/experiments", summary="Recent experiments")
async def experiments(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchExperimentRecord)
        .order_by(desc(ResearchExperimentRecord.created_at))
        .limit(min(limit, 200)),
    )
    return {
        "experiments": [
            {
                "experiment_id": r.experiment_id,
                "name": r.name,
                "kind": r.kind,
                "status": r.status,
                "hypothesis": r.hypothesis,
                "conclusion": r.conclusion,
                "metrics": r.metrics,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/backtests", summary="Recent backtests")
async def backtests(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchBacktestRecord)
        .order_by(desc(ResearchBacktestRecord.created_at))
        .limit(min(limit, 200)),
    )
    return {
        "backtests": [
            {
                "backtest_id": r.backtest_id,
                "strategy": r.strategy,
                "total_return": r.total_return,
                "sharpe": r.sharpe,
                "max_drawdown": r.max_drawdown,
                "profit_factor": r.profit_factor,
                "trades": r.trades,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/candidates", summary="Candidate strategies")
async def candidates(container: Container = Depends(get_container)) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchCandidateRecord).order_by(desc(ResearchCandidateRecord.created_at)),
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.candidate_id in seen:
            continue
        seen.add(r.candidate_id)
        out.append(
            {
                "candidate_id": r.candidate_id,
                "name": r.name,
                "archetype": r.archetype,
                "version": r.version,
                "stage": r.stage,
                "payload": r.payload,
            }
        )
    return {"candidates": out}


@router.get("/shadows", summary="Shadow strategies (virtual, zero-capital)")
async def shadows(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live_status(container)
    if live and live.get("shadow_strategies"):
        return {"shadows": live["shadow_strategies"], "source": "live"}
    rows = await _rows(
        container,
        select(ResearchShadowRecord).order_by(desc(ResearchShadowRecord.created_at)).limit(50),
    )
    return {"shadows": [r.payload for r in rows], "source": "db"}


@router.get("/rankings", summary="Latest strategy ranking (from shadow scorecards)")
async def rankings(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live_status(container)
    shadows_ = (live or {}).get("shadow_strategies", [])
    ranked = sorted(shadows_, key=lambda s: s.get("expectancy", 0.0), reverse=True)
    return {"ranking": [s.get("name") for s in ranked], "entries": ranked}


@router.get("/promotions", summary="Promotion decisions (recommendations only)")
async def promotions(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchPromotionRecord)
        .order_by(desc(ResearchPromotionRecord.created_at))
        .limit(min(limit, 200)),
    )
    return {
        "promotions": [
            {
                "candidate_id": r.candidate_id,
                "candidate_name": r.candidate_name,
                "kind": r.kind,
                "outcome": r.outcome,
                "manual_approved": r.manual_approved,
                "rationale": r.rationale,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/knowledge", summary="Knowledge-Base entries (append-only)")
async def knowledge(
    limit: int = 100, container: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchKnowledgeRecord)
        .order_by(desc(ResearchKnowledgeRecord.created_at))
        .limit(min(limit, 500)),
    )
    return {
        "entries": [
            {
                "entry_id": r.entry_id,
                "category": r.category,
                "title": r.title,
                "body": r.body,
                "data": r.data,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/hypotheses", summary="Recorded hypotheses")
async def hypotheses(
    limit: int = 100, container: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchHypothesisRecord)
        .order_by(desc(ResearchHypothesisRecord.created_at))
        .limit(min(limit, 500)),
    )
    return {
        "hypotheses": [
            {
                "hypothesis_id": r.hypothesis_id,
                "statement": r.statement,
                "status": r.status,
                "rationale": r.rationale,
                "evidence": r.evidence,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/reports", summary="Generated research reports")
async def reports(limit: int = 50, container: Container = Depends(get_container)) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(ResearchReportRecord)
        .order_by(desc(ResearchReportRecord.created_at))
        .limit(min(limit, 200)),
    )
    return {
        "reports": [
            {
                "report_id": r.report_id,
                "period": r.period,
                "title": r.title,
                "summary": r.summary,
                "payload": r.payload,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post("/candidates/{candidate_id}/promote", summary="Record a human promotion decision")
async def promote_candidate(
    candidate_id: str,
    approve: bool = False,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """Fail-closed, human-gated. Requires ``approve=true``; even then this only
    records a governance decision — it deploys nothing and enables no live trading.
    """
    if not approve:
        raise HTTPException(
            status_code=400,
            detail="promotion requires explicit approve=true (human confirmation)",
        )
    row = await _one(
        container,
        select(ResearchCandidateRecord)
        .where(ResearchCandidateRecord.candidate_id == candidate_id)
        .order_by(desc(ResearchCandidateRecord.created_at))
        .limit(1),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    # The response used to claim "recorded" while writing nothing, so the
    # Promotion decisions panel stayed empty forever and a governance decision
    # left no trace. Persist it before saying it happened.
    record = ResearchPromotionRecord(
        candidate_id=candidate_id,
        candidate_name=row.name,
        kind="manual",
        outcome="approved",
        manual_approved=True,
        rationale="Recorded from the dashboard by a human operator.",
        payload={
            "candidate_id": candidate_id,
            "name": row.name,
            "archetype": row.archetype,
            "version": row.version,
            "stage": row.stage,
            "source": "dashboard",
        },
    )
    if container.database is None:
        raise HTTPException(
            status_code=503, detail="promotion cannot be recorded: no database configured"
        )
    try:
        async with container.database.session() as session:
            session.add(record)
    except Exception as exc:  # never claim success on a failed write
        raise HTTPException(
            status_code=500, detail=f"promotion could not be recorded: {exc}"
        ) from exc

    return {
        "candidate_id": candidate_id,
        "name": row.name,
        "recorded": True,
        "note": (
            "Human approval recorded. Deployment to production remains a separate, "
            "manual action outside the Research Lab; nothing was activated."
        ),
    }


# -- helpers ------------------------------------------------------------------


async def _live_status(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=RESEARCH_STATUS_NAMESPACE)
    try:
        result = await cache.get(RESEARCH_STATUS_KEY)
    except Exception:  # dashboard must render even if Redis is down
        return None
    return result if isinstance(result, dict) else None


async def _counts(container: Container) -> dict[str, Any]:
    empty = {
        "experiments_total": 0,
        "backtests_total": 0,
        "candidates_total": 0,
        "promotions_total": 0,
        "knowledge_total": 0,
    }
    if container.database is None:
        return empty
    try:
        async with container.database.session() as session:
            experiments_total = await session.scalar(
                select(func.count()).select_from(ResearchExperimentRecord)
            )
            backtests_total = await session.scalar(
                select(func.count()).select_from(ResearchBacktestRecord)
            )
            candidates_total = await session.scalar(
                select(func.count(func.distinct(ResearchCandidateRecord.candidate_id)))
            )
            promotions_total = await session.scalar(
                select(func.count()).select_from(ResearchPromotionRecord)
            )
            knowledge_total = await session.scalar(
                select(func.count()).select_from(ResearchKnowledgeRecord)
            )
    except Exception:  # never fail the endpoint on a DB hiccup
        return empty
    return {
        "experiments_total": int(experiments_total or 0),
        "backtests_total": int(backtests_total or 0),
        "candidates_total": int(candidates_total or 0),
        "promotions_total": int(promotions_total or 0),
        "knowledge_total": int(knowledge_total or 0),
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
