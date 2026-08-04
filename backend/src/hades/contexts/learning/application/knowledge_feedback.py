"""Knowledge Feedback — every outcome flows back into the brain's memory.

The spec insists the system learn from *everything*, not only its wins: executed
trades (with realised ROI/TP/SL), **rejected opportunities**, missed moves,
regime changes and errors. This service is the write-path into the append-only
:class:`OutcomeStore` the Dataset Builder later reads.

Two entry points:

    * :meth:`record_outcome` — a closed trade's realised labels. Called by the
      composition root when the Knowledge context announces a completed
      ``LessonLearned``: a decision married to its outcome, carrying the feature
      vector **as it stood when the decision was taken**.

      This used to be unreachable. The docstring said the execution/portfolio
      context would call it "in a later phase"; that phase never arrived, so the
      ledger only ever accumulated the weak negatives below. A single-class
      dataset has an undefined AUC, so ``ValidationEngine``'s ``min_auc`` gate
      could never be met, no candidate was ever validated, and the committee
      stayed pinned to its default priors — the real cold-start lock, not the
      probability threshold it was long mistaken for.
    * :meth:`on_token_rejected` — reacts to the Security Engine rejecting a token
      and records it as a (weakly-labelled) negative example, so the committee
      also learns from what the platform declined.

It writes memory; it never trades or decides.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.features.domain.ports import FeatureStore
from hades.contexts.learning.application.feature_catalog import FeatureNormalizer
from hades.contexts.learning.domain.models import TrainingSample
from hades.contexts.learning.domain.ports import OutcomeStore
from hades.contexts.security.domain.events import TokenRejected
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.feedback")


class KnowledgeFeedback:
    """Turns realised outcomes and rejections into training memory."""

    def __init__(
        self,
        outcomes: OutcomeStore,
        feature_store: FeatureStore,
        normalizer: FeatureNormalizer,
        *,
        rejection_weight: float = 0.5,
    ) -> None:
        self._outcomes = outcomes
        self._features = feature_store
        self._normalizer = normalizer
        self._rejection_weight = rejection_weight

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(TokenRejected.__name__, self.on_token_rejected)

    async def record_outcome(
        self,
        *,
        token_mint: str,
        features: dict[str, float],
        realized_roi: float,
        hit_tp: bool,
        hit_sl: bool,
        at: datetime | None = None,
    ) -> None:
        """Record a closed trade's realised labels (learn from wins *and* losses)."""
        sample = TrainingSample(
            token_mint=token_mint,
            at=at or datetime.now(UTC),
            features=features,
            label_roi_positive=realized_roi > 0.0,
            label_hit_tp=hit_tp,
            label_hit_sl=hit_sl,
            realized_roi=realized_roi,
            was_executed=True,
            was_rejected=False,
            weight=1.0,
        )
        await self._outcomes.append([sample])

    async def on_token_rejected(self, event: DomainEvent) -> None:
        """Record a security-rejected token as a weak negative example."""
        if not isinstance(event, TokenRejected):
            return
        try:
            feature_set = await self._features.latest(event.token)
            if feature_set is None:
                return
            vector = self._normalizer.normalize(feature_set)
            values = dict(vector.values)
            values["security.score"] = max(0.0, min(1.0, event.score / 100.0))
            values["security.approved"] = 0.0
            sample = TrainingSample(
                token_mint=str(event.token.mint),
                at=datetime.now(UTC),
                features=values,
                label_roi_positive=False,
                label_hit_tp=False,
                label_hit_sl=True,
                realized_roi=0.0,
                was_executed=False,
                was_rejected=True,
                weight=self._rejection_weight,
            )
            await self._outcomes.append([sample])
        except Exception as exc:  # feedback is best-effort, never breaks the bus
            _logger.warning("rejection_feedback_failed", mint=str(event.token.mint), error=str(exc))
