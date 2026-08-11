"""Risk Manager endpoints - the guardian's read API + human-gated controls.

Read-only for status and the decision audit; the control actions (reset the Kill
Switch, close/trip the Circuit Breaker, enter/leave Emergency Mode) are explicit,
human-gated operations. Because the API process does not own the running Risk
Manager (the Worker does), a control action is published as a
``RiskControlCommandIssued`` event the Worker acts on - and audits like anything
else. No endpoint here executes a trade.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.api.security import Principal, get_principal
from hades.bootstrap import Container
from hades.contexts.risk.domain.events import (
    ACTION_ENTER_EMERGENCY,
    ACTION_EXIT_EMERGENCY,
    ACTION_RESET_CIRCUIT_BREAKER,
    ACTION_RESET_KILL_SWITCH,
    ACTION_TRIP_CIRCUIT_BREAKER,
    RiskControlCommandIssued,
)
from hades.ops.risk_runtime import RISK_STATUS_NAMESPACE, STATUS_KEY
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.errors import PermissionDeniedError
from hades.shared_kernel.logging import describe, get_logger
from hades.shared_kernel.persistence.models.risk import RiskDecisionRecord

_logger = get_logger("api.risk")

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])


@router.get("/status", summary="Live Risk Manager status")
async def risk_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container)
    counts = await _counts(container)
    return {
        "enabled": container.settings.risk.enabled,
        "running": live is not None,
        **counts,
        "live": live or {},
    }


@router.get("/kill-switch", summary="Kill Switch state")
async def kill_switch(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {
        "level": live.get("kill_switch_level", 0),
        "label": live.get("kill_switch_label", "none"),
        "reason": live.get("kill_switch_reason", ""),
        "consecutive_losses": live.get("consecutive_losses", 0),
        "levels": {
            "reduce_after": container.settings.risk.consecutive_losses_reduce,
            "halt_after": container.settings.risk.consecutive_losses_halt,
            "observation_dd_pct": container.settings.risk.drawdown_observation_pct,
            "paper_only_dd_pct": container.settings.risk.drawdown_paper_only_pct,
            "emergency_dd_pct": container.settings.risk.drawdown_emergency_pct,
        },
    }


@router.get("/circuit-breaker", summary="Circuit Breaker state")
async def circuit_breaker(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    return {
        "open": live.get("circuit_breaker_open", False),
        "reasons": live.get("circuit_breaker_reasons", []),
    }


@router.get("/drawdown", summary="Drawdown state vs limits")
async def drawdown(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    r = container.settings.risk
    return {
        "drawdown_pct": live.get("drawdown_pct", 0.0),
        "daily_loss_pct": live.get("daily_loss_pct", 0.0),
        "limits": {
            "daily_pct": r.daily_max_loss_pct,
            "weekly_pct": r.weekly_max_loss_pct,
            "monthly_pct": r.monthly_max_loss_pct,
            "daily_max_loss_usd": r.daily_max_loss_usd,
            "daily_max_stop_losses": r.daily_max_stop_losses,
        },
    }


@router.get("/exposure", summary="Portfolio exposure summary vs caps")
async def exposure(container: Container = Depends(get_container)) -> dict[str, Any]:
    live = await _live(container) or {}
    r = container.settings.risk
    return {
        "portfolio_exposure_pct": live.get("portfolio_exposure_pct", 0.0),
        "open_positions": live.get("open_positions", 0),
        "caps": {
            "max_portfolio_pct": r.max_portfolio_exposure_pct,
            "per_token_pct": r.max_exposure_per_token_pct,
            "per_developer_pct": r.max_exposure_per_developer_pct,
            "per_cluster_pct": r.max_exposure_per_cluster_pct,
            "per_strategy_pct": r.max_exposure_per_strategy_pct,
            "per_narrative_pct": r.max_exposure_per_narrative_pct,
            "per_regime_pct": r.max_exposure_per_regime_pct,
            "max_concurrent_positions": r.max_concurrent_positions,
            "max_correlated_positions": r.max_correlated_positions,
        },
    }


@router.get("/decisions", summary="Recent risk decisions (approvals + rejections)")
async def decisions(
    limit: int = Query(50, ge=1, le=200),
    decision: str | None = Query(None, description="filter: 'approve' or 'reject'"),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    stmt = select(RiskDecisionRecord)
    if decision in ("approve", "reject"):
        stmt = stmt.where(RiskDecisionRecord.decision == decision)
    stmt = stmt.order_by(desc(RiskDecisionRecord.at)).limit(limit)
    rows = await _rows(container, stmt)
    return {"decisions": [_decision_summary(r) for r in rows]}


@router.get("/decisions/{mint}", summary="Latest full decision for a token")
async def decision_detail(
    mint: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    row = await _one(
        container,
        select(RiskDecisionRecord)
        .where(RiskDecisionRecord.mint == mint)
        .order_by(desc(RiskDecisionRecord.at))
        .limit(1),
    )
    return row.assessment if row and row.assessment else {}


# -- human-gated control actions ------------------------------------------------
#
# "human-gated" was a claim in a summary string, not a property of the code:
# none of these five depended on ``get_principal``, so the whole defence layer --
# kill switch, circuit breaker, emergency mode -- could be driven by anyone who
# could reach the port. On this deployment that is the LAN: API_BIND is the host's
# 192.168.100.43 and the api container publishes 8000 on it.
#
# Worse, turning API_AUTH_ENABLED on would not have closed it. A router that never
# asks for a principal is unaffected by the switch that decides what a principal
# must prove, so the flag would have bought the appearance of protection and none
# of it. That is the failure mode this log keeps recording (`dedup_key` in 6s,
# WATCHDOG_UNHEALTHY_AFTER_MISSED_BEATS before 6k): a name promising a behaviour
# the code never had.
#
# The split below is deliberate. Actions that *raise* a protection stay available
# to the implicit principal, because a halt is conservative and something that can
# only stop trading is not worth gating behind a credential an operator may not
# have to hand in an emergency. Actions that *lift* one require an authenticated
# operator, on the same reasoning as the switch to LIVE: they re-expose capital,
# and the implicit `system` principal must never be able to do that by itself.


@router.post("/kill-switch/reset", summary="Reset the Kill Switch (human-gated)")
async def reset_kill_switch(
    container: Container = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _require_operator(principal, "resetting the kill switch")
    await _command(container, ACTION_RESET_KILL_SWITCH, f"operator reset by {principal.identity}")
    return {"ok": True, "action": ACTION_RESET_KILL_SWITCH}


@router.post("/circuit-breaker/reset", summary="Close the Circuit Breaker (human-gated)")
async def reset_circuit_breaker(
    container: Container = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _require_operator(principal, "closing the circuit breaker")
    await _command(
        container, ACTION_RESET_CIRCUIT_BREAKER, f"operator reset by {principal.identity}"
    )
    return {"ok": True, "action": ACTION_RESET_CIRCUIT_BREAKER}


@router.post("/circuit-breaker/trip", summary="Trip the Circuit Breaker (human-gated)")
async def trip_circuit_breaker(
    reason: str = Query("manual"),
    container: Container = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    await _command(container, ACTION_TRIP_CIRCUIT_BREAKER, reason)
    return {"ok": True, "action": ACTION_TRIP_CIRCUIT_BREAKER}


@router.post("/emergency/enter", summary="Enter Emergency Mode (human-gated)")
async def enter_emergency(
    reason: str = Query("manual"),
    container: Container = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    await _command(container, ACTION_ENTER_EMERGENCY, reason)
    return {"ok": True, "action": ACTION_ENTER_EMERGENCY}


@router.post("/emergency/exit", summary="Leave Emergency Mode (human-gated)")
async def exit_emergency(
    container: Container = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _require_operator(principal, "leaving emergency mode")
    await _command(container, ACTION_EXIT_EMERGENCY, principal.identity)
    return {"ok": True, "action": ACTION_EXIT_EMERGENCY}


def _require_operator(principal: Principal, action: str) -> None:
    """Refuse an action that lifts a protection unless a real operator asked.

    Independent of ``API_AUTH_ENABLED``, exactly as the switch to LIVE is: the
    implicit `system` principal exists so read paths keep working with auth off,
    and it must never be sufficient to re-expose capital.
    """
    if not principal.authenticated:
        raise PermissionDeniedError(
            f"{action} requires an authenticated operator "
            "(set API_AUTH_ENABLED and present a valid X-API-Key)"
        )


# -- helpers --------------------------------------------------------------------


async def _live(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=RISK_STATUS_NAMESPACE)
    try:
        result = await cache.get(STATUS_KEY)
    except Exception as exc:  # dashboard must render even if Redis is down
        _logger.warning("api_query_failed", endpoint="_live", error=describe(exc))
        return None
    return result if isinstance(result, dict) else None


async def _command(container: Container, action: str, reason: str) -> None:
    await container.event_bus.publish(
        RiskControlCommandIssued(aggregate_id=new_id(), action=action, reason=reason)
    )


async def _counts(container: Container) -> dict[str, Any]:
    empty = {"approvals_total": 0, "rejections_total": 0}
    if container.database is None:
        return empty
    try:
        async with container.database.session() as session:
            approvals = await session.scalar(
                select(func.count())
                .select_from(RiskDecisionRecord)
                .where(RiskDecisionRecord.decision == "approve")
            )
            rejections = await session.scalar(
                select(func.count())
                .select_from(RiskDecisionRecord)
                .where(RiskDecisionRecord.decision == "reject")
            )
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="_counts", error=describe(exc))
        return empty
    return {"approvals_total": int(approvals or 0), "rejections_total": int(rejections or 0)}


async def _rows(container: Container, stmt: Any) -> list[Any]:
    if container.database is None:
        return []
    try:
        async with container.database.session() as session:
            return list((await session.scalars(stmt)).all())
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="_rows", error=describe(exc))
        return []


async def _one(container: Container, stmt: Any) -> Any:
    if container.database is None:
        return None
    try:
        async with container.database.session() as session:
            return await session.scalar(stmt)
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="_one", error=describe(exc))
        return None


def _decision_summary(record: RiskDecisionRecord) -> dict[str, Any]:
    return {
        "mint": record.mint,
        "symbol": record.symbol,
        "at": record.at.isoformat() if record.at else None,
        "decision": record.decision,
        "reject_reason": record.reject_reason,
        "conviction": float(record.conviction) if record.conviction is not None else None,
        "risk_amount_usd": float(record.risk_amount_usd)
        if record.risk_amount_usd is not None
        else None,
        "notional_usd": float(record.notional_usd) if record.notional_usd is not None else None,
        "strategy": record.strategy,
        "kill_switch_level": record.kill_switch_level,
        "headline": record.headline,
    }
