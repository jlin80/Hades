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

Read-only and window-scoped. That was once assumed to make it cheap enough for
the dashboard to poll; it does not. Measured on CT 203, the ``features`` stage
alone took **36.2 s**, and the dashboard polls faster than the query returns, so
requests stacked — six concurrent copies, the oldest 60 s in — each holding a
connection that the Security Engine's own reads then queued behind. An endpoint
built to explain why nothing was getting through had become a reason nothing was
getting through, which is the same shape as the discovery-source incident this
platform already has a write-up for.

Two changes keep it honest. Migration ``0012`` gives the counting query an index
that serves it, and the results are computed **once per window per TTL, with one
in-flight computation at a time**: pollers that arrive during a computation wait
for it and share its answer instead of starting their own. The cache is what
bounds the damage — an index makes the query fast, but nothing except
single-flight stops a poll loop from multiplying whatever the query costs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.shared_kernel.config.settings import EventBusTransport
from hades.shared_kernel.persistence.models.learning import CommitteePredictionRecord
from hades.shared_kernel.persistence.models.market import Feature
from hades.shared_kernel.persistence.models.risk import RiskDecisionRecord
from hades.shared_kernel.persistence.models.security import SecurityAssessmentRecord
from hades.shared_kernel.persistence.models.tokens import Token
from hades.shared_kernel.persistence.models.trades import Position, Trade

router = APIRouter(prefix="/api/v1/funnel", tags=["funnel"])

FUNNEL_CACHE_TTL_SECONDS = 60.0
"""How long a computed funnel stays servable.

Sized against the cost of being wrong in each direction: a minute-old funnel
still answers "is anything getting through", while a window shorter than the
query's own duration puts the platform back to computing it continuously.
"""


class _SingleFlightCache:
    """One in-flight computation per key, and a TTL on what it produced.

    The TTL alone would not be enough. Ten pollers arriving while the cache is
    cold all miss, and ten identical 36 s queries is exactly the failure this
    exists to prevent — so the lock is the load-bearing half: whoever arrives
    during a computation waits for it and takes its result.

    Failures are deliberately not cached. A cached error would outlive the
    hiccup that caused it and report a broken pipeline for a minute after the
    pipeline recovered, on the one endpoint an operator consults to find out.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._locks: dict[int, asyncio.Lock] = {}
        self._entries: dict[int, tuple[float, dict[str, Any]]] = {}

    def _fresh(self, key: int) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        computed_at, payload = entry
        if monotonic() - computed_at > self._ttl:
            return None
        return payload

    async def get(
        self, key: int, compute: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        hit = self._fresh(key)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-checked inside the lock: a caller that queued here while the
            # winner was computing wants the winner's answer, not its own query.
            hit = self._fresh(key)
            if hit is not None:
                return hit
            payload = await compute()
            self._entries[key] = (monotonic(), payload)
            return payload

    def clear(self) -> None:
        """Drop everything cached. For tests, and for nothing else."""
        self._entries.clear()
        self._locks.clear()


_cache = _SingleFlightCache(FUNNEL_CACHE_TTL_SECONDS)


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
    empty: dict[str, Any] = {
        "window_hours": hours,
        "stages": [],
        "reject_reasons": {},
        "diagnosis": "No database configured — the funnel cannot be computed.",
    }
    if container.database is None:
        return empty

    try:
        return await _cache.get(hours, lambda: _compute(container, hours))
    except Exception as exc:  # the dashboard must render even on a DB hiccup
        return {**empty, "diagnosis": f"Funnel query failed: {exc}"}


async def _compute(container: Container, hours: int) -> dict[str, Any]:
    """The funnel's actual queries. Raises, so failures are never cached."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    assert container.database is not None  # guarded by the caller
    async with container.database.session() as session:
        discovered = await session.scalar(
            select(func.count()).select_from(Token).where(Token.first_seen_at >= since)
        )
        featured = await session.scalar(
            select(func.count(func.distinct(Feature.token_id))).where(Feature.computed_at >= since)
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
        "diagnosis": _diagnose(
            stages,
            reject_reasons,
            int(open_now or 0),
            # Only asked when the funnel is empty from the top, which is the one
            # case where a stopped consumer and a silent Scanner look identical.
            stalled_groups=(
                await _stalled_consumer_groups(container) if int(discovered or 0) == 0 else []
            ),
        ),
    }


def _stage(key: str, label: str, count: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "count": int(count or 0)}


async def _stalled_consumer_groups(container: Container) -> list[str]:
    """Consumer groups that have stopped reading the event bus, if any.

    Asked before blaming a producer, because the funnel once reported zero at all
    nine stages and pointed at the Scanner while the Scanner was publishing
    normally — its events were landing in a stream nobody was consuming. Every
    counter here is fed by a handler on the far side of that bus, so a dead
    consumer zeroes the whole funnel and looks exactly like a dead Scanner.

    Best-effort: a diagnosis that cannot be computed must never break the
    endpoint that carries it.
    """
    settings = container.settings
    if settings.event_bus.transport != EventBusTransport.REDIS:
        return []
    stream = f"{settings.event_bus.stream_prefix}:stream"
    max_idle_ms = settings.watchdog.event_bus_max_idle_seconds * 1000.0
    try:
        client = container.redis.client()
        groups = await client.xinfo_groups(stream)  # type: ignore[no-untyped-call]
        stalled: list[str] = []
        for group in groups:
            name = str(group.get("name", "?"))
            consumers = await client.xinfo_consumers(stream, name)  # type: ignore[no-untyped-call]
            if not consumers:
                stalled.append(name)
                continue
            if min(float(c.get("idle", 0)) for c in consumers) > max_idle_ms:
                stalled.append(name)
        return sorted(stalled)
    except Exception:  # diagnosis is advisory, never load-bearing
        return []


def _diagnose(
    stages: list[dict[str, Any]],
    reject_reasons: dict[str, int],
    open_now: int,
    *,
    stalled_groups: list[str] | None = None,
) -> str:
    """Name the first stage that lost everything — the actionable sentence.

    The funnel's numbers already contain the answer, but reading a cliff off nine
    counters is exactly the work an operator shouldn't have to redo at 3am.
    """
    counts = {s["key"]: s["count"] for s in stages}
    if counts["discovered"] == 0:
        # Rule out the bus before naming the Scanner. Every stage below is
        # written by a handler downstream of the event bus, so a stopped
        # consumer empties the funnel from the top and is indistinguishable
        # from a Scanner that found nothing — the platform spent four days
        # in exactly that state being told to check its sources.
        stalled = stalled_groups or []
        if stalled:
            return (
                "Nothing reached any stage, and the event bus is not being consumed "
                f"({', '.join(stalled)} stalled). The Scanner may well be publishing "
                "normally — fix the consumer before looking at the sources."
            )
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
