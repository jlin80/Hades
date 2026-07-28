"""Strategy Engine read API - strategies, signals, weights, ranking, shadow.

Read-only. The API process does not own the running Strategy Engine (the Worker
does), so these endpoints read the live status snapshot the runtime publishes to
Redis. No endpoint here produces a signal or changes a weight - the engine is
driven solely by the ``CommitteePredictionGenerated`` event flow, and strategies
never execute anything.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.ops.strategy_runtime import STATUS_KEY, STRATEGY_STATUS_NAMESPACE
from hades.shared_kernel.cache import CacheService

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


@router.get("/status", summary="Live Strategy Engine status")
async def strategy_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container)
    return {
        "enabled": container.settings.strategy.enabled,
        "running": live is not None,
        "live": live or {},
    }


@router.get("", summary="All strategies with their metadata + current state")
async def strategies(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {
        "count": live.get("count", 0),
        "active": live.get("active", 0),
        "strategies": live.get("strategies", []),
    }


@router.get("/ranking", summary="Strategies ranked by current dynamic weight")
async def ranking(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {"ranking": live.get("ranking", [])}


@router.get("/weights", summary="Current dynamic weight of each strategy")
async def weights(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    items = {
        s["name"]: s.get("weight")
        for s in live.get("strategies", [])
        if isinstance(s, dict) and "name" in s
    }
    return {"weights": items}


@router.get("/performance", summary="Self-evaluation scorecard of each strategy")
async def performance(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    rows = [
        {
            "name": s.get("name"),
            "sharpe": s.get("sharpe"),
            "profit_factor": s.get("profit_factor"),
            "win_rate": s.get("win_rate"),
            "expectancy": s.get("expectancy"),
            "trades": s.get("trades"),
            "degraded": s.get("degraded"),
        }
        for s in live.get("strategies", [])
        if isinstance(s, dict)
    ]
    return {"performance": rows}


@router.get("/shadow", summary="Strategies currently running in shadow (pre-production)")
async def shadow(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    rows = [s for s in live.get("strategies", []) if isinstance(s, dict) and s.get("shadow")]
    return {"shadow": rows}


@router.get("/signals", summary="Recent per-strategy signals")
async def signals(
    limit: int = Query(50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    live = await _live(container) or {}
    recent = live.get("recent_signals", [])
    return {"signals": recent[:limit]}


@router.get("/ensembles", summary="Recent fused ensemble signals")
async def ensembles(
    limit: int = Query(50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    live = await _live(container) or {}
    recent = live.get("recent_ensembles", [])
    return {"last_ensemble": live.get("last_ensemble", {}), "ensembles": recent[:limit]}


async def _live(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=STRATEGY_STATUS_NAMESPACE)
    try:
        result = await cache.get(STATUS_KEY)
    except Exception:  # dashboard must render even if Redis is down
        return None
    return result if isinstance(result, dict) else None
