"""The Risk Manager's edge onto the Exploration programme.

This adapter implements :class:`ExplorationPort` — a protocol the Risk Manager
declares, in the Risk Manager's own vocabulary — on top of the Exploration
context's service. It is the only file in the risk context that knows exploration
exists, and it is the reason the two contexts share no types: past this
translation, nothing in ``contexts/risk`` refers to an exploration model, and
nothing in ``contexts/exploration`` refers to a risk one.

The direction is Risk → Exploration, and it is one-way by construction:
exploration cannot call the guardian back, because it holds no reference to it
and its isolation test forbids the import. That is what keeps "the Risk Manager
is the only authoriser" true in the presence of a collaborator that exists to
argue for trades.

Note what is *not* translated. The candidate handed across carries a probability,
a confidence and its cohort tags — nothing else. Exploration is not given the
security verdict, the portfolio state or the sizing, because those are inputs to
rules it does not get to waive, and a richer type would invite a rule over there
that quietly did.
"""

from __future__ import annotations

from hades.contexts.exploration.application.service import ExplorationService
from hades.contexts.exploration.domain.models import ExplorationCandidate, ExplorationVerdict
from hades.contexts.risk.domain.models import ExplorationGrant, RiskCandidate
from hades.shared_kernel.logging import get_logger

_logger = get_logger("risk.exploration")


class ExplorationAdapter:
    """Translates between the guardian's candidate and the programme's verdict."""

    def __init__(self, service: ExplorationService) -> None:
        self._service = service

    async def consider(
        self, candidate: RiskCandidate, *, waived_policy: str
    ) -> ExplorationGrant | None:
        verdict = await self._service.consider(_to_exploration(candidate))
        if not verdict.granted:
            return None
        return ExplorationGrant(
            notional_usd=verdict.notional_usd,
            stop_loss_pct=verdict.stop_loss_pct,
            take_profit_pct=verdict.take_profit_pct,
            waived_policy=waived_policy,
            cohort_key=verdict.cohort_key,
            cohort_count=verdict.cohort_count,
            evidence_summary=verdict.evidence_summary,
            budget_summary=verdict.budget_summary,
            reasons=verdict.reasons,
        )

    async def commit(self, candidate: RiskCandidate, grant: ExplorationGrant) -> None:
        # The grant is reconstructed rather than carried because the Risk Manager
        # holds the risk-side type and the service accounts in its own. Only the
        # three fields the ledger records are involved, so there is nothing here
        # that could drift into re-deciding what was already decided.
        await self._service.commit(
            _to_verdict(candidate, grant), correlation_id=candidate.correlation_id
        )
        _logger.info(
            "exploration_trade_approved",
            mint=str(candidate.token.mint),
            notional_usd=grant.notional_usd,
            cohort=grant.cohort_key,
        )


def _to_exploration(candidate: RiskCandidate) -> ExplorationCandidate:
    """The narrow view exploration is allowed to reason over."""
    cohorts = {
        key: value
        for key, value in (
            ("developer", candidate.developer),
            ("cluster", candidate.cluster),
            ("narrative", candidate.narrative),
            # The launchpad is not on a RiskCandidate — the guardian judges a
            # token, not its provenance — so that dimension simply contributes
            # nothing here rather than being guessed. An absent cohort is a
            # cohort that cannot justify a trade, which is the safe direction.
        )
        if value
    }
    return ExplorationCandidate(
        subject=str(candidate.token.mint),
        prob_roi_positive=candidate.prob_roi_positive,
        confidence=candidate.confidence,
        cohorts=cohorts,
        correlation_id=candidate.correlation_id,
    )


def _to_verdict(candidate: RiskCandidate, grant: ExplorationGrant) -> ExplorationVerdict:
    # ``at`` is left unset on purpose: the ledger records when the budget was
    # *charged*, which is now, not when the committee looked at the token. Using
    # the candidate's timestamp would file a spend under the wrong day whenever a
    # decision straddled midnight, and the daily budget would be quietly wrong.
    return ExplorationVerdict(
        granted=True,
        subject=str(candidate.token.mint),
        notional_usd=grant.notional_usd,
        stop_loss_pct=grant.stop_loss_pct,
        take_profit_pct=grant.take_profit_pct,
        cohort_key=grant.cohort_key,
        cohort_count=grant.cohort_count,
        reasons=grant.reasons,
        evidence_summary=grant.evidence_summary,
        budget_summary=grant.budget_summary,
    )


__all__ = ["ExplorationAdapter"]
