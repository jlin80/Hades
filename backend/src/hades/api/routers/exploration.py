"""Exploration endpoints — read what the cold-start programme is doing.

Read-only, and structurally so: there is no endpoint here that grants a trade,
raises a budget or restarts a finished programme. Those are configuration, and
configuration is the operator's deliberate act at deploy time rather than an HTTP
call — a budget that could be raised over the network would be a budget in name
only.

``/exploration/status`` is the page to read, and the fields to read first are
``active`` and ``inactive_reason``. Between them they answer the question that
matters: *is the platform still buying evidence, and if not, why not?* "Finished"
and "out of money" and "switched off" are three very different states, and a
single boolean would collapse them — which is the exact class of ambiguity that
cost this platform weeks the last time round.

Failures are reported, not swallowed: a missing snapshot is ``stale``, never
zeros. "The worker has not published in 30 seconds" and "the programme has spent
nothing" must not look alike.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.contexts.exploration.infrastructure.stores import (
    InMemoryExplorationLedger,
    PostgresExplorationLedger,
)
from hades.ops.exploration_runtime import EXPLORATION_STATUS_NAMESPACE, STATUS_KEY
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["exploration"])

_logger = get_logger("api.exploration")


@router.get("/exploration/status", summary="State of the cold-start exploration programme")
async def exploration_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    """Live status published by the worker, with an honest fallback.

    The snapshot is computed in the worker (which owns the ledger and the memory)
    and cached in Redis. It carries the evidence census the shutdown condition is
    measured against, the four budget ceilings, what has been spent against each,
    and — when the programme is not granting — the reason.
    """
    cache = CacheService(container.redis, namespace=EXPLORATION_STATUS_NAMESPACE)
    try:
        snapshot = await cache.get(STATUS_KEY)
    except Exception as exc:
        _logger.warning("exploration_status_unavailable", error=str(exc))
        return {
            "enabled": container.settings.exploration.enabled,
            "stale": True,
            "error": "status cache unavailable",
        }
    if not isinstance(snapshot, dict):
        return {
            "enabled": container.settings.exploration.enabled,
            "stale": True,
            "note": "no snapshot published yet — is the worker running?",
        }
    return {**snapshot, "stale": False}


@router.get("/exploration/grants", summary="Every exploration trade charged to the budget")
async def exploration_grants(
    container: Container = Depends(get_container),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    """The ledger itself, newest-first — the programme's complete accounting.

    Each row is one trade the Risk Manager approved under exploration rules, with
    the cohort whose thin sample justified it. That last field is what makes the
    programme auditable on its own terms: it answers whether the budget was
    actually spread across the cohorts the memory was missing, or poured into one
    developer because the Scanner happened to keep finding them.
    """
    ledger = (
        PostgresExplorationLedger(container.database)
        if container.database is not None
        else InMemoryExplorationLedger()
    )
    try:
        rows = await ledger.recent(limit=limit)
        total_usd, total_trades = await ledger.spent_total()
    except Exception as exc:
        _logger.warning("exploration_grants_failed", error=str(exc))
        return {"count": 0, "total_trades": 0, "error": str(exc), "grants": []}
    return {
        "count": len(rows),
        "total_trades": total_trades,
        "total_spent_usd": round(total_usd, 6),
        "grants": [row.model_dump(mode="json") for row in rows],
    }
