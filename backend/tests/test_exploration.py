"""Exploration: the budgeted, self-terminating answer to the cold start.

Organised around the four ways this component could be wrong while looking
right, which is the useful axis for a thing that spends money to buy evidence:

1. **It could waive the wrong rule.** A grant is supposed to relax the AI
   Committee's conviction gates and nothing else. A version that also softened
   the security, developer, wallet or liquidity checks would look identical on
   every dashboard — a few extra tiny trades — while quietly buying rug pulls.
2. **It could refuse to end.** Auto-shutdown is the property that makes the
   programme safe to switch on without a calendar reminder. A version that
   re-decided on every candidate, or that measured sufficiency against a total
   without checking both classes, would keep spending forever on a memory that
   already had what it needed — or on one that never could.
3. **It could overspend.** Four independent ceilings, each derived from an
   append-only ledger. A version accumulating a total in memory would look
   correct until the first restart.
4. **It could stop being explainable.** Every verdict has to carry the
   arithmetic that produced it, and an exploration approval must never be
   readable, afterwards, as an ordinary conviction trade that happened to be
   small.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.exploration.application.factory import exploration_config_from_settings
from hades.contexts.exploration.application.policy import (
    ExplorationPolicy,
    day_start,
    week_start,
)
from hades.contexts.exploration.application.service import ExplorationService
from hades.contexts.exploration.domain.events import (
    ExplorationBudgetExhausted,
    ExplorationCompleted,
    ExplorationGranted,
    ExplorationSpent,
)
from hades.contexts.exploration.domain.models import (
    EvidenceStatus,
    EvidenceTarget,
    ExplorationBudget,
    ExplorationCandidate,
    ExplorationConfig,
    ExplorationDecline,
    ExplorationRecord,
    ExplorationSpend,
)
from hades.contexts.exploration.infrastructure.evidence import KnowledgeEvidence
from hades.contexts.exploration.infrastructure.stores import InMemoryExplorationLedger
from hades.contexts.knowledge.domain.models import Lesson
from hades.contexts.knowledge.infrastructure.stores import InMemoryLessonStore
from hades.contexts.risk.application.factory import build_risk_manager
from hades.contexts.risk.domain.events import TradeApproved, TradeRejected
from hades.contexts.risk.domain.models import (
    CapitalSnapshot,
    DrawdownSnapshot,
    PortfolioRiskState,
    RiskCandidate,
    RiskConfig,
    RiskDecision,
    RiskRejectReason,
    SizingConfig,
)
from hades.contexts.risk.infrastructure.exploration import ExplorationAdapter
from hades.contexts.risk.infrastructure.stores import (
    InMemoryRiskAuditStore,
    InMemoryRiskStateStore,
)
from hades.shared_kernel.config.settings import Settings
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import InMemoryEventBus

_MINT = "So11111111111111111111111111111111111111112"

# The band the default configuration samples from is [0.35, 0.55]; 0.45 sits in
# the middle of it, comfortably below the production floor of 0.55.
_UNCERTAIN = 0.45


# --- fixtures ----------------------------------------------------------------


def _config(**over: object) -> ExplorationConfig:
    base: dict[str, object] = {
        "enabled": True,
        "budget": ExplorationBudget(
            per_trade_usd=1.0,
            daily_usd=5.0,
            weekly_usd=20.0,
            total_usd=50.0,
            max_trades_per_day=5,
            max_trades_per_week=20,
        ),
        "target": EvidenceTarget(min_lessons=20, min_per_class=5, cohort_target=3),
    }
    base.update(over)
    return ExplorationConfig(**base)  # type: ignore[arg-type]


def _candidate(**over: object) -> ExplorationCandidate:
    base: dict[str, object] = {
        "subject": _MINT,
        "prob_roi_positive": _UNCERTAIN,
        "confidence": 0.4,
        "cohorts": {"developer": "devA"},
    }
    base.update(over)
    return ExplorationCandidate(**base)  # type: ignore[arg-type]


def _evidence(**over: object) -> EvidenceStatus:
    base: dict[str, object] = {
        "lessons": 2,
        "positive": 1,
        "negative": 1,
        "cohorts": {},
        "target": EvidenceTarget(min_lessons=20, min_per_class=5, cohort_target=3),
    }
    base.update(over)
    return EvidenceStatus(**base)  # type: ignore[arg-type]


def _lesson(index: int, *, positive: bool, tags: dict[str, str] | None = None) -> Lesson:
    at = datetime.now(UTC) - timedelta(hours=index + 1)
    return Lesson(
        ref=f"ref-{index}",
        subject=_MINT,
        decided_at=at,
        settled_at=at + timedelta(minutes=30),
        features={"liquidity": 1.0},
        tags=tags or {},
        realized_roi=0.2 if positive else -0.2,
        label_roi_positive=positive,
        label_hit_tp=positive,
        label_hit_sl=not positive,
    )


class _StubEvidence:
    """An evidence port that returns whatever the test says the memory holds."""

    def __init__(self, status: EvidenceStatus) -> None:
        self.status_value = status
        self.calls = 0

    async def status(self, *, cohort_dimensions: object) -> EvidenceStatus:
        self.calls += 1
        return self.status_value


class _BrokenEvidence:
    async def status(self, *, cohort_dimensions: object) -> EvidenceStatus:
        raise RuntimeError("permanent memory is down")


def _service(
    config: ExplorationConfig | None = None,
    evidence: EvidenceStatus | None = None,
    ledger: InMemoryExplorationLedger | None = None,
    bus: InMemoryEventBus | None = None,
) -> ExplorationService:
    return ExplorationService(
        config or _config(),
        _StubEvidence(evidence or _evidence()),
        ledger or InMemoryExplorationLedger(),
        event_bus=bus,
        # The caches exist for the hot path, not for the tests: a test that had
        # to reason about a TTL would be testing the clock.
        evidence_ttl_seconds=0.0,
        spend_ttl_seconds=0.0,
    )


# =============================================================================
# 1. The policy is a stated arithmetic, and it is deterministic
# =============================================================================


def _decide(
    config: ExplorationConfig,
    candidate: ExplorationCandidate,
    evidence: EvidenceStatus,
    spend: ExplorationSpend | None = None,
):
    return ExplorationPolicy(config).decide(
        candidate, evidence, spend or ExplorationSpend(), now=datetime.now(UTC)
    )


def test_a_short_memory_and_an_uncertain_candidate_earn_a_sample() -> None:
    verdict = _decide(_config(), _candidate(), _evidence())
    assert verdict.granted
    assert verdict.notional_usd == 1.0
    assert verdict.cohort_key == "developer=devA"
    # Explainability is the product here, not a side effect: the reasons must
    # state the band, the cohort, the evidence gap and the fixed size.
    joined = " ".join(verdict.reasons).lower()
    assert "band" in joined
    assert "cohort" in joined
    assert "fixed" in joined
    assert "lessons" in verdict.evidence_summary


def test_the_same_inputs_always_produce_the_same_verdict() -> None:
    """No randomness anywhere. A programme whose selection cannot be replayed
    cannot defend the trades it bought."""
    config, candidate, evidence = _config(), _candidate(), _evidence()
    verdicts = [_decide(config, candidate, evidence) for _ in range(25)]
    assert {(v.granted, v.cohort_key, v.notional_usd) for v in verdicts} == {
        (True, "developer=devA", 1.0)
    }


def test_disabled_is_the_default_and_grants_nothing() -> None:
    assert ExplorationConfig().enabled is False
    verdict = _decide(_config(enabled=False), _candidate(), _evidence())
    assert not verdict.granted
    assert verdict.decline is ExplorationDecline.DISABLED


def test_a_candidate_below_the_floor_is_not_an_open_question() -> None:
    verdict = _decide(_config(), _candidate(prob_roi_positive=0.05), _evidence())
    assert verdict.decline is ExplorationDecline.OUTSIDE_BAND
    assert "floor" in verdict.reasons[0]


def test_a_candidate_above_the_ceiling_belongs_to_the_production_path() -> None:
    """Exploration must not spend its budget on a trade the platform was going
    to make anyway — that would flatter the programme with someone else's win."""
    verdict = _decide(_config(), _candidate(prob_roi_positive=0.95), _evidence())
    assert verdict.decline is ExplorationDecline.OUTSIDE_BAND
    assert "ceiling" in verdict.reasons[0]


def test_a_saturated_cohort_buys_precision_the_platform_does_not_lack() -> None:
    evidence = _evidence(cohorts={"developer=devA": 3})  # cohort_target is 3
    assert _decide(_config(), _candidate(), evidence).decline is (
        ExplorationDecline.COHORT_SATURATED
    )


def test_the_least_known_cohort_is_the_one_that_justifies_the_trade() -> None:
    """One thin cohort is enough, and it is the thin one that gets named."""
    evidence = _evidence(cohorts={"developer=devA": 9, "narrative=cat": 1})
    verdict = _decide(
        _config(),
        _candidate(cohorts={"developer": "devA", "narrative": "cat"}),
        evidence,
    )
    assert verdict.granted
    assert verdict.cohort_key == "narrative=cat"
    assert verdict.cohort_count == 1


def test_an_unattributed_candidate_samples_the_global_population() -> None:
    verdict = _decide(_config(), _candidate(cohorts={}), _evidence())
    assert verdict.granted
    assert verdict.cohort_key is None
    assert "global population" in " ".join(verdict.reasons)


def test_an_inverted_band_is_rejected_at_construction() -> None:
    """A band that accepts nothing is the hardest failure here to notice: the
    programme reports itself active and simply never fires."""
    try:
        ExplorationConfig(min_prob_roi_positive=0.6, max_prob_roi_positive=0.4)
    except ValueError as exc:
        assert "inverted band" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("an inverted exploration band must not be constructible")


# =============================================================================
# 2. It turns itself off — and for the right reason
# =============================================================================


def test_sufficiency_needs_both_classes_not_just_a_count() -> None:
    """The condition that kept this platform in cold start. Sixty lessons all on
    one side of zero are a single-class dataset: the AUC is undefined and no
    validation gate can pass, however healthy the count looks."""
    target = EvidenceTarget(min_lessons=20, min_per_class=5)
    all_losses = EvidenceStatus(lessons=60, positive=0, negative=60, target=target)
    assert all_losses.sufficient is False

    thin_positives = EvidenceStatus(lessons=60, positive=2, negative=58, target=target)
    assert thin_positives.sufficient is False

    both = EvidenceStatus(lessons=60, positive=30, negative=30, target=target)
    assert both.sufficient is True


def test_a_sufficient_memory_declines_and_never_spends_again() -> None:
    evidence = _evidence(lessons=40, positive=20, negative=20)
    verdict = _decide(_config(), _candidate(), evidence)
    assert verdict.decline is ExplorationDecline.EVIDENCE_SUFFICIENT


async def test_reaching_sufficiency_latches_the_programme_off_and_announces_it() -> None:
    bus = InMemoryEventBus()
    seen: list[DomainEvent] = []

    async def _capture(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(ExplorationCompleted.__name__, _capture)

    service = _service(evidence=_evidence(lessons=40, positive=20, negative=20), bus=bus)
    await service.consider(_candidate())
    assert service.completed
    assert len(seen) == 1 and isinstance(seen[0], ExplorationCompleted)

    # Announced exactly once, however many candidates arrive afterwards, and
    # short-circuited without touching the memory again.
    for _ in range(5):
        verdict = await service.consider(_candidate())
        assert verdict.decline is ExplorationDecline.EVIDENCE_SUFFICIENT
    assert len(seen) == 1


async def test_completion_does_not_unlatch_if_a_later_read_undercounts() -> None:
    """Lessons are append-only, so sufficiency cannot genuinely be lost. A
    transient read that said otherwise must not restart a finished programme
    and start spending again with no announcement that it had."""
    stub = _StubEvidence(_evidence(lessons=40, positive=20, negative=20))
    service = ExplorationService(
        _config(),
        stub,
        InMemoryExplorationLedger(),
        evidence_ttl_seconds=0.0,
        spend_ttl_seconds=0.0,
    )
    await service.consider(_candidate())
    assert service.completed

    stub.status_value = _evidence(lessons=0, positive=0, negative=0)
    verdict = await service.consider(_candidate())
    assert not verdict.granted
    assert verdict.decline is ExplorationDecline.EVIDENCE_SUFFICIENT


async def test_an_unreadable_memory_fails_closed() -> None:
    """Not knowing whether more evidence is needed is a reason to stop spending,
    never a reason to carry on."""
    service = ExplorationService(
        _config(),
        _BrokenEvidence(),
        InMemoryExplorationLedger(),
        evidence_ttl_seconds=0.0,
        spend_ttl_seconds=0.0,
    )
    verdict = await service.consider(_candidate())
    assert not verdict.granted
    assert verdict.decline is ExplorationDecline.UNAVAILABLE


# =============================================================================
# 3. The budget is four ceilings, derived from an append-only ledger
# =============================================================================


def test_each_ceiling_declines_with_its_own_cause() -> None:
    config = _config()
    cases = [
        (ExplorationSpend(total_usd=50.0), ExplorationDecline.TOTAL_BUDGET),
        (ExplorationSpend(week_usd=20.0), ExplorationDecline.WEEKLY_BUDGET),
        (ExplorationSpend(week_trades=20), ExplorationDecline.WEEKLY_TRADES),
        (ExplorationSpend(day_usd=5.0), ExplorationDecline.DAILY_BUDGET),
        (ExplorationSpend(day_trades=5), ExplorationDecline.DAILY_TRADES),
    ]
    for spend, expected in cases:
        assert _decide(config, _candidate(), _evidence(), spend).decline is expected


def test_the_lifetime_budget_states_exactly_how_many_samples_exist() -> None:
    """The size is fixed, so the budget is a trade count, knowable in advance."""
    assert _config().budget.max_trades_ever == 50


async def test_spend_is_derived_from_the_ledger_not_from_a_counter() -> None:
    """A total held in a process resets on restart, silently re-authorising the
    day's budget on every deploy. Rebuilding the service must change nothing."""
    ledger = InMemoryExplorationLedger()
    now = datetime.now(UTC)
    for i in range(5):
        await ledger.append(
            ExplorationRecord(subject=f"m{i}", granted_at=now, notional_usd=1.0)
        )
    verdict = await _service(ledger=ledger).consider(_candidate())
    # $5 of a $5 daily allowance: the dollar ceiling binds first (it is checked
    # before the trade count, and here both are reached at once).
    assert verdict.decline is ExplorationDecline.DAILY_BUDGET

    # A brand-new service over the same ledger reaches the same conclusion.
    assert (await _service(ledger=ledger).consider(_candidate())).decline is (
        ExplorationDecline.DAILY_BUDGET
    )


async def test_yesterdays_spend_does_not_count_against_today() -> None:
    ledger = InMemoryExplorationLedger()
    yesterday = day_start(datetime.now(UTC)) - timedelta(hours=1)
    for i in range(5):
        await ledger.append(
            ExplorationRecord(subject=f"m{i}", granted_at=yesterday, notional_usd=1.0)
        )
    verdict = await _service(ledger=ledger).consider(_candidate())
    assert verdict.granted
    # ...but it still counts against the week and the lifetime totals, which is
    # the whole reason the ceilings are independent.
    spent_week, count = await ledger.spent_since(week_start(datetime.now(UTC)))
    assert count == 5 and spent_week == 5.0


async def test_the_budget_is_charged_on_approval_not_on_grant() -> None:
    """A grant the Risk Manager then vetoes must cost the programme nothing."""
    ledger = InMemoryExplorationLedger()
    service = _service(ledger=ledger)
    verdict = await service.consider(_candidate())
    assert verdict.granted
    assert await ledger.spent_total() == (0.0, 0)

    await service.commit(verdict)
    assert await ledger.spent_total() == (1.0, 1)


async def test_committing_a_decline_charges_nothing() -> None:
    ledger = InMemoryExplorationLedger()
    service = _service(
        evidence=_evidence(lessons=40, positive=20, negative=20), ledger=ledger
    )
    await service.commit(await service.consider(_candidate()))
    assert await ledger.spent_total() == (0.0, 0)


async def test_budget_exhaustion_is_announced_once_per_window() -> None:
    bus = InMemoryEventBus()
    seen: list[DomainEvent] = []

    async def _capture(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(ExplorationBudgetExhausted.__name__, _capture)

    ledger = InMemoryExplorationLedger()
    now = datetime.now(UTC)
    for i in range(5):
        await ledger.append(
            ExplorationRecord(subject=f"m{i}", granted_at=now, notional_usd=1.0)
        )
    service = _service(ledger=ledger, bus=bus)
    for _ in range(4):
        await service.consider(_candidate())
    assert len(seen) == 1
    assert isinstance(seen[0], ExplorationBudgetExhausted)
    assert seen[0].window == "daily"


async def test_a_grant_and_a_charge_are_both_announced() -> None:
    bus = InMemoryEventBus()
    seen: list[DomainEvent] = []

    async def _capture(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(ExplorationGranted.__name__, _capture)
    bus.subscribe(ExplorationSpent.__name__, _capture)

    service = _service(bus=bus)
    verdict = await service.consider(_candidate())
    await service.commit(verdict, correlation_id="corr-1")
    assert [type(e).__name__ for e in seen] == ["ExplorationGranted", "ExplorationSpent"]
    assert isinstance(seen[1], ExplorationSpent)
    assert seen[1].spent_total_usd == 1.0


# =============================================================================
# 4. The evidence census counts ground truth and nothing else
# =============================================================================


async def test_the_census_counts_settled_lessons_and_their_cohorts() -> None:
    lessons = InMemoryLessonStore()
    await lessons.append(
        [
            _lesson(0, positive=True, tags={"developer": "devA"}),
            _lesson(1, positive=False, tags={"developer": "devA"}),
            _lesson(2, positive=False, tags={"developer": "devB", "narrative": "dog"}),
            _lesson(3, positive=False, tags={}),  # unattributable: counts globally only
        ]
    )
    status = await KnowledgeEvidence(lessons, EvidenceTarget()).status(
        cohort_dimensions=("developer", "narrative")
    )
    assert (status.lessons, status.positive, status.negative) == (4, 1, 3)
    assert status.cohorts == {"developer=devA": 2, "developer=devB": 1, "narrative=dog": 1}
    assert status.available is True


async def test_a_broken_lesson_store_reports_unavailable_not_empty() -> None:
    """An empty memory means "keep exploring"; an unreadable one means "stop".
    Collapsing the two is how a broken platform and a young one came to look
    identical for weeks."""

    class _Broken:
        async def append(self, lessons: object) -> None: ...

        async def load(self, *, limit: int = 0, since_iso: str | None = None) -> list[Lesson]:
            raise RuntimeError("database is down")

        async def count(self) -> int:
            return 0

    status = await KnowledgeEvidence(_Broken(), EvidenceTarget()).status(
        cohort_dimensions=("developer",)
    )
    assert status.available is False
    assert status.sufficient is False  # unavailable is never "we are done"


# =============================================================================
# 5. Wired into the Risk Manager: it waives conviction, never safety
# =============================================================================


def _risk_token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="T")


def _risk_candidate(**over: object) -> RiskCandidate:
    base: dict[str, object] = {
        "token": _risk_token(),
        "at": datetime.now(UTC),
        # Below the production floor of 0.55, inside the exploration band.
        "prob_roi_positive": _UNCERTAIN,
        "prob_hit_tp": 0.4,
        "prob_hit_sl": 0.4,
        "confidence": 0.35,
        "security_score": 90.0,
        "developer_score": 80.0,
        "wallet_risk_score": 20.0,
        "liquidity_score": 85.0,
        "liquidity_usd": 50_000.0,
        "regime": "bull",
        "strategy": "momentum",
        "developer": "devA",
    }
    base.update(over)
    return RiskCandidate(**base)  # type: ignore[arg-type]


def _risk_state(equity: float = 1000.0) -> PortfolioRiskState:
    return PortfolioRiskState(
        capital=CapitalSnapshot(
            equity_usd=equity, cash_usd=equity, invested_usd=0.0, reserve_pct=0.0
        ),
        drawdown=DrawdownSnapshot(peak_equity_usd=equity, current_equity_usd=equity),
    )


def _risk(service: ExplorationService | None = None):
    bus = InMemoryEventBus()
    events: list[DomainEvent] = []

    async def _capture(event: DomainEvent) -> None:
        events.append(event)

    bus.subscribe(TradeApproved.__name__, _capture)
    bus.subscribe(TradeRejected.__name__, _capture)
    manager = build_risk_manager(
        RiskConfig(sizing=SizingConfig(max_position_size_usd=1000.0)),
        exploration=ExplorationAdapter(service) if service is not None else None,
        event_bus=bus,
        audit=InMemoryRiskAuditStore(),
        state_store=InMemoryRiskStateStore(),
    )
    return manager, events


async def test_without_exploration_the_chain_is_exactly_what_it_was() -> None:
    """The regression that would matter most: exploration must be additive. A
    candidate the conviction gate muted is still rejected, with the same cause."""
    manager, events = _risk()
    result = await manager.evaluate(_risk_candidate(), _risk_state())
    assert result.decision is RiskDecision.REJECT
    assert result.reject_reason is RiskRejectReason.LOW_PROBABILITY
    assert isinstance(events[0], TradeRejected)


async def test_a_disabled_programme_changes_nothing_either() -> None:
    manager, _events = _risk(_service(_config(enabled=False)))
    result = await manager.evaluate(_risk_candidate(), _risk_state())
    assert result.decision is RiskDecision.REJECT
    assert result.reject_reason is RiskRejectReason.LOW_PROBABILITY


async def test_a_grant_turns_the_same_rejection_into_a_tiny_sample() -> None:
    ledger = InMemoryExplorationLedger()
    manager, events = _risk(_service(ledger=ledger))
    result = await manager.evaluate(_risk_candidate(), _risk_state())

    assert result.decision is RiskDecision.APPROVE
    assert result.is_exploration
    assert result.sizing is not None
    assert float(result.sizing.notional.amount) == 1.0  # the fixed sample size
    assert result.conviction == 0.0  # not conviction-weighted, by construction
    # The budget is charged only now, on approval.
    assert await ledger.spent_total() == (1.0, 1)

    approval = events[0]
    assert isinstance(approval, TradeApproved)
    assert approval.exploration is True


async def test_an_exploration_approval_never_reads_like_a_conviction_one() -> None:
    manager, _events = _risk(_service())
    result = await manager.evaluate(_risk_candidate(), _risk_state())
    assert "EXPLORATION" in result.headline
    assert result.exploration is not None
    assert result.exploration.waived_policy == "min_probability"
    caveats = " ".join(result.caveats)
    assert "generate evidence" in caveats
    assert "not for expected return" in caveats
    assert "budget" in caveats


async def test_the_exploration_envelope_is_tight_and_has_no_trailing_stop() -> None:
    """A trailing exit makes the realised return depend on the exit rule as well
    as the path, which muddies the one thing the sample was bought for."""
    manager, _events = _risk(_service())
    result = await manager.evaluate(_risk_candidate(), _risk_state())
    assert result.sizing is not None
    assert result.sizing.stop_loss.value == 12.0
    assert result.sizing.trailing_enabled is False


async def test_safety_rules_are_never_waived() -> None:
    """The property the whole design exists to protect. Each of these candidates
    is inside the exploration band and would be granted on conviction grounds —
    and each must still be rejected, by its own rule, with the programme active."""
    cases = [
        ({"security_approved": False}, RiskRejectReason.SECURITY_REJECTED),
        ({"security_score": 10.0}, RiskRejectReason.LOW_SECURITY_SCORE),
        ({"developer_score": 5.0}, RiskRejectReason.DEVELOPER_RISKY),
        ({"wallet_risk_score": 99.0}, RiskRejectReason.WALLET_SUSPICIOUS),
        ({"liquidity_usd": 100.0}, RiskRejectReason.LIQUIDITY_INSUFFICIENT),
    ]
    for override, expected in cases:
        ledger = InMemoryExplorationLedger()
        manager, _events = _risk(_service(ledger=ledger))
        result = await manager.evaluate(_risk_candidate(**override), _risk_state())
        assert result.decision is RiskDecision.REJECT, override
        assert result.reject_reason is expected, override
        assert not result.is_exploration
        # And the programme was never even asked, so nothing was charged.
        assert await ledger.spent_total() == (0.0, 0)


async def test_the_defence_layer_is_never_waived() -> None:
    """An exploration trade under an open circuit breaker is still a trade, and
    the reason the breaker exists does not care how small it is."""
    from hades.contexts.risk.domain.models import CircuitBreakerReason

    manager, _events = _risk(_service())
    await manager.trip_circuit_breaker(CircuitBreakerReason.RPC_UNSTABLE, "test")
    result = await manager.evaluate(_risk_candidate(), _risk_state())
    assert result.reject_reason is RiskRejectReason.CIRCUIT_BREAKER_OPEN

    manager2, _e2 = _risk(_service())
    await manager2.enter_emergency("test")
    result2 = await manager2.evaluate(_risk_candidate(), _risk_state())
    assert result2.reject_reason is RiskRejectReason.EMERGENCY_MODE


async def test_allocation_rules_still_bind_and_the_veto_costs_nothing() -> None:
    """The book's limits apply to a cheap trade exactly as to an expensive one —
    and a grant vetoed after the fact must not be charged."""
    ledger = InMemoryExplorationLedger()
    manager, _events = _risk(_service(ledger=ledger))
    broke = PortfolioRiskState(
        capital=CapitalSnapshot(
            equity_usd=100.0, cash_usd=0.0, invested_usd=100.0, reserve_pct=0.0
        ),
        drawdown=DrawdownSnapshot(peak_equity_usd=100.0, current_equity_usd=100.0),
    )
    result = await manager.evaluate(_risk_candidate(), broke)
    assert result.decision is RiskDecision.REJECT
    assert await ledger.spent_total() == (0.0, 0)


async def test_a_broken_programme_cannot_manufacture_an_approval() -> None:
    """An optional collaborator's failure may cost a sample. It may never cost
    the guardian's correctness, and it may certainly never produce a trade."""

    class _Exploding:
        async def consider(self, candidate: object, *, waived_policy: str) -> None:
            raise RuntimeError("exploration is broken")

        async def commit(self, candidate: object, grant: object) -> None:
            raise RuntimeError("exploration is broken")

    manager = build_risk_manager(
        RiskConfig(),
        exploration=_Exploding(),
        event_bus=InMemoryEventBus(),
        audit=InMemoryRiskAuditStore(),
    )
    result = await manager.evaluate(_risk_candidate(), _risk_state())
    assert result.decision is RiskDecision.REJECT
    assert result.reject_reason is RiskRejectReason.LOW_PROBABILITY


async def test_exploration_approvals_are_counted_apart_from_the_rest() -> None:
    """Folding them into the headline count would let a programme of deliberate
    small losses read as a strategy performing badly."""
    manager, _events = _risk(_service())
    await manager.evaluate(_risk_candidate(), _risk_state())
    snapshot = manager.snapshot(_risk_state())
    assert snapshot.approvals_session == 1
    assert snapshot.exploration_approvals_session == 1


# =============================================================================
# 6. Configuration reaches the policy
# =============================================================================


def test_every_setting_lands_in_the_policy() -> None:
    settings = Settings()
    settings.exploration.enabled = True
    settings.exploration.per_trade_usd = 2.5
    settings.exploration.total_budget_usd = 500.0
    settings.exploration.target_lessons = 111
    settings.exploration.target_per_class = 33
    settings.exploration.stop_loss_pct = 7.5

    config = exploration_config_from_settings(settings)
    assert config.enabled is True
    assert config.budget.per_trade_usd == 2.5
    assert config.budget.total_usd == 500.0
    assert config.target.min_lessons == 111
    assert config.target.min_per_class == 33
    assert config.stop_loss_pct == 7.5
    assert config.budget.max_trades_ever == 200


def test_exploration_is_off_until_an_operator_says_otherwise() -> None:
    assert Settings().exploration.enabled is False
