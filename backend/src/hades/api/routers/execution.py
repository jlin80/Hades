"""Execution Engine read API — orders, transactions, wallet and metrics.

Read-only. The API process does not own the running Execution Engine (the Worker
does), so these endpoints read the live status snapshot the runtime publishes to
Redis. No endpoint here executes or cancels a trade — execution is driven solely
by the ``TradeApproved`` event flow, and the mode is changed only through the
guarded ``/api/v1/trading/mode`` switch.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.ops.execution_runtime import EXECUTION_STATUS_NAMESPACE, STATUS_KEY
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.logging import describe, get_logger

_logger = get_logger("api.execution")

router = APIRouter(prefix="/api/v1/execution", tags=["execution"])


@router.get("/status", summary="Live Execution Engine status")
async def execution_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container)
    return {
        "enabled": container.settings.execution.enabled,
        "running": live is not None,
        "live": live or {},
    }


@router.get("/orders", summary="Recent orders")
async def orders(
    limit: int = Query(50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    live = await _live(container) or {}
    recent = live.get("recent_orders", [])
    return {"counts": live.get("orders", {}), "orders": recent[:limit]}


@router.get("/transactions", summary="Recent transactions")
async def transactions(
    limit: int = Query(50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    live = await _live(container) or {}
    recent = live.get("recent_transactions", [])
    return {"stats": live.get("transaction_stats", {}), "transactions": recent[:limit]}


@router.get("/wallet", summary="Trading wallet health (no secrets)")
async def wallet(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    info = live.get("wallet")
    return info if isinstance(info, dict) else {"configured": False}


@router.get("/metrics", summary="Execution throughput + cost metrics")
async def metrics(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {
        "mode": live.get("mode", container.settings.trading_mode.value),
        "is_live": live.get("is_live", False),
        "live_executor_available": live.get("live_executor_available", False),
        "counts": live.get("orders", {}),
        "transaction_stats": live.get("transaction_stats", {}),
    }


async def _live(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=EXECUTION_STATUS_NAMESPACE)
    try:
        result = await cache.get(STATUS_KEY)
    except Exception as exc:  # dashboard must render even if Redis is down
        _logger.warning("api_query_failed", endpoint="_live", error=describe(exc))
        return None
    return result if isinstance(result, dict) else None
