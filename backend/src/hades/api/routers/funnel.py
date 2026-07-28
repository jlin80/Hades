"""Pipeline funnel — where tokens stop becoming trades.

Every stage of Hades reports itself healthy while doing nothing, because "no
trades" is indistinguishable from "nothing qualified" from inside any single
component. An operator watching a scanner chew through thousands of tokens and
a portfolio that never moves has no way to tell whether the platform is broken
or simply disciplined, and no endpoint answered that question: each context
exposed its own status, and the join across them lived only in an operator's
head.

This counts *distinct mints* surviving each hand-off over a window —
discovered -> features -> security -> committee -> risk -> filled order ->
position — plus the reasons the Risk Manager gave for the rejections. The
cliff between two adjacent numbers is the answer, and the reject-reason
breakdown turns "risk rejected everything" into which policy did it.

Read-only and window-scoped, so it stays cheap enough for the dashboard to poll.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.shared_kernel.persistence.models.learning import CommitteePredictionRecord
from hades.shared_kernel.persistence.models.market import Feature
from hades.shared_kernel.persistence.models.risk import RiskDecisionRecord
from hades.shared_kernel.persistence.models.security import SecurityAssessmentRecord
from hades.shared_kernel.persistence.models.tokens import Token
from hades.shared_kernel.persistence.models.trades import Position, Trade

router = APIRouter(prefix="/api/v1/funnel", tags=["funnel"])


@router.get("", summary="Pipeline funnel — where tokens stop becoming trades")
async def funnel(
    hours: int = Query(24, ge=1, le=720, description="Window in hours."),
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    """Distinct mints reaching each stage in the last ``hours``.

    Stages are counted independently rather than by joining a token through the
    whole chain: a mint discovered before the window can still be traded inside
    it, and a strict join would hide that. The numbers are therefore a profile
    of activity per stage, not a single cohort — which is what "is anything
    getting through right now" actually asks.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    empty: dict[str, Any] = {
        "window_hours": hours,
        "stages": [],
        "reject_reasons": {},
        "diagnosis": "No database configured — the funnel cannot be computed.",
    }
    if container.database is None:
        return empty

    try:
        async with container.database.session() as session:
            discovered = await session.scalar(
                select(func.count()).select_from(Token).where(Token.first_seen_at >= since)
            )
            featured = await session.scalar(
                select(func.count(func.distinct(Feature.token_id))).where(
                    Feature.computed_at >= since
                )
            )
            assessed = await session.scalar(
                select(func.count(func.distinct(SecurityAssessmentRecord.mint))).where(
                    SecurityAssessmentRecord.analyzed_at >= since
                )
            )
            security_ok = await session.scalar(
                select(func.count(func.distinct(SecurityAssessmentRecord.mint))).where(
                    SecurityAssessmentRecord.analyzed_at >= since,
                    SecurityAssessmentRecord.approved.is_(True),
                )
            )
            predicted = await session.scalar(
                select(func.count(func.distinct(CommitteePredictionRecord.mint))).where(
                    CommitteePredictionRecord.at >= since,
                    CommitteePredictionRecord.shadow.is_(False),
                )
            )
            risk_seen = await session.scalar(
                select(func.count(func.distinct(RiskDecisionRecord.mint))).where(
                    RiskDecisionRecord.at >= since
                )
            )
            risk_ok = await session.scalar(
                select(func.count(func.distinct(RiskDecisionRecord.mint))).where(
                    RiskDecisionRecord.at >= since,
                    RiskDecisionRecord.decision == "approve",
                )
            )
            filled = await session.scalar(
                select(func.count())
                .select_from(Trade)
                .where(Trade.filled_at >= since, Trade.status == "filled")
            )
            opened = await session.scalar(
                select(func.count()).select_from(Position).where(Position.opened_at >= since)
            )
            open_now = await session.scalar(
                select(func.count()).select_from(Position).where(Position.status == "open")
            )
            reasons = (
                await session.execute(
                    select(RiskDecisionRecord.reject_reason, func.count())
                    .where(
                        RiskDecisionRecord.at >= since,
                        RiskDecisionRecord.decision != "approve",
                        RiskDecisionRecord.reject_reason.is_not(None),
                    )
                    .group_by(RiskDecisionRecord.reject_reason)
                    .order_by(desc(func.count()))
                )
            ).all()
    except Exception as exc:  # the dashboard must render even on a DB hiccup
        return {**empty, "diagnosis": f"Funnel query failed: {exc}"}

    stages = [
        _stage("discovered", "Tokens discovered by the Scanner", discovered),
        _stage("features", "Reached the Feature Engine", featured),
        _stage("security_assessed", "Assessed by Security", assessed),
        _stage("security_approved", "Passed Security", security_ok),
        _stage("committee_predicted", "Scored by the AI Committee", predicted),
        _stage("risk_evaluated", "Reached the Risk Manager", risk_seen),
        _stage("risk_approved", "Approved by Risk", risk_ok),
        _stage("orders_filled", "Orders filled by Execution", filled),
        _stage("positions_opened", "Positions opened", opened),
    ]
    reject_reasons = {str(r): int(c) for r, c in reasons}
    return {
        "window_hours": hours,
        "stages": stages,
        "reject_reasons": reject_reasons,
        "open_positions_now": int(open_now or 0),
        "diagnosis": _diagnose(stages, reject_reasons, int(open_now or 0)),
    }


def _stage(key: str, label: str, count: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "count": int(count or 0)}


def _diagnose(stages: list[dict[str, Any]], reject_reasons: dict[str, int], open_now: int) -> str:
    """Name the first stage that lost everything — the actionable sentence.

    The funnel's numbers already contain the answer, but reading a cliff off nine
    counters is exactly the work an operator shouldn't have to redo at 3am.
    """
    counts = {s["key"]: s["count"] for s in stages}
    if counts["discovered"] == 0:
        return (
            "Nothing was discovered in this window — the Scanner's sources are the place to look."
        )
    if counts["positions_opened"] > 0:
        return (
            f"The pipeline is trading: {counts['positions_opened']} position(s) "
            f"opened, {open_now} open now."
        )

    previous = stages[0]
    for stage in stages[1:]:
        if stage["count"] == 0 and previous["count"] > 0:
            where = f"Nothing survived '{previous['label']}' -> '{stage['label']}'."
            if stage["key"] in ("risk_approved", "orders_filled") and reject_reasons:
                top = max(reject_reasons.items(), key=lambda kv: kv[1])
                return f"{where} Top rejection reason: {top[0]} ({top[1]})."
            return where
        previous = stage
    return (
        "Every stage is passing tokens through but no position opened — "
        "check Execution and the Position Monitor."
    )
