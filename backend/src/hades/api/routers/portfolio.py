"""Portfolio endpoints - the book's read API for the dashboard.

Read-only. Serves the live portfolio status (from the Redis snapshot the Worker
publishes) and the durable time series from PostgreSQL: the equity curve, the
snapshot history and the PnL log. Everything here is observation - the Portfolio
Manager never decides or executes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.ops.risk_runtime import PORTFOLIO_STATUS_NAMESPACE, STATUS_KEY
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.logging import describe, get_logger
from hades.shared_kernel.persistence.models.portfolio import (
    EquityCurve,
    PnLHistory,
    PortfolioHistory,
)

_logger = get_logger("api.portfolio")

router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])


@router.get("", summary="Live portfolio status")
async def portfolio(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container)
    return {"running": live is not None, "live": live or {}}


@router.get("/positions", summary="Open positions — which tokens hold the invested capital")
async def positions(container: Container = Depends(get_container)) -> dict[str, Any]:
    """The open book, one row per position.

    ``invested_usd`` says how much capital left cash; this says where it went.
    Without it the dashboard could report "1 open position" and an operator had
    no way to learn which token held it — the single most common question asked
    of a running bot, and the one the API could not answer.

    Sourced from the Worker's Redis snapshot, so it reflects the live in-memory
    book rather than a replayed event log.
    """
    live = await _live(container) or {}
    rows = live.get("positions")
    return {
        "positions": rows if isinstance(rows, list) else [],
        "open_positions": live.get("open_positions", 0),
        "invested_usd": live.get("invested_usd", 0.0),
        "updated_at": live.get("updated_at"),
    }


@router.get("/analytics", summary="Portfolio performance metrics")
async def analytics(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {"metrics": live.get("metrics", {}), "updated_at": live.get("updated_at")}


@router.get("/capital", summary="Capital allocation (available vs invested + reserve)")
async def capital(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {
        "equity_usd": live.get("equity_usd", 0.0),
        "cash_usd": live.get("cash_usd", 0.0),
        "invested_usd": live.get("invested_usd", 0.0),
        "available_usd": live.get("available_usd", 0.0),
        "reserve_pct": live.get("reserve_pct", container.settings.risk.liquidity_reserve_pct),
        "exposure_pct": live.get("exposure_pct", 0.0),
    }


@router.get("/equity-curve", summary="Equity curve samples")
async def equity_curve(
    limit: int = Query(500, ge=1, le=5000),
    mode: str | None = Query(None),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    stmt = select(EquityCurve)
    if mode:
        stmt = stmt.where(EquityCurve.mode == mode)
    stmt = stmt.order_by(desc(EquityCurve.timestamp)).limit(limit)
    rows = await _rows(container, stmt)
    points = [
        {"t": r.timestamp.isoformat() if r.timestamp else None, "equity_usd": float(r.equity_usd)}
        for r in reversed(rows)
    ]
    return {"points": points}


@router.get("/history", summary="Portfolio snapshot history")
async def history(
    limit: int = Query(200, ge=1, le=1000),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    rows = await _rows(
        container,
        select(PortfolioHistory).order_by(desc(PortfolioHistory.snapshot_at)).limit(limit),
    )
    return {
        "history": [
            {
                "at": r.snapshot_at.isoformat() if r.snapshot_at else None,
                "mode": r.mode,
                "equity_usd": float(r.total_equity_usd) if r.total_equity_usd is not None else None,
                "cash_usd": float(r.cash_usd) if r.cash_usd is not None else None,
                "invested_usd": float(r.positions_value_usd)
                if r.positions_value_usd is not None
                else None,
                "open_positions": r.open_positions,
                "exposure_pct": float(r.exposure_pct) if r.exposure_pct is not None else None,
                "details": r.details,
            }
            for r in rows
        ]
    }


@router.get("/pnl", summary="Realised / unrealised PnL log")
async def pnl(
    limit: int = Query(200, ge=1, le=1000),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    rows = await _rows(
        container, select(PnLHistory).order_by(desc(PnLHistory.timestamp)).limit(limit)
    )
    return {
        "pnl": [
            {
                "at": r.timestamp.isoformat() if r.timestamp else None,
                "kind": r.kind,
                "mode": r.mode,
                "amount_usd": float(r.amount_usd),
            }
            for r in rows
        ]
    }


# -- helpers --------------------------------------------------------------------


async def _live(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=PORTFOLIO_STATUS_NAMESPACE)
    try:
        result = await cache.get(STATUS_KEY)
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="_live", error=describe(exc))
        return None
    return result if isinstance(result, dict) else None


async def _rows(container: Container, stmt: Any) -> list[Any]:
    if container.database is None:
        return []
    try:
        async with container.database.session() as session:
            return list((await session.scalars(stmt)).all())
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="_rows", error=describe(exc))
        return []
