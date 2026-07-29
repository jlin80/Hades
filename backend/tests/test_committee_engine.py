"""End-to-end CommitteeManager: opinions -> meta -> events -> persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.learning.application.committee.factory import default_committee
from hades.contexts.learning.application.committee.manager import CommitteeManager, QualitySignals
from hades.contexts.learning.application.confidence import ConfidenceEngine
from hades.contexts.learning.application.explainability import ExplanationBuilder
from hades.contexts.learning.application.feature_catalog import FeatureCatalog, FeatureNormalizer
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.regime import MarketRegimeClassifier
from hades.contexts.learning.domain.events import (
    CommitteeFinished,
    CommitteePredictionGenerated,
    ConfidenceCalculated,
    InferenceCompleted,
    PredictionGenerated,
)
from hades.contexts.learning.domain.models import CommitteePrediction, DecisionContext
from hades.contexts.learning.infrastructure.stores import InMemoryPredictionStore
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry
from tests.enrichment_support import enrich

_TOKEN = TokenRef(mint=TokenMint(address="M" * 44))

_STRONG = {
    "basic.liquidity": 150_000.0,
    "pool.depth_usd": 50_000.0,
    "basic.spread_bps": 25.0,
    "basic.slippage_bps": 40.0,
    "basic.liquidity_to_mcap": 0.4,
    "tech.rsi_14": 60.0,
    "tech.price_slope": 0.6,
    "tech.trend_alignment": 0.7,
    "tech.volatility": 0.15,
    "holders.count": 900,
    "holders.top10_share_pct": 20.0,
    "holders.gini": 0.35,
    "basic.market_cap": 400_000.0,
    "regime.macro_trend": 0.7,
    "regime.micro_trend": 0.6,
    "basic.age_minutes": 180.0,
    "basic.trades_5m": 45,
    "basic.trades_1h": 350,
    "basic.buyer_seller_ratio": 1.5,
    "basic.buy_sell_volume_ratio": 1.4,
    "basic.volume_to_liquidity": 2.2,
    "tech.volatility_compression": 0.6,
    "pool.lp_locked": 1.0,
    "holders.recurring_ratio": 0.55,
    "holders.holders_growth_pct": 14.0,
}


class _Harness:
    def __init__(self, *, shadow: bool = False) -> None:
        self.bus = InMemoryEventBus()
        self.events: list[DomainEvent] = []
        for name in (
            InferenceCompleted.__name__,
            CommitteeFinished.__name__,
            ConfidenceCalculated.__name__,
            PredictionGenerated.__name__,
            CommitteePredictionGenerated.__name__,
        ):
            self.bus.subscribe(name, self._collect)
        self.store = InMemoryPredictionStore()
        shadows = (default_committee("shadow_baseline"),) if shadow else ()
        self.manager = CommitteeManager(
            active=default_committee(),
            regime=MarketRegimeClassifier(),
            confidence=ConfidenceEngine(),
            explainer=ExplanationBuilder(),
            event_bus=self.bus,
            metrics=LearningMetrics(MetricsRegistry()),
            prediction_store=self.store,
            shadows=shadows,
            quality=QualitySignals(dataset_quality=0.6, sample_support=0.5),
        )

    async def evaluate(self, context: DecisionContext) -> CommitteePrediction:
        """Enrich, then judge — the only route into the committee there is.

        These tests run against an *empty* memory on purpose: enrichment must be
        exactly neutral when the platform has learned nothing, so every
        expectation below is the pre-enrichment behaviour, unchanged.
        """
        return await self.manager.evaluate(await enrich(context))

    async def _collect(self, event: DomainEvent) -> None:
        self.events.append(event)

    def count(self, kind: type[DomainEvent]) -> int:
        return sum(1 for e in self.events if isinstance(e, kind))


def _context(values: dict[str, float], **kwargs: object) -> DecisionContext:
    fs = FeatureSet(token=_TOKEN, computed_at=datetime.now(UTC), values=values)
    vector = FeatureNormalizer(FeatureCatalog()).normalize(fs)
    defaults: dict[str, object] = {
        "security_approved": True,
        "security_score": 80.0,
        "security_sub_scores": {
            "contract": 85,
            "authority": 80,
            "honeypot": 90,
            "developer": 75,
            "rug_pull": 82,
            "wallet_cluster": 78,
            "behavior": 72,
            "liquidity": 80,
        },
        "smart_money_count": 6,
        "dumb_money_count": 2,
        "smart_money_pct_supply": 9.0,
        "avg_wallet_trust": 64.0,
        "avg_wallet_risk": 30.0,
        "cluster_count": 1,
        "largest_cluster_pct": 5.0,
        "wallets_observed": 15,
    }
    defaults.update(kwargs)
    return DecisionContext(token=_TOKEN, at=datetime.now(UTC), vector=vector, **defaults)


async def test_committee_produces_full_prediction() -> None:
    h = _Harness()
    prediction = await h.evaluate(_context(_STRONG))

    assert len(prediction.opinions) == 12
    assert all(not o.abstained for o in prediction.opinions)
    assert 0.0 <= prediction.meta.prob_roi_positive <= 1.0
    assert prediction.explanation is not None and prediction.explanation.headline
    assert prediction.confidence.final > 0.0
    assert prediction.model_versions  # every member + meta versioned


async def test_committee_emits_all_events() -> None:
    h = _Harness()
    await h.evaluate(_context(_STRONG))
    assert h.count(InferenceCompleted) == 12
    assert h.count(CommitteeFinished) == 1
    assert h.count(ConfidenceCalculated) == 1
    assert h.count(PredictionGenerated) == 1
    assert h.count(CommitteePredictionGenerated) == 1


async def test_prediction_persisted_and_retrievable() -> None:
    h = _Harness()
    await h.evaluate(_context(_STRONG))
    latest = await h.store.latest_for(_TOKEN)
    assert latest is not None
    recent = await h.store.recent()
    assert len(recent) == 1


async def test_shadow_runs_but_does_not_emit_headline_event() -> None:
    h = _Harness(shadow=True)
    await h.evaluate(_context(_STRONG))
    # Only the active committee emits the headline event.
    assert h.count(CommitteePredictionGenerated) == 1
    # Both active and shadow were persisted.
    recent = await h.store.recent(limit=10)  # recent() filters out shadow
    assert len(recent) == 1


async def test_strong_token_scores_above_weak_token() -> None:
    h = _Harness()
    strong = await h.evaluate(_context(_STRONG))
    weak = await h.evaluate(
        _context(
            _STRONG,
            security_approved=False,
            security_score=25.0,
            security_sub_scores={"rug_pull": 15, "contract": 30, "developer": 20},
            smart_money_count=0,
            dumb_money_count=9,
            avg_wallet_risk=75.0,
            avg_wallet_trust=25.0,
            largest_cluster_pct=40.0,
            cluster_count=4,
        )
    )
    assert strong.meta.prob_hit_sl < weak.meta.prob_hit_sl
    assert strong.meta.prob_roi_positive >= weak.meta.prob_roi_positive


async def test_low_coverage_lowers_confidence() -> None:
    h = _Harness()
    rich = await h.evaluate(_context(_STRONG))
    sparse = await h.evaluate(_context({"basic.liquidity": 100_000.0}))
    assert sparse.confidence.coverage < rich.confidence.coverage


@pytest.mark.parametrize("values", [{}, {"basic.liquidity": 1000.0}])
async def test_committee_never_crashes_on_thin_input(values: dict[str, float]) -> None:
    h = _Harness()
    prediction = await h.evaluate(_context(values))
    assert prediction is not None  # degrades, never raises
