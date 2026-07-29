"""The Risk Manager - the only component authorised to approve or reject a trade.

This is the guardian of capital and the orchestrator of the whole risk
subsystem. It runs the Trade Approval chain for one candidate:

    global gates (emergency / circuit breaker / kill switch)
        -> quality policies (probability, confidence, security, dev, wallet, liq)
        -> Position Sizing Engine (dynamic, conviction-weighted)
        -> allocation policies (positions, capital, exposure, correlation,
           risk budget, drawdown, trade rate)
        -> APPROVE (with sized envelope) or REJECT (with the vetoing reason)

Every outcome is a fully explainable :class:`RiskAssessment` - drivers, blockers
and caveats - published as ``TradeApproved`` / ``TradeRejected`` and written to
the append-only audit store. The manager also owns the defence layer (Kill
Switch, Circuit Breaker, Emergency Mode) and persists that control state so a
halt survives restarts.

It NEVER executes anything. An approval is a permission slip with a size on it;
the Execution Engine (a later phase) is the only thing that turns it into an
order. No trade is ever executed without an approval from here - that invariant
is the reason this component exists.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.risk.application.circuit_breaker import CircuitBreaker
from hades.contexts.risk.application.kill_switch import KillSwitch
from hades.contexts.risk.application.metrics import RiskMetrics
from hades.contexts.risk.application.sizing import PositionSizingEngine
from hades.contexts.risk.domain.events import (
    CircuitBreakerReset,
    CircuitBreakerTripped,
    EmergencyModeEntered,
    EmergencyModeExited,
    KillSwitchLevelChanged,
    KillSwitchReset,
    RiskReduced,
    TradeApproved,
    TradeRejected,
)
from hades.contexts.risk.domain.models import (
    CircuitBreakerReason,
    ExplorationGrant,
    KillSwitchLevel,
    PolicyOutcome,
    PortfolioRiskState,
    ReasonKind,
    RiskAssessment,
    RiskCandidate,
    RiskConfig,
    RiskControlState,
    RiskDecision,
    RiskReason,
    RiskRejectReason,
    RiskSnapshot,
    SizingDecision,
)
from hades.contexts.risk.domain.ports import (
    ExplorationPort,
    RiskAuditStore,
    RiskPolicy,
    RiskStateStore,
)
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("risk.manager")


class RiskManager:
    """Approves or rejects trades and owns the defence layer. Never executes."""

    def __init__(
        self,
        *,
        config: RiskConfig,
        sizing: PositionSizingEngine,
        quality_policies: Sequence[RiskPolicy],
        allocation_policies: Sequence[RiskPolicy],
        kill_switch: KillSwitch,
        circuit_breaker: CircuitBreaker,
        conviction_policies: Sequence[RiskPolicy] = (),
        exploration: ExplorationPort | None = None,
        event_bus: EventBus | None = None,
        audit: RiskAuditStore | None = None,
        state_store: RiskStateStore | None = None,
        notifier: NotificationPublisher | None = None,
        metrics: RiskMetrics | None = None,
    ) -> None:
        self._cfg = config
        self._sizing = sizing
        # ``quality`` is the SAFETY tuple and ``conviction`` the waivable one.
        # The split is the whole mechanism by which exploration cannot weaken a
        # safety rule: the manager only ever consults ``_conviction`` when
        # deciding what a grant may cover, so a rule placed in ``_quality``
        # cannot be waived even by a caller who wants it to be.
        self._quality = tuple(quality_policies)
        self._conviction = tuple(conviction_policies)
        self._exploration = exploration
        self._allocation = tuple(allocation_policies)
        self._kill = kill_switch
        self._breaker = circuit_breaker
        self._bus = event_bus
        self._audit = audit
        self._state_store = state_store
        self._notifier = notifier
        self._metrics = metrics
        self._emergency = False
        self._approvals = 0
        self._rejections = 0
        self._exploration_approvals = 0

    # -- the approval chain ---------------------------------------------------

    async def evaluate(self, candidate: RiskCandidate, state: PortfolioRiskState) -> RiskAssessment:
        """Run the full trade-approval chain for one candidate."""
        started = time.perf_counter()
        try:
            assessment = await self._decide(candidate, state)
        except Exception as exc:  # a bad candidate must never crash the guardian
            if self._metrics is not None:
                self._metrics.errors.inc()
            _logger.warning(
                "risk_evaluation_failed", mint=str(candidate.token.mint), error=str(exc)
            )
            assessment = self._reject(
                candidate,
                RiskRejectReason.TRADING_HALTED,
                f"risk evaluation error: {exc}",
                policies=(),
            )
        if self._metrics is not None:
            self._metrics.review_seconds.observe(time.perf_counter() - started)
        await self._publish(assessment, candidate)
        return assessment

    async def _decide(self, candidate: RiskCandidate, state: PortfolioRiskState) -> RiskAssessment:
        # 1. Global gates - the whole book, before anything token-specific. The
        #    defence layer is never waivable: an exploration trade under an open
        #    circuit breaker or an engaged kill switch is still a trade, and the
        #    reason those halts exist does not care how small it is.
        gate = self._global_gate(candidate, state)
        if gate is not None:
            return gate

        # 2. SAFETY policies - is the token itself fit to touch at any size?
        #    Security, developer, wallet, liquidity. Nothing waives these.
        for policy in self._quality:
            outcome = policy.evaluate(candidate, state, 0.0)
            if not outcome.passed:
                return self._reject(
                    candidate,
                    outcome.reason or RiskRejectReason.TRADING_HALTED,
                    outcome.detail,
                    policies=(outcome,),
                )

        # 3. CONVICTION policies - is the opportunity good enough to back? These
        #    are the only rules an exploration grant may waive, and it may waive
        #    one of them, named, at a fixed tiny size.
        grant: ExplorationGrant | None = None
        vetoed: PolicyOutcome | None = None
        for policy in self._conviction:
            outcome = policy.evaluate(candidate, state, 0.0)
            if not outcome.passed:
                vetoed = outcome
                break
        if vetoed is not None:
            grant = await self._consider_exploration(candidate, vetoed)
            if grant is None:
                return self._reject(
                    candidate,
                    vetoed.reason or RiskRejectReason.TRADING_HALTED,
                    vetoed.detail,
                    policies=(vetoed,),
                )

        # 4. Position Sizing. Under a grant the size is the fixed exploration
        #    sample, not a conviction-weighted amount - see PositionSizingEngine.
        if grant is not None:
            sizing = self._sizing.explore(grant, available_usd=state.capital.available_usd)
        else:
            sizing = self._sizing.size(
                candidate,
                equity_usd=state.capital.equity_usd,
                available_usd=state.capital.available_usd,
                kill_switch_factor=self._kill.state.size_factor,
            )
        if not sizing.approved or sizing.sizing is None:
            return self._reject(
                candidate, RiskRejectReason.POSITION_TOO_SMALL, sizing.detail, policies=()
            )
        notional = float(sizing.sizing.notional.amount)

        # 5. Allocation policies - does the book have room for this size? Run in
        #    full for exploration too: the budget bounds what the programme may
        #    spend, but the portfolio's capital, exposure, correlation, drawdown
        #    and rate limits bound what the *book* will carry, and a cheap trade
        #    is not exempt from either.
        passed: list[PolicyOutcome] = []
        for policy in self._allocation:
            outcome = policy.evaluate(candidate, state, notional)
            passed.append(outcome)
            if not outcome.passed:
                return self._reject(
                    candidate,
                    outcome.reason or RiskRejectReason.TRADING_HALTED,
                    outcome.detail,
                    policies=tuple(passed),
                )

        # 6. Approved. The exploration budget is charged here and nowhere else:
        #    a grant that got this far and was then vetoed above cost nothing.
        assessment = self._approve(candidate, sizing, tuple(passed), grant=grant)
        if grant is not None:
            await self._charge_exploration(candidate, grant)
        return assessment

    async def _consider_exploration(
        self, candidate: RiskCandidate, vetoed: PolicyOutcome
    ) -> ExplorationGrant | None:
        """Ask the exploration programme whether this veto is worth sampling.

        Wrapped because the guardian's correctness must not depend on an optional
        collaborator's: an exception here means *no grant*, so the candidate is
        rejected exactly as it would have been if exploration did not exist.
        Failing the other way - letting an error produce a trade - is the one
        outcome this function may never have.
        """
        if self._exploration is None:
            return None
        try:
            return await self._exploration.consider(candidate, waived_policy=vetoed.policy)
        except Exception as exc:
            if self._metrics is not None:
                self._metrics.errors.inc()
            _logger.warning(
                "exploration_unavailable", mint=str(candidate.token.mint), error=str(exc)
            )
            return None

    async def _charge_exploration(self, candidate: RiskCandidate, grant: ExplorationGrant) -> None:
        if self._exploration is None:
            return
        try:
            await self._exploration.commit(candidate, grant)
        except Exception as exc:  # the trade is approved; the charge is not lost silently
            _logger.error(
                "exploration_charge_failed", mint=str(candidate.token.mint), error=str(exc)
            )

    def _global_gate(
        self, candidate: RiskCandidate, state: PortfolioRiskState
    ) -> RiskAssessment | None:
        if self._emergency:
            return self._reject(
                candidate, RiskRejectReason.EMERGENCY_MODE, "emergency mode active", policies=()
            )
        if self._breaker.is_open():
            reasons = ", ".join(r.value for r in self._breaker.state.reasons) or "unsafe conditions"
            return self._reject(
                candidate,
                RiskRejectReason.CIRCUIT_BREAKER_OPEN,
                f"circuit breaker open: {reasons}",
                policies=(),
            )
        ks = self._kill.evaluate(state.drawdown)
        if ks.blocks_entries:
            return self._reject(
                candidate,
                RiskRejectReason.KILL_SWITCH_ENGAGED,
                f"kill switch at {ks.level.label}: {ks.reason}",
                policies=(),
            )
        return None

    # -- assessment construction ----------------------------------------------

    def _approve(
        self,
        candidate: RiskCandidate,
        sizing: SizingDecision,
        policies: tuple[PolicyOutcome, ...],
        *,
        grant: ExplorationGrant | None = None,
    ) -> RiskAssessment:
        assert sizing.sizing is not None
        notional = float(sizing.sizing.notional.amount)
        reasons = self._drivers(candidate) + self._caveats(candidate)
        if grant is not None:
            # An exploration approval must never read like a conviction one. The
            # headline says so, and the reasons carry the arithmetic that bought
            # the sample - including, first, the plain statement that this trade
            # was not taken for its edge.
            reasons = self._exploration_reasons(grant) + reasons
            headline = (
                f"Approved ${notional:.4f} as an EXPLORATION sample "
                f"(P(ROI+) {candidate.prob_roi_positive:.2f}, waives "
                f"{grant.waived_policy}; {grant.evidence_summary})"
            )
            self._exploration_approvals += 1
            if self._metrics is not None:
                self._metrics.exploration_approvals.inc()
        else:
            headline = (
                f"Approved ${notional:.4f} (conviction {sizing.conviction:.2f}, "
                f"P(ROI+) {candidate.prob_roi_positive:.2f}, risk ${sizing.risk_amount_usd:.2f})"
            )
        self._approvals += 1
        if self._metrics is not None:
            self._metrics.approvals.inc()
        return RiskAssessment(
            exploration=grant,
            token=candidate.token,
            at=datetime.now(UTC),
            decision=RiskDecision.APPROVE,
            sizing=sizing.sizing,
            conviction=sizing.conviction,
            risk_amount_usd=sizing.risk_amount_usd,
            headline=headline,
            reasons=reasons,
            policies=policies,
            kill_switch_level=int(self._kill.state.level),
            circuit_breaker_open=self._breaker.state.open,
            correlation_id=candidate.correlation_id,
        )

    def _reject(
        self,
        candidate: RiskCandidate,
        reason: RiskRejectReason,
        detail: str,
        *,
        policies: tuple[PolicyOutcome, ...],
    ) -> RiskAssessment:
        reasons = (
            RiskReason(kind=ReasonKind.BLOCKER, text=detail or reason.value),
            *self._caveats(candidate),
        )
        self._rejections += 1
        if self._metrics is not None:
            self._metrics.rejections.labels(reason=reason.value).inc()
        return RiskAssessment(
            token=candidate.token,
            at=datetime.now(UTC),
            decision=RiskDecision.REJECT,
            reject_reason=reason,
            headline=f"Rejected: {detail or reason.value}",
            reasons=reasons,
            policies=policies,
            kill_switch_level=int(self._kill.state.level),
            circuit_breaker_open=self._breaker.state.open,
            correlation_id=candidate.correlation_id,
        )

    @staticmethod
    def _exploration_reasons(grant: ExplorationGrant) -> tuple[RiskReason, ...]:
        """Spell out, in the decision itself, that this was bought not backed."""
        cohort = (
            f"cohort {grant.cohort_key} known from {grant.cohort_count} settled trades"
            if grant.cohort_key
            else "no cohort attribution - sampled against the global population"
        )
        return (
            RiskReason(
                kind=ReasonKind.CAVEAT,
                text=(
                    "EXPLORATION trade: taken to generate evidence, not for expected "
                    f"return. Fixed size ${grant.notional_usd:.4f} on the independent "
                    f"exploration budget; waives only '{grant.waived_policy}'"
                ),
            ),
            RiskReason(kind=ReasonKind.CAVEAT, text=f"evidence: {grant.evidence_summary}"),
            RiskReason(kind=ReasonKind.CAVEAT, text=f"budget: {grant.budget_summary}"),
            RiskReason(kind=ReasonKind.DRIVER, text=cohort),
            *(RiskReason(kind=ReasonKind.DRIVER, text=text) for text in grant.reasons),
        )

    @staticmethod
    def _drivers(c: RiskCandidate) -> tuple[RiskReason, ...]:
        out: list[RiskReason] = []
        if c.prob_roi_positive >= 0.6:
            out.append(
                RiskReason(kind=ReasonKind.DRIVER, text=f"high P(ROI+) {c.prob_roi_positive:.2f}")
            )
        if c.expected_edge > 0:
            out.append(
                RiskReason(kind=ReasonKind.DRIVER, text=f"positive edge {c.expected_edge:+.2f}")
            )
        if c.security_score >= 80:
            out.append(
                RiskReason(kind=ReasonKind.DRIVER, text=f"strong security {c.security_score:.0f}")
            )
        if c.wallet_risk_score <= 40:
            out.append(RiskReason(kind=ReasonKind.DRIVER, text="healthy wallet profile"))
        if c.regime in ("bull", "fomo"):
            out.append(RiskReason(kind=ReasonKind.DRIVER, text=f"favourable regime ({c.regime})"))
        return tuple(out)

    @staticmethod
    def _caveats(c: RiskCandidate) -> tuple[RiskReason, ...]:
        out: list[RiskReason] = []
        if c.confidence < 0.5:
            out.append(
                RiskReason(kind=ReasonKind.CAVEAT, text=f"low confidence {c.confidence:.2f}")
            )
        if c.regime in ("panic", "bear", "highly_volatile"):
            out.append(RiskReason(kind=ReasonKind.CAVEAT, text=f"hostile regime ({c.regime})"))
        if 0 < c.liquidity_usd < 15_000:
            out.append(RiskReason(kind=ReasonKind.CAVEAT, text="thin liquidity"))
        return tuple(out)

    # -- publication ----------------------------------------------------------

    async def _publish(self, assessment: RiskAssessment, candidate: RiskCandidate) -> None:
        if self._audit is not None:
            try:
                await self._audit.record(assessment)
            except Exception as exc:  # audit best-effort, never blocks a decision
                _logger.warning("risk_audit_failed", error=str(exc))
        if self._bus is None:
            return
        if assessment.approved and assessment.sizing is not None:
            await self._bus.publish(
                TradeApproved(
                    aggregate_id=new_id(),
                    token=candidate.token,
                    sizing=assessment.sizing,
                    conviction=assessment.conviction,
                    risk_amount_usd=assessment.risk_amount_usd,
                    rationale=assessment.headline,
                    strategy=candidate.strategy,
                    developer=candidate.developer,
                    cluster=candidate.cluster,
                    narrative=candidate.narrative,
                    regime=candidate.regime,
                    # Travels with the approval so the position, the fill and —
                    # the point of the whole programme — the settled lesson can
                    # all be attributed to the exploration budget that paid for
                    # them. Without it the memory would hold the trade but not
                    # the fact that it was bought to be learned from.
                    exploration=assessment.is_exploration,
                    correlation_id_ref=candidate.correlation_id,
                )
            )
        else:
            await self._bus.publish(
                TradeRejected(
                    aggregate_id=new_id(),
                    token=candidate.token,
                    reason=assessment.reject_reason or RiskRejectReason.TRADING_HALTED,
                    detail=assessment.headline,
                    strategy=candidate.strategy,
                    correlation_id_ref=candidate.correlation_id,
                )
            )

    # -- defence-layer control ------------------------------------------------

    async def record_trade_result(self, *, is_win: bool) -> None:
        """Feed a closed-trade outcome to the Kill Switch and react to changes."""
        previous = self._kill.state.level
        new = self._kill.record_result(is_win=is_win)
        if new.level != previous:
            await self._on_kill_switch_change(previous, new.level, new.reason)
        if new.size_factor < 1.0 and self._bus is not None:
            await self._bus.publish(
                RiskReduced(
                    aggregate_id=new_id(),
                    reason=new.reason,
                    size_factor=new.size_factor,
                    consecutive_losses=new.consecutive_losses,
                )
            )
        await self._persist()

    async def _on_kill_switch_change(
        self, previous: KillSwitchLevel, level: KillSwitchLevel, reason: str
    ) -> None:
        if self._metrics is not None:
            self._metrics.kill_switch_changes.inc()
            self._metrics.kill_switch_level.set(int(level))
        _logger.warning(
            "kill_switch_level_changed", previous=int(previous), level=int(level), reason=reason
        )
        if level is KillSwitchLevel.EMERGENCY:
            await self.enter_emergency(f"kill switch escalated: {reason}")
        if self._bus is not None:
            await self._bus.publish(
                KillSwitchLevelChanged(
                    aggregate_id=new_id(),
                    previous_level=int(previous),
                    level=int(level),
                    label=level.label,
                    reason=reason,
                )
            )
        if self._notifier is not None and level >= KillSwitchLevel.HALTED:
            await self._notifier.notify(
                title=f"Kill Switch: {level.label}",
                body=reason,
                severity=Severity.CRITICAL,
                channel="alerts",
                dedup_key=f"killswitch-{level.label}",
            )

    async def reset_kill_switch(self) -> None:
        self._kill.reset()
        if self._metrics is not None:
            self._metrics.kill_switch_level.set(0)
        if self._bus is not None:
            await self._bus.publish(KillSwitchReset(aggregate_id=new_id()))
        await self._persist()

    async def trip_circuit_breaker(self, reason: CircuitBreakerReason, detail: str = "") -> None:
        state = self._breaker.trip(reason, detail)
        if self._metrics is not None:
            self._metrics.circuit_breaker_trips.labels(reason=reason.value).inc()
            self._metrics.circuit_breaker_open.set(1.0)
        if self._bus is not None:
            await self._bus.publish(
                CircuitBreakerTripped(
                    aggregate_id=new_id(), reasons=state.reasons, detail=state.detail
                )
            )
        if self._notifier is not None:
            await self._notifier.notify(
                title="Circuit Breaker tripped",
                body=detail or reason.value,
                severity=Severity.CRITICAL,
                channel="alerts",
                dedup_key=f"breaker-{reason.value}",
            )
        await self._persist()

    async def reset_circuit_breaker(self) -> None:
        self._breaker.close("manual reset")
        if self._metrics is not None:
            self._metrics.circuit_breaker_open.set(0.0)
        if self._bus is not None:
            await self._bus.publish(
                CircuitBreakerReset(aggregate_id=new_id(), detail="manual reset")
            )
        await self._persist()

    async def enter_emergency(self, reason: str) -> None:
        if self._emergency:
            return
        self._emergency = True
        if self._metrics is not None:
            self._metrics.emergency_mode.set(1.0)
        _logger.error("emergency_mode_entered", reason=reason)
        if self._bus is not None:
            await self._bus.publish(EmergencyModeEntered(aggregate_id=new_id(), reason=reason))
        if self._notifier is not None:
            await self._notifier.notify(
                title="EMERGENCY MODE",
                body=reason,
                severity=Severity.CRITICAL,
                channel="alerts",
                dedup_key="emergency",
            )
        await self._persist()

    async def exit_emergency(self, reason: str = "manual") -> None:
        if not self._emergency:
            return
        self._emergency = False
        if self._metrics is not None:
            self._metrics.emergency_mode.set(0.0)
        if self._bus is not None:
            await self._bus.publish(EmergencyModeExited(aggregate_id=new_id(), reason=reason))
        await self._persist()

    # -- state --------------------------------------------------------------

    @property
    def emergency_mode(self) -> bool:
        return self._emergency

    def control_state(self) -> RiskControlState:
        return RiskControlState(
            kill_switch=self._kill.state,
            circuit_breaker=self._breaker.state,
            emergency_mode=self._emergency,
        )

    def restore(self, control: RiskControlState) -> None:
        """Rehydrate the defence layer from persisted state (on startup)."""
        self._kill.set_state(control.kill_switch)
        self._breaker.set_state(control.circuit_breaker)
        self._emergency = control.emergency_mode
        if self._metrics is not None:
            self._metrics.kill_switch_level.set(int(control.kill_switch.level))
            self._metrics.circuit_breaker_open.set(1.0 if control.circuit_breaker.open else 0.0)
            self._metrics.emergency_mode.set(1.0 if control.emergency_mode else 0.0)

    def snapshot(self, state: PortfolioRiskState | None = None) -> RiskSnapshot:
        exposure = 0.0
        if state is not None and state.capital.equity_usd > 0:
            exposure = round(
                sum(p.notional_usd for p in state.open_positions)
                / state.capital.equity_usd
                * 100.0,
                4,
            )
            if self._metrics is not None:
                self._metrics.portfolio_exposure_pct.set(exposure)
        return RiskSnapshot(
            at=datetime.now(UTC),
            kill_switch=self._kill.state,
            circuit_breaker=self._breaker.state,
            emergency_mode=self._emergency,
            open_positions=state.open_count if state else 0,
            portfolio_exposure_pct=exposure,
            daily_loss_pct=state.drawdown.daily_loss_pct if state else 0.0,
            drawdown_pct=state.drawdown.drawdown_pct if state else 0.0,
            consecutive_losses=self._kill.state.consecutive_losses,
            approvals_session=self._approvals,
            rejections_session=self._rejections,
            exploration_approvals_session=self._exploration_approvals,
        )

    async def _persist(self) -> None:
        if self._state_store is None:
            return
        try:
            await self._state_store.save(self.control_state())
        except Exception as exc:  # persistence best-effort; state stays in memory
            _logger.warning("risk_control_persist_failed", error=str(exc))
