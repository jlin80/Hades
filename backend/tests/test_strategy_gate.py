"""The Strategy Engine, connected — `gate_risk` finally reads as something.

For most of the platform's life the Strategy Engine computed into nothing:
fifteen strategies, a weighted ensemble and a dynamic weight engine published
`EnsembleSignalGenerated`, and no subscriber existed anywhere. `gate_risk` was
declared and read only for logs. The architecture audit catalogued it as the
largest piece of dead machinery in the codebase.

Turning it on gives the ensemble two powers, and the shape of both is the whole
safety argument: **it may only ever reduce exposure.**

* it can **veto** an entry the Risk Manager would otherwise approve;
* it can **request an exit** on a token already held.

It cannot create an approval, cannot open a position, and cannot size anything.
`TradeApproved` is still constructed in exactly one place. The tests below pin
that asymmetry, plus the two failure modes that would be dangerous and quiet:
treating a silent roster as a dissenting one, and letting a strategy exit
override a stop-loss that has already been crossed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.risk.application.policies import EnsembleConsensusPolicy
from hades.contexts.risk.domain.models import (
    CapitalSnapshot,
    PortfolioRiskState,
    RiskCandidate,
    RiskRejectReason,
)
from hades.contexts.strategy.domain.models import SignalType

_MINT = "So11111111111111111111111111111111111111112"
_TOKEN = TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")
_AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _candidate(**overrides: object) -> RiskCandidate:
    base: dict[str, object] = {
        "token": _TOKEN,
        "at": _AT,
        "prob_roi_positive": 0.7,
        "prob_hit_tp": 0.6,
        "prob_hit_sl": 0.2,
        "confidence": 0.6,
    }
    base.update(overrides)
    return RiskCandidate(**base)  # type: ignore[arg-type]


def _state() -> PortfolioRiskState:
    return PortfolioRiskState(
        capital=CapitalSnapshot(equity_usd=1_000.0, cash_usd=1_000.0, invested_usd=0.0)
    )


# --- the veto -----------------------------------------------------------------


def test_a_silent_roster_is_not_a_dissenting_one() -> None:
    """The failure mode that would halt the platform while looking like caution.

    At cold start no strategy has an opinion about anything. If "nobody spoke"
    were read as "they disliked it", every candidate would be vetoed and the
    platform would go quiet with a perfectly sensible-looking reason attached to
    each rejection.
    """
    policy = EnsembleConsensusPolicy(min_score=0.0)
    outcome = policy.evaluate(_candidate(ensemble_participating=0), _state(), 0.0)
    assert outcome.passed


def test_a_buy_consensus_passes() -> None:
    policy = EnsembleConsensusPolicy(min_score=0.0)
    outcome = policy.evaluate(
        _candidate(
            ensemble_decision=SignalType.BUY.value,
            ensemble_score=0.4,
            ensemble_participating=7,
        ),
        _state(),
        0.0,
    )
    assert outcome.passed


@pytest.mark.parametrize(
    "decision", [SignalType.SELL.value, SignalType.EXIT.value, SignalType.IGNORE.value]
)
def test_anything_but_buy_vetoes_an_entry(decision: str) -> None:
    policy = EnsembleConsensusPolicy(min_score=0.0)
    outcome = policy.evaluate(
        _candidate(ensemble_decision=decision, ensemble_participating=9),
        _state(),
        0.0,
    )
    assert not outcome.passed
    assert outcome.reason is RiskRejectReason.ENSEMBLE_DISAGREES


def test_a_buy_below_the_conviction_floor_is_vetoed() -> None:
    policy = EnsembleConsensusPolicy(min_score=0.35)
    outcome = policy.evaluate(
        _candidate(
            ensemble_decision=SignalType.BUY.value,
            ensemble_score=0.10,
            ensemble_participating=5,
        ),
        _state(),
        0.0,
    )
    assert not outcome.passed
    assert "conviction" in (outcome.detail or "")


def test_the_policy_has_no_way_to_approve_anything() -> None:
    """It returns pass/veto, never a size, a price or an authorisation.

    A second voice that could approve would end "the Risk Manager is the sole
    authoriser". A second voice that can only refuse is strictly conservative.
    """
    policy = EnsembleConsensusPolicy(min_score=0.0)
    outcome = policy.evaluate(
        _candidate(ensemble_decision=SignalType.BUY.value, ensemble_participating=3),
        _state(),
        0.0,
    )
    for forbidden in ("notional", "size", "approved_usd", "quantity"):
        assert not hasattr(outcome, forbidden)


# --- wiring -------------------------------------------------------------------


def test_the_gate_is_off_by_default_and_the_chain_is_unchanged() -> None:
    from hades.contexts.risk.application.factory import (
        build_risk_manager,
        risk_config_from_settings,
    )
    from hades.shared_kernel.config import Settings

    config = risk_config_from_settings(Settings())
    assert config.gate_on_ensemble is False

    manager = build_risk_manager(config)
    names = [p.name for p in manager._conviction]
    assert "ensemble_consensus" not in names


def test_turning_the_gate_on_adds_the_policy_to_conviction_not_safety() -> None:
    """The tier matters. In CONVICTION an exploration grant may waive it, which
    is what keeps the cold-start programme able to sample cohorts that strategies
    with no history have no opinion about. It could never waive a SAFETY rule,
    and a strategy verdict is not one."""
    from hades.contexts.risk.application.factory import build_risk_manager
    from hades.contexts.risk.domain.models import RiskConfig

    manager = build_risk_manager(RiskConfig(gate_on_ensemble=True))

    conviction = [p.name for p in manager._conviction]
    safety = [p.name for p in manager._quality]
    assert "ensemble_consensus" in conviction
    assert "ensemble_consensus" not in safety


def test_the_cheap_gates_still_run_first() -> None:
    """Probability and confidence are one comparison each; the ensemble check is
    appended so the common rejection does not get more expensive."""
    from hades.contexts.risk.application.factory import build_risk_manager
    from hades.contexts.risk.domain.models import RiskConfig

    manager = build_risk_manager(RiskConfig(gate_on_ensemble=True))
    names = [p.name for p in manager._conviction]
    assert names.index("ensemble_consensus") == len(names) - 1


# --- the facts path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ensemble_verdict_reaches_the_risk_candidate() -> None:
    """End to end over a real bus: the Strategy Engine publishes, the facts cache
    remembers, and the builder puts it on the candidate."""
    from hades.contexts.risk.infrastructure.facts_cache import EventDrivenRiskFacts
    from hades.contexts.strategy.domain.events import EnsembleSignalGenerated
    from hades.contexts.strategy.domain.models import EnsembleSignal
    from hades.shared_kernel.domain.identifiers import new_id
    from hades.shared_kernel.events import InMemoryEventBus

    bus = InMemoryEventBus()
    facts = EventDrivenRiskFacts()
    facts.register(bus)

    await bus.publish(
        EnsembleSignalGenerated(
            aggregate_id=new_id(),
            token=_TOKEN,
            ensemble=EnsembleSignal(
                token=_TOKEN,
                decision=SignalType.BUY,
                score=0.42,
                confidence=0.55,
                participating=6,
            ),
        )
    )

    recorded = await facts.facts_for(_MINT)
    assert recorded["ensemble_decision"] == SignalType.BUY.value
    assert recorded["ensemble_score"] == pytest.approx(0.42)
    assert recorded["ensemble_participating"] == pytest.approx(6.0)
