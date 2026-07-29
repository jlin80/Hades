"""Knowledge feedback, subscriber wiring, factory round-trip and schema tests."""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.features.infrastructure.feature_store import InMemoryFeatureStore
from hades.contexts.intelligence.domain.events import WalletIntelligenceComputed
from hades.contexts.intelligence.domain.models import IntelligenceSnapshot
from hades.contexts.learning.application.committee.factory import (
    committee_from_cards,
    default_committee,
)
from hades.contexts.learning.application.committee.manager import CommitteeManager, QualitySignals
from hades.contexts.learning.application.confidence import ConfidenceEngine
from hades.contexts.learning.application.explainability import ExplanationBuilder
from hades.contexts.learning.application.feature_catalog import FeatureCatalog, FeatureNormalizer
from hades.contexts.learning.application.knowledge_feedback import KnowledgeFeedback
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.regime import MarketRegimeClassifier
from hades.contexts.learning.application.subscriber import CommitteeHandler
from hades.contexts.learning.application.training import TrainingEngine
from hades.contexts.learning.domain.events import CommitteePredictionGenerated
from hades.contexts.learning.domain.models import (
    Dataset,
    DecisionContext,
    ModelCard,
    ModelMetrics,
    ModelWeights,
    TrainingSample,
)
from hades.contexts.learning.domain.ports import DecisionContextBuilder
from hades.contexts.learning.infrastructure import serializers as ser
from hades.contexts.learning.infrastructure.stores import (
    InMemoryOutcomeStore,
    InMemoryPredictionStore,
)
from hades.contexts.security.domain.events import TokenRejected
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry
from tests.enrichment_support import enricher

_TOKEN = TokenRef(mint=TokenMint(address="M" * 44))


# --- knowledge feedback ------------------------------------------------------


async def test_feedback_records_executed_outcome() -> None:
    store = InMemoryOutcomeStore()
    feedback = KnowledgeFeedback(store, InMemoryFeatureStore(), FeatureNormalizer(FeatureCatalog()))
    await feedback.record_outcome(
        token_mint="A" * 44,
        features={"basic.liquidity": 0.8},
        realized_roi=0.3,
        hit_tp=True,
        hit_sl=False,
    )
    samples = await store.load()
    assert len(samples) == 1 and samples[0].label_roi_positive and samples[0].was_executed


async def test_feedback_learns_from_security_rejection() -> None:
    store = InMemoryOutcomeStore()
    features = InMemoryFeatureStore()
    await features.save(
        FeatureSet(token=_TOKEN, computed_at=datetime.now(UTC), values={"basic.liquidity": 0.2})
    )
    feedback = KnowledgeFeedback(store, features, FeatureNormalizer(FeatureCatalog()))
    bus = InMemoryEventBus()
    feedback.register(bus)
    await bus.publish(
        TokenRejected(aggregate_id=new_id(), token=_TOKEN, score=20.0, reasons=("rug",))
    )
    samples = await store.load()
    assert len(samples) == 1
    assert samples[0].was_rejected and not samples[0].label_roi_positive
    assert samples[0].weight < 1.0  # weak label


# --- subscriber wiring -------------------------------------------------------


class _StubBuilder(DecisionContextBuilder):
    def __init__(self) -> None:
        fs = FeatureSet(
            token=_TOKEN,
            computed_at=datetime.now(UTC),
            values={"basic.liquidity": 120_000.0, "security.score": 0.0},
        )
        self._vector = FeatureNormalizer(FeatureCatalog()).normalize(fs)

    async def build(self, event: WalletIntelligenceComputed) -> DecisionContext | None:
        return DecisionContext(
            token=event.token, at=datetime.now(UTC), vector=self._vector, security_score=78.0
        )


async def test_handler_runs_committee_on_intelligence_event() -> None:
    bus = InMemoryEventBus()
    seen: list[DomainEvent] = []

    async def _collect(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(CommitteePredictionGenerated.__name__, _collect)
    manager = CommitteeManager(
        active=default_committee(),
        regime=MarketRegimeClassifier(),
        confidence=ConfidenceEngine(),
        explainer=ExplanationBuilder(),
        event_bus=bus,
        metrics=LearningMetrics(MetricsRegistry()),
        prediction_store=InMemoryPredictionStore(),
        quality=QualitySignals(),
    )
    CommitteeHandler(manager, _StubBuilder(), enricher()).register(bus)
    await bus.publish(
        WalletIntelligenceComputed(
            aggregate_id=new_id(),
            token=_TOKEN,
            snapshot=IntelligenceSnapshot(token=_TOKEN, wallets_observed=5, smart_money_count=3),
        )
    )
    assert len(seen) == 1


async def test_handler_swallows_none_context() -> None:
    class _NoneBuilder(DecisionContextBuilder):
        async def build(self, event: WalletIntelligenceComputed) -> DecisionContext | None:
            return None

    bus = InMemoryEventBus()
    manager = CommitteeManager(
        active=default_committee(),
        regime=MarketRegimeClassifier(),
        confidence=ConfidenceEngine(),
        explainer=ExplanationBuilder(),
        event_bus=bus,
        metrics=LearningMetrics(MetricsRegistry()),
        prediction_store=InMemoryPredictionStore(),
    )
    CommitteeHandler(manager, _NoneBuilder(), enricher()).register(bus)
    # No context -> no crash, no prediction.
    await bus.publish(
        WalletIntelligenceComputed(
            aggregate_id=new_id(), token=_TOKEN, snapshot=IntelligenceSnapshot(token=_TOKEN)
        )
    )


# --- factory reconstruction --------------------------------------------------


def test_committee_from_cards_uses_trained_weights_and_defaults() -> None:
    dataset_cards = TrainingEngine().train(_learnable_dataset(), version="1").cards
    committee = committee_from_cards(dataset_cards)
    assert len(committee.specialists) == 12
    # Meta reconstructed with three trained heads.
    assert set(committee.meta.heads) == {"roi_positive", "hit_tp", "hit_sl"}
    # A promoted-card variant keeps the trained meta version.
    assert committee.meta.version == "1"


def test_committee_from_cards_falls_back_to_default() -> None:
    committee = committee_from_cards(())  # empty registry -> all defaults
    assert len(committee.specialists) == 12


# --- serializers round-trip --------------------------------------------------


def test_model_card_row_round_trip() -> None:
    card = ModelCard(
        model_id="m1",
        name="meta",
        version="3",
        weights=ModelWeights(bias=0.1, coefficients={"security": 1.0}),
        heads={
            "roi_positive": ModelWeights(bias=0.1, coefficients={"security": 1.0}),
            "hit_tp": ModelWeights(bias=-0.2, coefficients={"momentum": 0.8}),
        },
        metrics=ModelMetrics(samples=50, auc=0.7),
    )
    restored = ser.card_from_row(ser.card_to_row(card))
    assert restored.name == "meta" and restored.version == "3"
    assert restored.weights.coefficients == {"security": 1.0}
    assert set(restored.heads) == {"roi_positive", "hit_tp"}


# --- schema registration -----------------------------------------------------


def test_committee_tables_registered_on_metadata() -> None:
    from hades.shared_kernel.persistence.database import Base

    tables = set(Base.metadata.tables)
    for name in (
        "committee_predictions",
        "committee_models",
        "committee_datasets",
        "committee_feature_importance",
        "committee_drift",
        "committee_outcomes",
    ):
        assert name in tables


def _learnable_dataset(n: int = 120) -> Dataset:
    import random

    rng = random.Random(3)
    base = datetime.now(UTC)
    samples: list[TrainingSample] = []
    for i in range(n):
        liq, sec = rng.random(), rng.random()
        positive = (1.5 * liq + 1.4 * sec + rng.gauss(0, 0.25)) > 1.2
        samples.append(
            TrainingSample(
                token_mint=f"{i:044d}"[:44],
                at=base,
                features={
                    "basic.liquidity": liq,
                    "security.score": sec,
                    "security.approved": 1.0 if sec > 0.5 else 0.0,
                    "security.sub.rug_pull": sec,
                    "holders.gini": 1 - liq,
                    "intel.smart_money_ratio": sec,
                    "tech.volatility": 1 - sec,
                },
                label_roi_positive=positive,
                label_hit_tp=positive,
                label_hit_sl=not positive,
                was_executed=i % 2 == 0,
                was_rejected=i % 2 == 1,
            )
        )
    names = tuple(sorted({k for s in samples for k in s.features}))
    return Dataset(dataset_id="ds-int", name="int", samples=tuple(samples), feature_names=names)
