"""The risk facts cache feeds the Risk Manager real upstream evidence.

Before it existed the builder always used ``NullRiskFactsPort``, so a candidate
was judged on the committee's own probabilities: a security-rejected token
arrived looking approved, and an abstaining specialist's flat 0.5 was read as a
security score of 50.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.common.domain.value_objects import Score, TokenMint, TokenRef
from hades.contexts.intelligence.domain.events import WalletIntelligenceComputed
from hades.contexts.intelligence.domain.models import Cluster, IntelligenceSnapshot
from hades.contexts.risk.domain.ports import RiskFactsPort
from hades.contexts.risk.infrastructure.facts_cache import EventDrivenRiskFacts
from hades.contexts.security.domain.events import SecurityScoreComputed
from hades.contexts.security.domain.models import SecurityAssessment, SecurityDecision
from hades.shared_kernel.domain.identifiers import new_id

_MINT = "M" * 44
_OTHER_MINT = "N" * 44
_DEPLOYER = "D" * 44


def _token(mint: str = _MINT) -> TokenRef:
    return TokenRef(mint=TokenMint(address=mint))


def _security(
    *,
    mint: str = _MINT,
    approved: bool = True,
    vetoed: bool = False,
    score: float = 82.0,
    developer_score: float = 71.0,
    liquidity_usd: float | None = 42_000.0,
) -> SecurityScoreComputed:
    facts: dict[str, object] = {"developer": {"deployer": _DEPLOYER}}
    if liquidity_usd is not None:
        facts["liquidity"] = {"liquidity_usd": liquidity_usd, "dex": "raydium"}
    assessment = SecurityAssessment(
        token=_token(mint),
        decision=SecurityDecision.APPROVED if approved else SecurityDecision.REJECTED,
        score=Score(value=score),
        sub_scores={"developer": developer_score, "liquidity": 66.0},
        facts=facts,
        analyzed_at=datetime.now(UTC),
    )
    return SecurityScoreComputed(
        aggregate_id=new_id(),
        token=assessment.token,
        assessment=assessment,
        vetoed=vetoed,
    )


def _intelligence(
    *, mint: str = _MINT, avg_risk: float = 23.0, clusters: tuple[Cluster, ...] = ()
) -> WalletIntelligenceComputed:
    return WalletIntelligenceComputed(
        aggregate_id=new_id(),
        token=_token(mint),
        snapshot=IntelligenceSnapshot(token=_token(mint), avg_risk=avg_risk, clusters=clusters),
    )


def test_it_satisfies_the_port() -> None:
    assert isinstance(EventDrivenRiskFacts(), RiskFactsPort)


async def test_it_records_the_real_security_verdict() -> None:
    cache = EventDrivenRiskFacts()
    await cache.on_security(_security())

    facts = await cache.facts_for(_MINT)

    assert facts["security_approved"] is True
    assert facts["security_score"] == 82.0
    assert facts["developer_score"] == 71.0
    assert facts["liquidity_usd"] == 42_000.0
    assert facts["developer"] == _DEPLOYER


async def test_a_rejected_token_is_reported_as_not_approved() -> None:
    """The whole point: rejection must survive the trip to the Risk Manager."""
    cache = EventDrivenRiskFacts()
    await cache.on_security(_security(approved=False))

    assert (await cache.facts_for(_MINT))["security_approved"] is False


async def test_a_veto_overrides_an_approved_decision() -> None:
    cache = EventDrivenRiskFacts()
    await cache.on_security(_security(approved=True, vetoed=True))

    assert (await cache.facts_for(_MINT))["security_approved"] is False


async def test_wallet_risk_comes_from_the_intelligence_snapshot() -> None:
    cache = EventDrivenRiskFacts()
    await cache.on_intelligence(_intelligence(avg_risk=64.0))

    assert (await cache.facts_for(_MINT))["wallet_risk"] == 64.0


async def test_the_dominant_cluster_becomes_the_correlation_key() -> None:
    cache = EventDrivenRiskFacts()
    clusters = (
        Cluster(cluster_id="small", members=("a", "b"), pct_supply=3.0),
        Cluster(cluster_id="dominant", members=("c", "d"), pct_supply=31.0),
    )
    await cache.on_intelligence(_intelligence(clusters=clusters))

    assert (await cache.facts_for(_MINT))["cluster"] == "dominant"


async def test_security_and_intelligence_facts_merge_for_one_token() -> None:
    cache = EventDrivenRiskFacts()
    await cache.on_security(_security())
    await cache.on_intelligence(_intelligence(avg_risk=15.0))

    facts = await cache.facts_for(_MINT)

    assert facts["security_score"] == 82.0
    assert facts["wallet_risk"] == 15.0


async def test_facts_are_kept_per_token() -> None:
    cache = EventDrivenRiskFacts()
    await cache.on_security(_security(mint=_MINT, score=90.0))
    await cache.on_security(_security(mint=_OTHER_MINT, score=10.0))

    assert (await cache.facts_for(_MINT))["security_score"] == 90.0
    assert (await cache.facts_for(_OTHER_MINT))["security_score"] == 10.0


async def test_an_unknown_token_yields_no_facts() -> None:
    cache = EventDrivenRiskFacts()

    assert await cache.facts_for(_MINT) == {}


async def test_a_missing_liquidity_fact_is_simply_absent() -> None:
    """Absent must stay absent — the liquidity policy reads 0 as "unknown"."""
    cache = EventDrivenRiskFacts()
    await cache.on_security(_security(liquidity_usd=None))

    assert "liquidity_usd" not in await cache.facts_for(_MINT)


async def test_expired_facts_degrade_to_unknown() -> None:
    cache = EventDrivenRiskFacts(ttl_seconds=0.0001)
    await cache.on_security(_security())

    import asyncio

    await asyncio.sleep(0.01)

    assert await cache.facts_for(_MINT) == {}


async def test_the_cache_is_bounded_and_evicts_the_oldest() -> None:
    cache = EventDrivenRiskFacts(max_entries=2)
    await cache.on_security(_security(mint="A" * 44))
    await cache.on_security(_security(mint="B" * 44))
    await cache.on_security(_security(mint="C" * 44))

    assert cache.tracked == 2
    assert await cache.facts_for("A" * 44) == {}
    assert await cache.facts_for("C" * 44) != {}


async def test_an_unrelated_event_is_ignored() -> None:
    cache = EventDrivenRiskFacts()
    await cache.on_security(_intelligence())  # type: ignore[arg-type]

    assert cache.tracked == 0
