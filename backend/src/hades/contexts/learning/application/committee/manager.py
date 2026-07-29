"""Committee Manager — orchestrates the specialists, the meta-model and the rest.

For one token's :class:`DecisionContext` it:

    1. Augments the normalised feature vector with the ``security.*`` / ``intel.*``
       context features.
    2. Asks every specialist for its :class:`Opinion` (each stays pure).
    3. Classifies the market regime.
    4. Fuses the opinions with the Meta Model into three probabilities.
    5. Computes the multi-factor confidence and builds the explanation.
    6. Packages a :class:`CommitteePrediction`, persists it, and emits the events.

It also runs any **shadow committees** alongside the active one — they produce
predictions that are persisted for later comparison but flagged ``shadow`` and
never influence anything. The whole class is orchestration: it takes no trade
decision and enables nothing. A run never crashes the caller — failures are
metered and re-raised only to the subscriber's guard.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from hades.contexts.learning.application.committee.base import SpecialistModel
from hades.contexts.learning.application.committee.context_features import context_feature_values
from hades.contexts.learning.application.committee.history_features import history_feature_values
from hades.contexts.learning.application.confidence import ConfidenceEngine
from hades.contexts.learning.application.explainability import ExplanationBuilder
from hades.contexts.learning.application.meta_model import MetaModel
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.regime import MarketRegimeClassifier
from hades.contexts.learning.domain.events import (
    CommitteeFinished,
    CommitteePredictionGenerated,
    ConfidenceCalculated,
    InferenceCompleted,
    PredictionGenerated,
)
from hades.contexts.learning.domain.models import (
    META_MODEL_NAME,
    CandidateEnrichment,
    CommitteePrediction,
    EnrichedCandidate,
    MarketRegime,
    NormalizedVector,
    Opinion,
    RegimeAssessment,
)
from hades.contexts.learning.domain.ports import PredictionStore
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.manager")


@dataclass(frozen=True)
class QualitySignals:
    """Data-quality inputs to the confidence engine, set by the runtime.

    ``dataset_quality`` reflects how good the active model's training data was
    (e.g. its validation AUC); ``sample_support`` reflects how many similar
    historical examples exist. Both live outside any single token's inputs — the
    spec's requirement that confidence not depend only on the model output.
    """

    dataset_quality: float = 0.5
    sample_support: float = 0.5


@dataclass(frozen=True)
class Committee:
    """One committee configuration: its specialists and its meta-model."""

    specialists: tuple[SpecialistModel, ...]
    meta: MetaModel
    label: str = "active"


class CommitteeManager:
    """Runs the active committee (and any shadows) over a decision context."""

    def __init__(
        self,
        *,
        active: Committee,
        regime: MarketRegimeClassifier,
        confidence: ConfidenceEngine,
        explainer: ExplanationBuilder,
        event_bus: EventBus,
        metrics: LearningMetrics,
        prediction_store: PredictionStore,
        shadows: Sequence[Committee] = (),
        quality: QualitySignals | None = None,
    ) -> None:
        self._active = active
        self._regime = regime
        self._confidence = confidence
        self._explainer = explainer
        self._bus = event_bus
        self._metrics = metrics
        self._store = prediction_store
        self._shadows = tuple(shadows)
        self._quality = quality or QualitySignals()

    def set_quality(self, quality: QualitySignals) -> None:
        self._quality = quality

    def set_active(self, active: Committee) -> None:
        self._active = active

    def set_shadows(self, shadows: Sequence[Committee]) -> None:
        self._shadows = tuple(shadows)

    async def evaluate(self, candidate: EnrichedCandidate) -> CommitteePrediction:
        """Run the committee over an **enriched** candidate.

        The parameter type is the enforcement of the platform rule that no token
        reaches the AI Committee without first being enriched from the Knowledge
        Engine. There is no overload taking a bare :class:`DecisionContext` and
        no optional enrichment argument, so a caller that skipped the enricher
        cannot construct a call — and the isinstance guard makes that true at
        runtime too, where an untyped caller (a subscriber handed the wrong
        object by the bus) would otherwise fail deep inside the fusion with a
        confusing error.
        """
        if not isinstance(candidate, EnrichedCandidate):
            raise TypeError(
                "the committee only evaluates enriched candidates; "
                f"got {type(candidate).__name__}. Run the Candidate Enricher first."
            )
        started = asyncio.get_running_loop().time()
        correlation = new_id()
        try:
            vector = self._augment(candidate)
            prediction = await self._run(
                self._active, candidate, vector, correlation, shadow=False
            )
            for shadow in self._shadows:
                try:
                    await self._run(shadow, candidate, vector, correlation, shadow=True)
                except Exception as exc:  # a shadow must never affect the active run
                    _logger.warning("shadow_committee_failed", label=shadow.label, error=str(exc))
        except Exception:
            self._metrics.errors.inc()
            raise
        finally:
            elapsed = asyncio.get_running_loop().time() - started
            self._metrics.run_seconds.observe(elapsed)
        self._metrics.committee_runs.inc()
        return prediction

    # -- orchestration --------------------------------------------------------

    async def _run(
        self,
        committee: Committee,
        candidate: EnrichedCandidate,
        vector: NormalizedVector,
        correlation: str,
        *,
        shadow: bool,
    ) -> CommitteePrediction:
        started = asyncio.get_running_loop().time()
        context = candidate.context
        enrichment = candidate.enrichment
        opinions = self._opinions(committee, vector)
        regime = self._regime.classify(vector)
        meta = committee.meta.fuse(
            context.token, opinions, prior_log_odds=enrichment.prior_log_odds
        )
        volatility = self._volatility(vector, regime)
        confidence = self._confidence.compute(
            opinions=opinions,
            regime=regime,
            coverage=vector.coverage,
            volatility=volatility,
            dataset_quality=self._quality.dataset_quality,
            sample_support=self._sample_support(enrichment),
        )
        explanation = self._explainer.build(
            meta=meta,
            opinions=opinions,
            confidence=confidence,
            regime=regime,
            enrichment=enrichment,
        )
        duration_ms = round((asyncio.get_running_loop().time() - started) * 1000.0, 3)
        prediction = CommitteePrediction(
            token=context.token,
            at=context.at,
            meta=meta,
            opinions=opinions,
            confidence=confidence,
            regime=regime,
            explanation=explanation,
            model_versions=self._versions(committee),
            feature_coverage=vector.coverage,
            shadow=shadow,
            correlation_id=correlation,
            duration_ms=duration_ms,
            enrichment=enrichment,
        )
        await self._persist(prediction)
        if not shadow:
            await self._emit(prediction, opinions, correlation)
            self._meter(prediction)
        return prediction

    def _opinions(self, committee: Committee, vector: NormalizedVector) -> tuple[Opinion, ...]:
        opinions: list[Opinion] = []
        for specialist in committee.specialists:
            opinion = specialist.evaluate(vector)
            opinions.append(opinion)
            if committee.label != "active":
                continue
            if opinion.abstained:
                self._metrics.abstentions.labels(member=opinion.member.value).inc()
            else:
                self._metrics.opinions.labels(member=opinion.member.value).inc()
        return tuple(opinions)

    def _augment(self, candidate: EnrichedCandidate) -> NormalizedVector:
        context = candidate.context
        extra = context_feature_values(context)
        extra.update(history_feature_values(candidate.enrichment))
        if not extra:
            return context.vector
        merged = dict(context.vector.values)
        merged.update(extra)
        present = tuple(dict.fromkeys((*context.vector.present, *extra.keys())))
        return context.vector.model_copy(update={"values": merged, "present": present})

    def _sample_support(self, enrichment: CandidateEnrichment) -> float:
        """How many comparable historical examples exist — for *this* candidate.

        The configured value is a platform-wide floor derived from the dataset;
        the enrichment knows how much of that history is actually about tokens
        like this one. Taking the larger of the two means a candidate the
        platform has genuine precedent for is credited for it, while one it has
        never seen anything like is never credited with *less* than the
        platform-wide baseline it does have.
        """
        if not enrichment.evidence_available:
            return self._quality.sample_support
        return max(self._quality.sample_support, enrichment.sample_support)

    @staticmethod
    def _volatility(vector: NormalizedVector, regime: RegimeAssessment) -> float:
        base = vector.values.get("tech.volatility", 0.2)
        explosion = vector.values.get("tech.volatility_explosion", 0.0)
        panic = max(
            regime.scores.get(MarketRegime.HIGHLY_VOLATILE.value, 0.0),
            regime.scores.get(MarketRegime.PANIC.value, 0.0),
        )
        return max(base, 0.6 * explosion + 0.4 * panic)

    @staticmethod
    def _versions(committee: Committee) -> dict[str, str]:
        versions = {s.member.value: s.version for s in committee.specialists}
        versions[META_MODEL_NAME] = committee.meta.version
        return versions

    # -- side effects ---------------------------------------------------------

    async def _persist(self, prediction: CommitteePrediction) -> None:
        try:
            await self._store.save(prediction)
        except Exception as exc:  # persistence is best-effort
            _logger.warning("prediction_save_failed", error=str(exc))

    async def _emit(
        self, prediction: CommitteePrediction, opinions: Sequence[Opinion], correlation: str
    ) -> None:
        token = prediction.token
        for opinion in opinions:
            await self._bus.publish(
                InferenceCompleted(
                    aggregate_id=new_id(),
                    correlation_id=correlation,
                    token=token,
                    member=opinion.member.value,
                    probability=round(opinion.probability, 4),
                    confidence=round(opinion.confidence, 4),
                    model_version=opinion.model_version,
                )
            )
        await self._bus.publish(
            CommitteeFinished(
                aggregate_id=new_id(),
                correlation_id=correlation,
                token=token,
                members=len(opinions),
                abstained=sum(1 for o in opinions if o.abstained),
            )
        )
        await self._bus.publish(
            ConfidenceCalculated(
                aggregate_id=new_id(),
                correlation_id=correlation,
                token=token,
                confidence=prediction.confidence.final,
                coverage=prediction.confidence.coverage,
                agreement=prediction.confidence.agreement,
            )
        )
        await self._bus.publish(
            PredictionGenerated(
                aggregate_id=new_id(),
                correlation_id=correlation,
                token=token,
                prediction=prediction.meta,
            )
        )
        await self._bus.publish(
            CommitteePredictionGenerated(
                aggregate_id=new_id(),
                correlation_id=correlation,
                token=token,
                prediction=prediction,
            )
        )

    def _meter(self, prediction: CommitteePrediction) -> None:
        self._metrics.last_roi_probability.set(prediction.meta.prob_roi_positive)
        self._metrics.last_confidence.set(prediction.confidence.final)
