"""Committee handler — runs the brain when a token's analysis completes.

The AI Committee sits at the end of the analytical pipeline:

    scanner → features → security → intelligence → **committee**

It subscribes to Wallet Intelligence's ``WalletIntelligenceComputed`` — the last
stage, fired for every fully-analysed token (approved *and* rejected). On each
event the :class:`DecisionContextBuilder` assembles the full context (the
normalised feature vector joined to the security verdict and the wallet snapshot)
and the :class:`CommitteeManager` produces the auditable prediction.

Reacting to an event is the sanctioned cross-context coupling — the committee
never calls upstream contexts. And it never trades: it emits probabilities and an
explanation for a later Risk Manager to consider.
"""

from __future__ import annotations

from hades.contexts.intelligence.domain.events import WalletIntelligenceComputed
from hades.contexts.learning.application.committee.manager import CommitteeManager
from hades.contexts.learning.domain.ports import CandidateEnricher, DecisionContextBuilder
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.subscriber")


class CommitteeHandler:
    """Runs the committee whenever a token's wallet-intelligence completes.

    The enricher is a **required** constructor argument, not an optional
    collaborator: the pipeline is build → enrich → evaluate, and there is no
    branch through this class that reaches the committee without the middle
    step. Combined with ``CommitteeManager.evaluate`` accepting only an
    :class:`EnrichedCandidate`, that makes "no token is judged from scratch" a
    property of the code rather than of anyone's discipline.
    """

    def __init__(
        self,
        manager: CommitteeManager,
        builder: DecisionContextBuilder,
        enricher: CandidateEnricher,
    ) -> None:
        self._manager = manager
        self._builder = builder
        self._enricher = enricher

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(WalletIntelligenceComputed.__name__, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, WalletIntelligenceComputed):
            return
        try:
            context = await self._builder.build(event)
            if context is None:
                _logger.debug("committee_no_context", mint=str(event.token.mint))
                return
            candidate = await self._enricher.enrich(context)
            await self._manager.evaluate(candidate)
        except Exception as exc:  # never let one token break the subscriber
            _logger.warning("committee_skipped", mint=str(event.token.mint), error=str(exc))
