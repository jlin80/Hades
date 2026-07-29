"""AI Committee runtime composition — wires and runs the brain.

Process-level wiring (the ops layer may import application + infrastructure). It
assembles the whole committee subsystem from the container and subscribes it to
the end of the analytical pipeline:

    intelligence ─WalletIntelligenceComputed─► [context builder → committee]
        ─► InferenceCompleted / CommitteeFinished / ConfidenceCalculated /
           PredictionGenerated / CommitteePredictionGenerated

It also wires Knowledge Feedback (learning from security rejections), an optional
**shadow committee** for comparison, and — when ``auto_train`` is on — a periodic,
off-the-hot-path training pass that builds *candidate* models (never promotes;
promotion stays human/policy-gated). Nothing here trades, sizes or enables live.
It only produces probabilities and evidence, and publishes a Redis status snapshot
for the dashboard.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.features.infrastructure.feature_store import (
    CachingFeatureStore,
    InMemoryFeatureStore,
    PostgresFeatureStore,
)
from hades.contexts.knowledge.domain.events import LessonLearned
from hades.contexts.knowledge.domain.ports import KnowledgeStore, LessonStore
from hades.contexts.knowledge.infrastructure.stores import (
    InMemoryKnowledgeStore,
    InMemoryLessonStore,
    PostgresKnowledgeStore,
    PostgresLessonStore,
)
from hades.contexts.learning.application.committee.factory import (
    committee_from_cards,
    default_committee,
)
from hades.contexts.learning.application.committee.manager import (
    Committee,
    CommitteeManager,
    QualitySignals,
)
from hades.contexts.learning.application.confidence import ConfidenceEngine
from hades.contexts.learning.application.dataset_builder import DatasetBuilder
from hades.contexts.learning.application.enricher import (
    EnrichmentPolicy,
    KnowledgeCandidateEnricher,
)
from hades.contexts.learning.application.explainability import ExplanationBuilder
from hades.contexts.learning.application.feature_catalog import FeatureCatalog, FeatureNormalizer
from hades.contexts.learning.application.knowledge_feedback import KnowledgeFeedback
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.regime import MarketRegimeClassifier
from hades.contexts.learning.application.registry import ModelRegistryService
from hades.contexts.learning.application.subscriber import CommitteeHandler
from hades.contexts.learning.application.training import TrainingEngine
from hades.contexts.learning.application.validation import ValidationEngine
from hades.contexts.learning.domain.events import CommitteePredictionGenerated, ModelPromoted
from hades.contexts.learning.domain.models import META_MODEL_NAME, Dataset
from hades.contexts.learning.domain.ports import (
    DatasetStore,
    ModelRegistry,
    OutcomeStore,
    PredictionStore,
)
from hades.contexts.learning.infrastructure.candidate_history import KnowledgeCandidateHistory
from hades.contexts.learning.infrastructure.context_builder import DecisionContextBuilder
from hades.contexts.learning.infrastructure.model_registry import (
    InMemoryModelRegistry,
    PostgresModelRegistry,
)
from hades.contexts.learning.infrastructure.stores import (
    InMemoryDatasetStore,
    InMemoryOutcomeStore,
    InMemoryPredictionStore,
    PostgresDatasetStore,
    PostgresOutcomeStore,
    PostgresPredictionStore,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.runtime")

#: Redis namespace + key the Worker publishes live committee status under.
COMMITTEE_STATUS_NAMESPACE = "committee"
COMMITTEE_STATUS_KEY = "status"
_STATUS_INTERVAL_SECONDS = 5.0
_STATUS_TTL_SECONDS = 30


class CommitteeRuntime:
    """Owns the wired AI Committee subsystem and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._metrics = LearningMetrics(container.metrics)
        self._status_cache = CacheService(container.redis, namespace=COMMITTEE_STATUS_NAMESPACE)
        self._catalog = FeatureCatalog()
        self._normalizer = FeatureNormalizer(self._catalog)
        self._feature_store = self._build_feature_store()
        self._registry: ModelRegistry = self._build_registry()
        self._prediction_store: PredictionStore = self._build_prediction_store()
        self._outcome_store: OutcomeStore = self._build_outcome_store()
        self._dataset_store: DatasetStore = self._build_dataset_store()
        self._registry_service = ModelRegistryService(
            self._registry, self._c.event_bus, self._metrics
        )
        self._dataset_builder = DatasetBuilder(self._outcome_store)
        self._training = TrainingEngine()
        self._validation = ValidationEngine()
        self._manager = self._build_manager()
        self._enricher = self._build_enricher()
        self._feedback = KnowledgeFeedback(
            self._outcome_store, self._feature_store, self._normalizer
        )
        self._predictions_session = 0
        self._lessons_session = 0
        self._enriched_session = 0
        self._last: dict[str, Any] = {}
        self._register()

    # -- construction ---------------------------------------------------------

    def _build_feature_store(self) -> Any:
        if self._c.database is not None:
            return CachingFeatureStore(PostgresFeatureStore(self._c.database), self._c.redis)
        return InMemoryFeatureStore()

    def _build_registry(self) -> ModelRegistry:
        if self._c.database is not None:
            return PostgresModelRegistry(self._c.database)
        return InMemoryModelRegistry()

    def _build_prediction_store(self) -> PredictionStore:
        if self._c.database is not None:
            return PostgresPredictionStore(self._c.database)
        return InMemoryPredictionStore()

    def _build_outcome_store(self) -> OutcomeStore:
        if self._c.database is not None:
            return PostgresOutcomeStore(self._c.database)
        return InMemoryOutcomeStore()

    def _build_dataset_store(self) -> DatasetStore:
        if self._c.database is not None:
            return PostgresDatasetStore(self._c.database)
        return InMemoryDatasetStore()

    def _build_manager(self) -> CommitteeManager:
        learning = self._c.settings.learning
        shadows: tuple[Committee, ...] = ()
        if learning.shadow_enabled:
            shadows = (default_committee(label=learning.shadow_label),)
        quality = QualitySignals(
            dataset_quality=learning.default_dataset_quality,
            sample_support=learning.default_sample_support,
        )
        return CommitteeManager(
            active=default_committee(),
            regime=MarketRegimeClassifier(),
            confidence=ConfidenceEngine(),
            explainer=ExplanationBuilder(),
            event_bus=self._c.event_bus,
            metrics=self._metrics,
            prediction_store=self._prediction_store,
            shadows=shadows,
            quality=quality,
        )

    def _build_enricher(self) -> KnowledgeCandidateEnricher:
        """Wire the Candidate Enricher onto the platform's permanent memory.

        The store adapters are built here rather than borrowed from the
        Knowledge runtime: this is a **read-only** window, the two runtimes must
        be able to live in different processes, and a shared object handle would
        be a live reference from the brain into the memory's write path — the
        coupling the Knowledge context was designed to refuse.
        """
        learning = self._c.settings.learning
        if self._c.database is not None:
            lessons: LessonStore = PostgresLessonStore(self._c.database)
            observations: KnowledgeStore = PostgresKnowledgeStore(self._c.database)
        else:
            lessons = InMemoryLessonStore()
            observations = InMemoryKnowledgeStore()
        history = KnowledgeCandidateHistory(
            lessons,
            observations,
            self._normalizer,
            cache_ttl_seconds=learning.enrichment_cache_seconds,
        )
        policy = EnrichmentPolicy(
            lesson_window=learning.enrichment_lesson_window,
            shrinkage=learning.enrichment_shrinkage,
            max_prior_log_odds=learning.enrichment_max_prior_log_odds,
            support_target=learning.enrichment_support_target,
            neighbours=learning.enrichment_neighbours,
            min_cohort=learning.enrichment_min_cohort,
        )
        return KnowledgeCandidateEnricher(history, self._normalizer, policy, self._metrics)

    def _register(self) -> None:
        builder = DecisionContextBuilder(self._feature_store, self._normalizer, self._c.database)
        CommitteeHandler(self._manager, builder, self._enricher).register(self._c.event_bus)
        self._feedback.register(self._c.event_bus)
        self._c.event_bus.subscribe(CommitteePredictionGenerated.__name__, self._on_prediction)
        # The learning loop's return leg. Knowledge does the joining (a decision's
        # frozen evidence + its realised outcome); this is where ground truth
        # finally reaches the ledger the Dataset Builder reads.
        if self._c.settings.knowledge.feed_committee:
            self._c.event_bus.subscribe(LessonLearned.__name__, self._on_lesson)
        # A promotion that the runtime never notices is a promotion that did not
        # happen. ``set_active`` used to run only at startup, so a promoted model
        # changed nothing until the worker was restarted — the human-gated
        # promotion machinery worked perfectly and had no effect.
        self._c.event_bus.subscribe(ModelPromoted.__name__, self._on_model_promoted)

    async def _on_lesson(self, event: DomainEvent) -> None:
        """Write a completed lesson into the outcome ledger.

        The features come from the lesson, never from a fresh feature-store
        lookup: they were captured at decision time, and re-reading them now
        would label this trade with the state of the world at the moment it was
        sold — leakage that produces excellent metrics and a useless model.
        """
        if not isinstance(event, LessonLearned):
            return
        lesson = event.lesson
        try:
            await self._feedback.record_outcome(
                token_mint=lesson.subject,
                features=dict(lesson.features),
                realized_roi=lesson.realized_roi,
                hit_tp=lesson.label_hit_tp,
                hit_sl=lesson.label_hit_sl,
                at=lesson.decided_at,
            )
            self._lessons_session += 1
            _logger.info(
                "outcome_recorded",
                mint=lesson.subject,
                realized_roi=round(lesson.realized_roi, 6),
                positive=lesson.label_roi_positive,
                features=len(lesson.features),
            )
        except Exception as exc:  # one lesson must never break the bus
            self._metrics.errors.inc()
            _logger.warning("outcome_record_failed", mint=lesson.subject, error=str(exc))

    async def _on_model_promoted(self, event: DomainEvent) -> None:
        """Reload the active committee in place after a promotion."""
        if not isinstance(event, ModelPromoted):
            return
        try:
            await self._load_active_committee()
        except Exception as exc:  # a failed reload must not break the bus
            self._metrics.errors.inc()
            _logger.warning("committee_reload_failed", error=str(exc))

    # -- loading the active committee from the registry -----------------------

    async def _load_active_committee(self) -> None:
        try:
            active_cards = await self._registry.list_active()
        except Exception as exc:  # a cold registry must not stop startup
            _logger.warning("committee_registry_load_failed", error=str(exc))
            return
        if active_cards:
            self._manager.set_active(committee_from_cards(active_cards))
            _logger.info("committee_loaded_from_registry", models=len(active_cards))

    # -- scheduled training (candidates only; never promotes) -----------------

    async def train_once(self) -> None:
        """Build a dataset, train + validate candidates, register + propose.

        Runs off the hot path. It only *proposes* — promotion stays human-gated.
        """
        outcomes = await self._outcome_store.count()
        if outcomes < self._c.settings.learning.min_outcomes_to_train:
            _logger.info("training_deferred", outcomes=outcomes)
            return
        dataset = await self._dataset_builder.build()
        await self._dataset_store.save(dataset)
        self._refresh_quality(dataset)
        version = await self._registry.next_version(META_MODEL_NAME)
        result = self._training.train(dataset, version=version)
        self._metrics.training_runs.inc()
        for card in result.cards:
            await self._registry_service.register(card)
            incumbent = await self._registry.active(card.name)
            report = self._validation.validate(
                card, dataset, incumbent=incumbent.metrics if incumbent else None
            )
            await self._registry_service.record_validation(card, report)
            if report.passed:
                await self._registry_service.propose_promotion(card, report)
        _logger.info("training_pass_complete", candidates=len(result.cards), version=version)

    def _refresh_quality(self, dataset: Dataset) -> None:
        """Replace the configured priors with what the data actually says.

        ``dataset_quality`` and ``sample_support`` were read once from settings
        and never recomputed, so two numbers presented to the confidence engine
        as *measurements* were in fact constants — 0.5 and 0.35 forever, however
        much or little the platform had learned. The committee's confidence was
        therefore partly a reflection of a config file.

        Both are now derived. Support saturates at ``min_outcomes_to_train``:
        below it the platform genuinely does not have enough history, and saying
        so is the honest input to a confidence calculation. Quality is the
        balance of the labels — a dataset that is 99% one class supports very
        little regardless of its size, which is precisely the state this platform
        sat in while reporting healthy priors.
        """
        target = max(1, self._c.settings.learning.min_outcomes_to_train)
        support = min(1.0, dataset.size / target)
        positive_rate = dataset.positive_rate
        # Balance peaks at 1.0 for a 50/50 split and falls to 0.0 at either
        # extreme; a single-class dataset scores 0, which is the truth.
        balance = max(0.0, 1.0 - abs(0.5 - positive_rate) * 2.0)
        self._manager.set_quality(QualitySignals(dataset_quality=balance, sample_support=support))
        _logger.info(
            "quality_signals_refreshed",
            samples=dataset.size,
            positive_rate=round(positive_rate, 4),
            dataset_quality=round(balance, 4),
            sample_support=round(support, 4),
        )

    async def _training_loop(self) -> None:
        interval = max(1, self._c.settings.learning.retrain_interval_hours) * 3600
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await self.train_once()
                except Exception as exc:  # training must never crash the worker
                    self._metrics.errors.inc()
                    _logger.warning("scheduled_training_failed", error=str(exc))

    # -- dashboard stats ------------------------------------------------------

    async def _on_prediction(self, event: DomainEvent) -> None:
        if not isinstance(event, CommitteePredictionGenerated):
            return
        self._predictions_session += 1
        p = event.prediction
        enrichment = p.enrichment
        if enrichment is not None and enrichment.evidence_available:
            self._enriched_session += 1
        self._last = {
            "mint": str(p.token.mint),
            "prob_roi_positive": p.meta.prob_roi_positive,
            "prob_hit_tp": p.meta.prob_hit_tp,
            "prob_hit_sl": p.meta.prob_hit_sl,
            "confidence": p.confidence.final,
            "regime": p.regime.regime.value,
            "headline": p.explanation.headline if p.explanation else "",
            "at": p.at.isoformat(),
            # What the platform already knew when it produced this number.
            "prior_log_odds": enrichment.prior_log_odds if enrichment else 0.0,
            "history_samples": enrichment.total_samples if enrichment else 0,
            "history_dimensions": list(enrichment.informative_dimensions) if enrichment else [],
        }

    def snapshot(self) -> dict[str, Any]:
        """Live status for the dashboard (probabilities only — never trade info)."""
        return {
            "enabled": self._c.settings.learning.committee_enabled,
            "members": len(default_committee().specialists),
            "shadow_enabled": self._c.settings.learning.shadow_enabled,
            "auto_train": self._c.settings.learning.auto_train,
            "predictions_session": self._predictions_session,
            # Ground-truth samples that reached the ledger this session. Zero
            # while trades are closing means the learning loop is open again.
            "lessons_session": self._lessons_session,
            # Predictions this session that had real precedent behind them. Zero
            # while predictions climb means the committee is judging every token
            # from scratch — the state Phase 3 exists to end.
            "enriched_with_evidence_session": self._enriched_session,
            "last_prediction": self._last,
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._status_cache.set(
                    COMMITTEE_STATUS_KEY, self.snapshot(), ttl_seconds=_STATUS_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("committee_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STATUS_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        await self._load_active_committee()
        tasks = [asyncio.create_task(self._publish_status_loop(), name="committee-status")]
        if self._c.settings.learning.auto_train:
            tasks.append(asyncio.create_task(self._training_loop(), name="committee-training"))
        _logger.info("committee_runtime_started", auto_train=self._c.settings.learning.auto_train)
        return tasks

    async def stop(self) -> None:
        self._stop.set()
        _logger.info("committee_runtime_stopped")
