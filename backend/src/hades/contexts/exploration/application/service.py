"""The Exploration service — the policy, plus the state the policy must not own.

:mod:`policy` is pure and stays that way. Everything that has to touch the world
lives here: reading the evidence census, reading the spend ledger, announcing on
the bus, and holding the one piece of durable-in-spirit state the programme has —
whether it has finished.

**Finishing is a latch, not a query.** Once sufficiency is observed the service
stops granting for the rest of the process's life and says so on the bus, rather
than re-deciding on every candidate. That is a correctness choice: lessons are
append-only, so sufficiency cannot genuinely be lost, but a transient read that
undercounted them would otherwise restart a programme the platform had already
declared complete — spending budget again with no announcement that it had. A
latch makes "exploration ended" a fact with a timestamp rather than a condition
that happens to hold.

**Reads are cached, and the bound is stated.** Evidence changes at the rate
trades settle (hours); spend changes only when a grant is approved. Both are read
behind a short TTL because ``consider()`` runs on the Scanner's hot path — every
candidate the conviction gates mute arrives here — and re-aggregating the ledger
per token would tie the pipeline's throughput to the ledger's size. The spend
cache is invalidated on every commit, so the only way to exceed a ceiling is
several grants in flight within the same TTL window; the overshoot is bounded by
the number of concurrent grants times ``per_trade_usd`` (dollars, by
construction), and the ledger — always the source of truth — corrects it on the
next read. A cache that could hide a *persistent* overspend would not be
acceptable; one that can briefly overshoot by a dollar is.

Every failure degrades to "no grant". Exploration is optional spending: not being
able to tell whether more evidence is needed is a reason to stop, never to carry
on.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from hades.contexts.exploration.application.metrics import ExplorationMetrics
from hades.contexts.exploration.application.policy import (
    ExplorationPolicy,
    day_start,
    week_start,
)
from hades.contexts.exploration.domain.events import (
    ExplorationBudgetExhausted,
    ExplorationCompleted,
    ExplorationGranted,
    ExplorationSpent,
)
from hades.contexts.exploration.domain.models import (
    EvidenceStatus,
    ExplorationCandidate,
    ExplorationConfig,
    ExplorationDecline,
    ExplorationProgress,
    ExplorationRecord,
    ExplorationSpend,
    ExplorationStatus,
    ExplorationVerdict,
)
from hades.contexts.exploration.domain.ports import EvidencePort, ExplorationLedgerStore
from hades.contexts.exploration.domain.progress import compute_progress
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("exploration.service")

#: Which budget ceilings are announced on the bus when first reached.
_EXHAUSTION_WINDOWS: dict[ExplorationDecline, str] = {
    ExplorationDecline.DAILY_BUDGET: "daily",
    ExplorationDecline.WEEKLY_BUDGET: "weekly",
    ExplorationDecline.TOTAL_BUDGET: "total",
}


class ExplorationService:
    """Owns the exploration programme: its evidence, its budget and its end."""

    def __init__(
        self,
        config: ExplorationConfig,
        evidence: EvidencePort,
        ledger: ExplorationLedgerStore,
        *,
        event_bus: EventBus | None = None,
        metrics: ExplorationMetrics | None = None,
        evidence_ttl_seconds: float = 60.0,
        spend_ttl_seconds: float = 5.0,
    ) -> None:
        self._cfg = config
        self._policy = ExplorationPolicy(config)
        self._evidence = evidence
        self._ledger = ledger
        self._bus = event_bus
        self._metrics = metrics
        self._evidence_ttl = max(0.0, evidence_ttl_seconds)
        self._spend_ttl = max(0.0, spend_ttl_seconds)

        self._completed = False
        self._granted = 0
        self._declined = 0
        self._declines: dict[str, int] = {}
        self._announced: dict[str, str] = {}

        self._cached_evidence: EvidenceStatus | None = None
        self._evidence_at = 0.0
        self._cached_spend: ExplorationSpend | None = None
        self._spend_at = 0.0
        self._lock = asyncio.Lock()

        if self._metrics is not None:
            self._metrics.active.set(1.0 if config.enabled else 0.0)

    # -- the decision ---------------------------------------------------------

    async def consider(self, candidate: ExplorationCandidate) -> ExplorationVerdict:
        """Decide whether this candidate earns an exploration trade.

        Never raises. The Risk Manager calls this in the middle of its approval
        chain, and a failure in an *optional* programme must not be able to turn
        a rejection into an error — or, worse, an approval.
        """
        now = datetime.now(UTC)
        if not self._cfg.enabled:
            return self._record(
                self._policy.decide(candidate, EvidenceStatus(), ExplorationSpend(), now=now)
            )
        if self._completed:
            # Latched. Short-circuited before any I/O: once the programme is over
            # it costs nothing per candidate for the rest of the process's life.
            evidence = self._cached_evidence or EvidenceStatus()
            return self._record(
                self._policy.decide(candidate, evidence, ExplorationSpend(), now=now)
            )

        try:
            evidence = await self._evidence_status()
            spend = await self._spend(now)
        except Exception as exc:  # an optional programme never breaks the guardian
            if self._metrics is not None:
                self._metrics.errors.inc()
            _logger.warning("exploration_read_failed", error=str(exc))
            return self._record(
                self._policy.decide(
                    candidate, EvidenceStatus(available=False), ExplorationSpend(), now=now
                )
            )

        verdict = self._policy.decide(candidate, evidence, spend, now=now)
        await self._announce(verdict, evidence, spend, candidate)
        return self._record(verdict)

    async def commit(
        self, verdict: ExplorationVerdict, *, correlation_id: str | None = None
    ) -> None:
        """Charge an approved exploration trade to the budget.

        Called by the Risk Manager **after** it approves, never when it issues
        the grant. A candidate that clears exploration and is then vetoed by an
        allocation rule costs the programme nothing, and a budget that charged on
        grant would overstate its burn and understate its remaining runway.
        """
        if not verdict.granted:
            return
        record = ExplorationRecord(
            subject=verdict.subject,
            granted_at=verdict.at or datetime.now(UTC),
            notional_usd=verdict.notional_usd,
            cohort_key=verdict.cohort_key,
            reason=verdict.headline,
            correlation_id=correlation_id,
        )
        try:
            await self._ledger.append(record)
        except Exception as exc:
            # A charge that cannot be written is the one failure worth being loud
            # about: the trade happened, so an unrecorded spend means the budget
            # over-authorises from here on.
            if self._metrics is not None:
                self._metrics.errors.inc()
            _logger.error(
                "exploration_charge_not_recorded",
                subject=verdict.subject,
                notional_usd=verdict.notional_usd,
                error=str(exc),
            )
            return
        self._cached_spend = None  # the ledger moved; the cache is now a lie
        if self._metrics is not None:
            self._metrics.spent.inc(verdict.notional_usd)
        spend = await self._spend(datetime.now(UTC))
        _logger.info(
            "exploration_trade_charged",
            subject=verdict.subject,
            notional_usd=verdict.notional_usd,
            cohort=verdict.cohort_key,
            spent_total_usd=round(spend.total_usd, 4),
        )
        if self._bus is not None:
            await self._bus.publish(
                ExplorationSpent(
                    aggregate_id=new_id(),
                    subject=verdict.subject,
                    notional_usd=verdict.notional_usd,
                    spent_day_usd=round(spend.day_usd, 6),
                    spent_week_usd=round(spend.week_usd, 6),
                    spent_total_usd=round(spend.total_usd, 6),
                    trades_total=spend.total_trades,
                    correlation_id_ref=correlation_id,
                )
            )

    # -- status ---------------------------------------------------------------

    async def status(self) -> ExplorationStatus:
        """The whole programme, for the API and the dashboard."""
        now = datetime.now(UTC)
        records: list[ExplorationRecord] = []
        try:
            evidence = await self._evidence_status()
            spend = await self._spend(now)
            records = await self._ledger.recent(limit=200)
        except Exception as exc:
            if self._metrics is not None:
                self._metrics.errors.inc()
            _logger.warning("exploration_status_failed", error=str(exc))
            evidence, spend = EvidenceStatus(available=False), ExplorationSpend()
        active = self._cfg.enabled and not self._completed and not evidence.sufficient
        progress = compute_progress(
            evidence=evidence,
            budget=self._cfg.budget,
            spend=spend,
            records=records,
            now=now,
        )
        self._report_progress(progress, active=active)
        return ExplorationStatus(
            enabled=self._cfg.enabled,
            active=active,
            evidence=evidence,
            budget=self._cfg.budget,
            spend=spend,
            inactive_reason=self._inactive_reason(evidence),
            granted_total=self._granted,
            declined_total=self._declined,
            declines_by_reason=dict(self._declines),
            progress=progress,
        )

    def _report_progress(self, progress: ExplorationProgress, *, active: bool) -> None:
        """Publish progress to metrics, and say the two things worth waking up for.

        Both warnings are gated on the programme being *active*: a finished or
        switched-off programme with a short runway is not a problem, and an alert
        that fires in the ordinary end state is an alert the operator learns to
        ignore.
        """
        if self._metrics is not None:
            self._metrics.progress_pct.set(progress.pct_complete * 100.0)
            self._metrics.trades_remaining.set(progress.trades_remaining)
            self._metrics.eta_days.set(progress.eta_days if progress.eta_days is not None else -1.0)
        if not active:
            return
        if progress.budget_exhausts_first:
            _logger.warning(
                "exploration_budget_exhausts_before_evidence",
                pct_complete=round(progress.pct_complete * 100, 2),
                lessons_needed=progress.lessons_needed,
                trades_remaining=progress.trades_remaining,
                lessons_per_trade=progress.lessons_per_trade,
                note=progress.note,
            )
        elif progress.stalled:
            _logger.warning(
                "exploration_stalled",
                lessons_per_day=progress.lessons_per_day,
                trades_remaining=progress.trades_remaining,
                note="spending exploration budget without accumulating lessons",
            )

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def config(self) -> ExplorationConfig:
        return self._cfg

    # -- internals ------------------------------------------------------------

    def _inactive_reason(self, evidence: EvidenceStatus) -> str:
        if not self._cfg.enabled:
            return "disabled by configuration"
        if self._completed or evidence.sufficient:
            return f"evidence sufficient — {evidence.summary}"
        if not evidence.available:
            return "permanent memory unreadable — exploration fails closed"
        return ""

    async def _evidence_status(self) -> EvidenceStatus:
        now = time.monotonic()
        cached = self._cached_evidence
        if cached is not None and (now - self._evidence_at) <= self._evidence_ttl:
            return cached
        async with self._lock:
            cached = self._cached_evidence
            if cached is not None and (time.monotonic() - self._evidence_at) <= self._evidence_ttl:
                return cached
            status = await self._evidence.status(cohort_dimensions=self._cfg.cohort_dimensions)
            self._cached_evidence = status
            self._evidence_at = time.monotonic()
            if self._metrics is not None and status.available:
                self._metrics.evidence_lessons.set(status.lessons)
                self._metrics.evidence_positive.set(status.positive)
                self._metrics.evidence_negative.set(status.negative)
            return status

    async def _spend(self, now: datetime) -> ExplorationSpend:
        monotonic = time.monotonic()
        cached = self._cached_spend
        if cached is not None and (monotonic - self._spend_at) <= self._spend_ttl:
            return cached
        day_usd, day_trades = await self._ledger.spent_since(day_start(now))
        week_usd, week_trades = await self._ledger.spent_since(week_start(now))
        total_usd, total_trades = await self._ledger.spent_total()
        spend = ExplorationSpend(
            day_usd=day_usd,
            day_trades=day_trades,
            week_usd=week_usd,
            week_trades=week_trades,
            total_usd=total_usd,
            total_trades=total_trades,
        )
        self._cached_spend = spend
        self._spend_at = time.monotonic()
        if self._metrics is not None:
            left = spend.remaining(self._cfg.budget)
            self._metrics.budget_day_remaining.set(left["day_usd"])
            self._metrics.budget_total_remaining.set(left["total_usd"])
        return spend

    def _record(self, verdict: ExplorationVerdict) -> ExplorationVerdict:
        if verdict.granted:
            self._granted += 1
            if self._metrics is not None:
                self._metrics.granted.inc()
        else:
            self._declined += 1
            reason = (verdict.decline or ExplorationDecline.DISABLED).value
            self._declines[reason] = self._declines.get(reason, 0) + 1
            if self._metrics is not None:
                self._metrics.declined.labels(reason=reason).inc()
        return verdict

    async def _announce(
        self,
        verdict: ExplorationVerdict,
        evidence: EvidenceStatus,
        spend: ExplorationSpend,
        candidate: ExplorationCandidate,
    ) -> None:
        """Put the consequential transitions on the bus, each exactly once."""
        if verdict.granted:
            _logger.info(
                "exploration_granted",
                subject=verdict.subject,
                notional_usd=verdict.notional_usd,
                cohort=verdict.cohort_key,
                evidence=verdict.evidence_summary,
            )
            if self._bus is not None:
                await self._bus.publish(
                    ExplorationGranted(
                        aggregate_id=new_id(),
                        subject=verdict.subject,
                        notional_usd=verdict.notional_usd,
                        cohort_key=verdict.cohort_key,
                        cohort_count=verdict.cohort_count,
                        prob_roi_positive=candidate.prob_roi_positive,
                        confidence=candidate.confidence,
                        evidence_summary=verdict.evidence_summary,
                        budget_summary=verdict.budget_summary,
                        reasons=verdict.reasons,
                        correlation_id_ref=candidate.correlation_id,
                    )
                )
            return

        if verdict.decline is ExplorationDecline.EVIDENCE_SUFFICIENT:
            await self._complete(evidence)
            return

        window = _EXHAUSTION_WINDOWS.get(verdict.decline or ExplorationDecline.DISABLED)
        if window is not None:
            await self._announce_exhaustion(window, verdict, spend)

    async def _complete(self, evidence: EvidenceStatus) -> None:
        """Latch the programme off. The success condition, announced once."""
        if self._completed:
            return
        self._completed = True
        if self._metrics is not None:
            self._metrics.active.set(0.0)
        _logger.info(
            "exploration_completed",
            lessons=evidence.lessons,
            positive=evidence.positive,
            negative=evidence.negative,
        )
        if self._bus is not None:
            await self._bus.publish(
                ExplorationCompleted(
                    aggregate_id=new_id(),
                    lessons=evidence.lessons,
                    positive=evidence.positive,
                    negative=evidence.negative,
                    detail=(
                        "exploration switched itself off: " + evidence.summary + " — the "
                        "committee can now be validated against ground truth"
                    ),
                )
            )

    async def _announce_exhaustion(
        self, window: str, verdict: ExplorationVerdict, spend: ExplorationSpend
    ) -> None:
        # Announced once per window *instance*: a new day gets a new
        # announcement, the same day does not get one per candidate.
        stamp = {
            "daily": (verdict.at or datetime.now(UTC)).strftime("%Y-%m-%d"),
            "weekly": (verdict.at or datetime.now(UTC)).strftime("%G-W%V"),
            "total": "lifetime",
        }[window]
        if self._announced.get(window) == stamp:
            return
        self._announced[window] = stamp
        spent, limit = {
            "daily": (spend.day_usd, self._cfg.budget.daily_usd),
            "weekly": (spend.week_usd, self._cfg.budget.weekly_usd),
            "total": (spend.total_usd, self._cfg.budget.total_usd),
        }[window]
        _logger.info("exploration_budget_exhausted", window=window, spent_usd=round(spent, 4))
        if self._bus is not None:
            await self._bus.publish(
                ExplorationBudgetExhausted(
                    aggregate_id=new_id(),
                    window=window,
                    spent_usd=round(spent, 6),
                    limit_usd=limit,
                    detail=verdict.reasons[0] if verdict.reasons else "",
                )
            )


__all__ = ["ExplorationService"]
