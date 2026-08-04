"""Wallet Intelligence endpoints — the knowledge base for the dashboard.

Read-only. Exposes live status (from the transient Redis snapshot the Worker
publishes) and the durable knowledge from PostgreSQL: a wallet's full profile
(scores, reputation, behaviour, funding, cluster and the human explanation), its
timeline and relationships, the recent clusters, and the current smart-money
leaderboard. It reports knowledge only — never a trade decision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.ops.intelligence_runtime import (
    INTELLIGENCE_STATUS_KEY,
    INTELLIGENCE_STATUS_NAMESPACE,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.logging import describe, get_logger
from hades.shared_kernel.persistence.models import (
    IntelClusterRecord,
    WalletProfileRecord,
    WalletRelationshipRecord,
    WalletTimelineRecord,
)

_logger = get_logger("api.intelligence")

router = APIRouter(prefix="/api/v1/intelligence", tags=["intelligence"])


@router.get("/status", summary="Live Wallet Intelligence status")
async def intelligence_status(
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    live = await _live_status(container)
    counts = await _counts(container)
    return {
        "enabled": container.settings.intelligence.enabled,
        "running": live is not None,
        **counts,
        "live": live or {},
    }


@router.get("/wallet/{address}", summary="Full intelligence profile for a wallet")
async def wallet_profile(
    address: str, container: Container = Depends(get_container)
) -> dict[str, Any]:
    row = await _one(
        container,
        select(WalletProfileRecord).where(WalletProfileRecord.address == address),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no profile for wallet")
    return _profile_view(row)


@router.get("/wallet/{address}/timeline", summary="A wallet's event timeline")
async def wallet_timeline(
    address: str, limit: int = 100, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"address": address, "timeline": []}
    try:
        async with container.database.session() as session:
            rows = (
                await session.scalars(
                    select(WalletTimelineRecord)
                    .where(WalletTimelineRecord.address == address)
                    .order_by(desc(WalletTimelineRecord.at))
                    .limit(min(limit, 500))
                )
            ).all()
    except Exception as exc:  # never fail the dashboard on a DB hiccup
        _logger.warning("api_query_failed", endpoint="wallet_timeline", error=describe(exc))
        return {"address": address, "timeline": []}
    return {
        "address": address,
        "timeline": [
            {
                "at": r.at.isoformat() if r.at else None,
                "kind": r.kind,
                "mint": r.mint,
                "detail": r.detail,
            }
            for r in rows
        ],
    }


@router.get("/wallet/{address}/relationships", summary="A wallet's graph neighbours")
async def wallet_relationships(
    address: str, limit: int = 100, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"address": address, "relationships": []}
    try:
        async with container.database.session() as session:
            rows = (
                await session.scalars(
                    select(WalletRelationshipRecord)
                    .where(
                        (WalletRelationshipRecord.source == address)
                        | (WalletRelationshipRecord.target == address)
                    )
                    .order_by(desc(WalletRelationshipRecord.weight))
                    .limit(min(limit, 500))
                )
            ).all()
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="wallet_relationships", error=describe(exc))
        return {"address": address, "relationships": []}
    return {
        "address": address,
        "relationships": [
            {
                "source": r.source,
                "target": r.target,
                "kind": r.kind,
                "weight": r.weight,
                "observations": r.observations,
            }
            for r in rows
        ],
    }


@router.get("/clusters", summary="Recently detected coordinated-wallet clusters")
async def clusters(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"clusters": []}
    try:
        async with container.database.session() as session:
            rows = (
                await session.scalars(
                    select(IntelClusterRecord)
                    .order_by(desc(IntelClusterRecord.detected_at))
                    .limit(min(limit, 200))
                )
            ).all()
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="clusters", error=describe(exc))
        return {"clusters": []}
    return {
        "clusters": [
            {
                "cluster_id": r.cluster_id,
                "funder": r.funder,
                "member_count": r.member_count,
                "reason": r.reason,
                "confidence": r.confidence,
                "pct_supply": r.pct_supply,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            }
            for r in rows
        ]
    }


@router.get("/smart-money", summary="Current smart-money leaderboard")
async def smart_money(
    limit: int = 50, container: Container = Depends(get_container)
) -> dict[str, Any]:
    if container.database is None:
        return {"wallets": []}
    try:
        async with container.database.session() as session:
            rows = (
                await session.scalars(
                    select(WalletProfileRecord)
                    .where(WalletProfileRecord.money_class == "smart")
                    .order_by(desc(WalletProfileRecord.score))
                    .limit(min(limit, 200))
                )
            ).all()
    except Exception as exc:
        _logger.warning("api_query_failed", endpoint="smart_money", error=describe(exc))
        return {"wallets": []}
    return {"wallets": [_profile_view(r) for r in rows]}


# -- helpers ------------------------------------------------------------------


async def _live_status(container: Container) -> dict[str, Any] | None:
    cache = CacheService(container.redis, namespace=INTELLIGENCE_STATUS_NAMESPACE)
    try:
        result = await cache.get(INTELLIGENCE_STATUS_KEY)
    except Exception as exc:  # dashboard must render even if Redis is down
        _logger.warning("api_query_failed", endpoint="_live_status", error=describe(exc))
        return None
    return result if isinstance(result, dict) else None


async def _counts(container: Container) -> dict[str, Any]:
    empty = {"wallets_total": 0, "smart_money_total": 0, "clusters_total": 0}
    if container.database is None:
        return empty
    try:
        async with container.database.session() as session:
            wallets = await session.scalar(select(func.count()).select_from(WalletProfileRecord))
            smart = await session.scalar(
                select(func.count())
                .select_from(WalletProfileRecord)
                .where(WalletProfileRecord.money_class == "smart")
            )
            clusters_total = await session.scalar(
                select(func.count()).select_from(IntelClusterRecord)
            )
    except Exception as exc:  # never fail the endpoint on a DB hiccup
        _logger.warning("api_query_failed", endpoint="_counts", error=describe(exc))
        return empty
    return {
        "wallets_total": int(wallets or 0),
        "smart_money_total": int(smart or 0),
        "clusters_total": int(clusters_total or 0),
    }


async def _one(container: Container, stmt: Any) -> Any:
    if container.database is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    try:
        async with container.database.session() as session:
            return await session.scalar(stmt)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc


def _profile_view(record: WalletProfileRecord) -> dict[str, Any]:
    return {
        "address": record.address,
        "first_seen_at": record.first_seen_at.isoformat() if record.first_seen_at else None,
        "last_active_at": record.last_active_at.isoformat() if record.last_active_at else None,
        "observations": record.observations,
        "tokens_traded": record.tokens_traded,
        "launches": record.launches,
        "rugs_participated": record.rugs_participated,
        "successes": record.successes,
        "dexes": record.dexes,
        "roles": record.roles,
        "avg_pct_supply": record.avg_pct_supply,
        "reputation": record.reputation,
        "behavior": record.behavior,
        "behavior_confidence": record.behavior_confidence,
        "money_class": record.money_class,
        "smart_money_score": record.smart_money_score,
        "influence": record.influence,
        "funding": record.funding,
        "cluster_id": record.cluster_id,
        "wallet_score": record.score,
        "band": record.band,
        "explanation": record.explanation,
        "version": record.version,
    }
