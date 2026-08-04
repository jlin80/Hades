"""Risk context builder - turns a committee prediction into a RiskCandidate.

The AI Committee already distilled the upstream analysis into per-member
opinions (security, developer, wallet, liquidity...) and three fused
probabilities. The builder reads those directly - self-contained, no extra I/O -
and joins any additional facts from an optional :class:`RiskFactsPort` (developer
address, cluster, narrative, raw liquidity) so the Exposure and Correlation
engines have keys to aggregate on. Everything degrades to a neutral default when
a signal is absent: missing data lowers conviction, it is never an error.
"""

from __future__ import annotations

from typing import Any

from hades.contexts.learning.domain.events import CommitteePredictionGenerated
from hades.contexts.risk.domain.models import RiskCandidate
from hades.contexts.risk.domain.ports import RiskFactsPort


class NullRiskFactsPort:
    """A facts port that knows nothing - the builder then uses opinions only."""

    async def facts_for(self, mint: str) -> dict[str, float | bool | str | None]:
        return {}


class RiskContextBuilder:
    """Assembles a :class:`RiskCandidate` from a committee prediction event."""

    def __init__(self, facts: RiskFactsPort | None = None) -> None:
        self._facts = facts or NullRiskFactsPort()

    async def build(self, event: CommitteePredictionGenerated) -> RiskCandidate:
        prediction = event.prediction
        facts = await self._safe_facts(str(event.token.mint))
        opinions = prediction.opinion_of

        def member_score(member: str, default: float) -> float:
            op = opinions.get(member)
            return op.score if op is not None else default

        wallet_health = member_score("wallet", 50.0)
        headline = prediction.explanation.headline if prediction.explanation else ""

        return RiskCandidate(
            token=event.token,
            at=prediction.at,
            prob_roi_positive=prediction.meta.prob_roi_positive,
            prob_hit_tp=prediction.meta.prob_hit_tp,
            prob_hit_sl=prediction.meta.prob_hit_sl,
            confidence=prediction.confidence.final,
            regime=prediction.regime.regime.value,
            committee_headline=headline,
            security_approved=_as_bool(facts.get("security_approved"), default=True),
            security_score=_as_float(facts.get("security_score"), member_score("security", 50.0)),
            developer_score=_as_float(
                facts.get("developer_score"), member_score("developer", 50.0)
            ),
            wallet_risk_score=_as_float(facts.get("wallet_risk"), 100.0 - wallet_health),
            liquidity_score=member_score("liquidity", 50.0),
            liquidity_usd=_as_float(facts.get("liquidity_usd"), 0.0),
            ensemble_decision=_as_str(facts.get("ensemble_decision"), "unknown"),
            ensemble_score=_as_float(facts.get("ensemble_score"), 0.0),
            ensemble_confidence=_as_float(facts.get("ensemble_confidence"), 0.0),
            ensemble_participating=int(_as_float(facts.get("ensemble_participating"), 0.0)),
            strategy=_as_str(facts.get("strategy"), "default"),
            developer=_as_opt_str(facts.get("developer")),
            cluster=_as_opt_str(facts.get("cluster")),
            narrative=_as_opt_str(facts.get("narrative")),
            correlation_id=prediction.correlation_id or _as_opt_str(event.correlation_id),
        )

    async def _safe_facts(self, mint: str) -> dict[str, Any]:
        try:
            return dict(await self._facts.facts_for(mint))
        except Exception:  # facts are optional - never fail the build on them
            return {}


def _as_float(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _as_bool(value: object, *, default: bool) -> bool:
    return bool(value) if isinstance(value, bool) else default


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _as_opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
