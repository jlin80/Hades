"""The Candidate Enricher: the committee never judges a token from scratch.

The tests are organised around the four properties the phase claims, because
each of them is a way this component could be wrong while looking right:

1. **Enrichment is mandatory.** Not "wired up" — impossible to bypass.
2. **An empty memory changes nothing.** The whole point is that the cold start
   is solved with knowledge, never by quietly relaxing the platform.
3. **Real history moves the number, in the right direction, by a bounded amount.**
4. **A broken memory degrades to "we could not ask", never to a fabricated
   prior and never to a token that goes unjudged.**
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.learning.application.committee.factory import default_committee
from hades.contexts.learning.application.committee.history_features import history_feature_values
from hades.contexts.learning.application.committee.manager import CommitteeManager, QualitySignals
from hades.contexts.learning.application.confidence import ConfidenceEngine
from hades.contexts.learning.application.enricher import EnrichmentPolicy
from hades.contexts.learning.application.explainability import ExplanationBuilder
from hades.contexts.learning.application.feature_catalog import FeatureCatalog, FeatureNormalizer
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.narrative import narrative_of
from hades.contexts.learning.application.regime import MarketRegimeClassifier
from hades.contexts.learning.domain.models import (
    CandidateIdentity,
    DecisionContext,
    EvidenceBasis,
    HistoricalLesson,
    HistoricalRecord,
    HistoryDimension,
)
from hades.contexts.learning.infrastructure.stores import InMemoryPredictionStore
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry
from tests.enrichment_support import FakeHistory, enrich, enricher

_TOKEN = TokenRef(mint=TokenMint(address="M" * 44))
_DEV = "Dev" + "1" * 41
_NOW = datetime.now(UTC)

_VALUES = {
    "basic.liquidity": 120_000.0,
    "pool.depth_usd": 40_000.0,
    "tech.rsi_14": 58.0,
    "tech.price_slope": 0.4,
    "tech.volatility": 0.2,
    "holders.count": 800,
    "holders.top10_share_pct": 22.0,
    "holders.gini": 0.36,
    "basic.market_cap": 350_000.0,
    "basic.age_minutes": 120.0,
    "basic.trades_1h": 240,
}


def _context(**kwargs: object) -> DecisionContext:
    fs = FeatureSet(token=_TOKEN, computed_at=_NOW, values=dict(_VALUES))
    vector = FeatureNormalizer(FeatureCatalog()).normalize(fs)
    defaults: dict[str, object] = {
        "security_approved": True,
        "security_score": 78.0,
        "identity": CandidateIdentity(mint=str(_TOKEN.mint), developer=_DEV),
    }
    defaults.update(kwargs)
    return DecisionContext(token=_TOKEN, at=_NOW, vector=vector, **defaults)


def _lesson(
    *,
    positive: bool,
    tags: dict[str, str] | None = None,
    subject: str = "other",
    age_hours: int = 24,
    features: dict[str, float] | None = None,
) -> HistoricalLesson:
    return HistoricalLesson(
        subject=subject,
        decided_at=_NOW - timedelta(hours=age_hours),
        features=features if features is not None else dict(_VALUES),
        tags=tags or {},
        realized_roi=0.35 if positive else -0.28,
        label_roi_positive=positive,
        label_hit_tp=positive,
        label_hit_sl=not positive,
    )


def _manager(bus: InMemoryEventBus | None = None) -> CommitteeManager:
    return CommitteeManager(
        active=default_committee(),
        regime=MarketRegimeClassifier(),
        confidence=ConfidenceEngine(),
        explainer=ExplanationBuilder(),
        event_bus=bus or InMemoryEventBus(),
        metrics=LearningMetrics(MetricsRegistry()),
        prediction_store=InMemoryPredictionStore(),
        quality=QualitySignals(dataset_quality=0.5, sample_support=0.35),
    )


# --- 1. enrichment is mandatory ---------------------------------------------


async def test_committee_refuses_an_unenriched_candidate() -> None:
    """The rule the phase exists for, asserted at the only door into the brain."""
    with pytest.raises(TypeError, match="enriched"):
        await _manager().evaluate(_context())  # type: ignore[arg-type]


async def test_handler_cannot_be_built_without_an_enricher() -> None:
    """There is no default and no optional argument to forget to pass."""
    from hades.contexts.learning.application.subscriber import CommitteeHandler

    with pytest.raises(TypeError):
        CommitteeHandler(_manager(), object())  # type: ignore[call-arg]


async def test_every_prediction_carries_the_memory_it_was_judged_with() -> None:
    prediction = await _manager().evaluate(await enrich(_context()))
    assert prediction.enrichment is not None
    assert prediction.enrichment.identity.mint == str(_TOKEN.mint)
    # Consulted and empty is a *recorded* state, distinct from never consulted.
    assert prediction.enrichment.evidence_available is False


# --- 2. an empty memory changes nothing --------------------------------------


async def test_empty_memory_is_exactly_neutral() -> None:
    """No history must mean no nudge — not a small one, not a helpful one."""
    candidate = await enrich(_context())
    assert candidate.enrichment.prior_log_odds == 0.0
    assert candidate.enrichment.total_samples == 0
    assert all(not prior.is_informative for prior in candidate.enrichment.priors)


async def test_empty_memory_reproduces_the_pre_enrichment_probabilities() -> None:
    """The regression that would matter most: enrichment silently recalibrating.

    The fusion with a zero prior must be bit-for-bit the fusion without one.
    """
    candidate = await enrich(_context())
    committee = default_committee()
    vector = candidate.context.vector
    opinions = tuple(s.evaluate(vector) for s in committee.specialists)
    baseline = committee.meta.fuse(_TOKEN, opinions)
    with_zero_prior = committee.meta.fuse(_TOKEN, opinions, prior_log_odds=0.0)
    assert with_zero_prior.prob_roi_positive == baseline.prob_roi_positive
    assert with_zero_prior.prob_hit_sl == baseline.prob_hit_sl


async def test_a_tiny_cohort_is_reported_but_silent() -> None:
    """Two winning trades are an anecdote; the prior must not act on them."""
    history = FakeHistory(tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(2)))
    candidate = await enrich(_context(), history)
    developer = candidate.enrichment.by_dimension[HistoryDimension.DEVELOPER.value]
    assert developer.samples == 2
    assert developer.strength == 0.0
    assert candidate.enrichment.prior_log_odds == 0.0


# --- 3. real history informs the committee -----------------------------------


async def test_a_developers_record_reaches_the_prior() -> None:
    history = FakeHistory(
        tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(20))
    )
    candidate = await enrich(_context(), history)
    developer = candidate.enrichment.by_dimension[HistoryDimension.DEVELOPER.value]

    assert developer.basis is EvidenceBasis.OUTCOMES
    assert developer.samples == 20
    assert developer.raw_positive_rate == 1.0
    # Shrunk toward ignorance: twenty wins is strong, not certain.
    assert 0.5 < developer.positive_rate < 1.0
    assert candidate.enrichment.prior_log_odds > 0.0
    assert candidate.enrichment.evidence_available


async def test_a_bad_record_pushes_the_other_way() -> None:
    good = FakeHistory(tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(20)))
    bad = FakeHistory(tuple(_lesson(positive=False, tags={"developer": _DEV}) for _ in range(20)))
    assert (await enrich(_context(), good)).enrichment.prior_log_odds > 0.0
    assert (await enrich(_context(), bad)).enrichment.prior_log_odds < 0.0


async def test_history_moves_the_fused_probabilities_in_the_right_direction() -> None:
    """The prior has to actually reach the three numbers, and coherently.

    Good precedent must raise P(ROI+) *and* lower P(SL). Applying one sign to
    all three heads would have made an encouraging history argue that the token
    is more likely to stop out, because the stop head's weights are negative.
    """
    manager = _manager()
    neutral = await manager.evaluate(await enrich(_context()))
    encouraging = await manager.evaluate(
        await enrich(
            _context(),
            FakeHistory(
                tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(40))
            ),
        )
    )
    assert encouraging.meta.prob_roi_positive > neutral.meta.prob_roi_positive
    assert encouraging.meta.prob_hit_sl < neutral.meta.prob_hit_sl


async def test_the_nudge_is_bounded_however_lopsided_the_history() -> None:
    """History informs; it must never overrule the token in front of us."""
    policy = EnrichmentPolicy(max_prior_log_odds=0.4)
    history = FakeHistory(
        tuple(
            _lesson(
                positive=True,
                tags={"developer": _DEV, "narrative": "doge"},
                subject=str(_TOKEN.mint),
            )
            for _ in range(500)
        )
    )
    candidate = await enricher(history, policy).enrich(
        _context(
            identity=CandidateIdentity(mint=str(_TOKEN.mint), developer=_DEV, narrative="doge")
        )
    )
    assert candidate.enrichment.prior_log_odds == pytest.approx(0.4)


async def test_similar_patterns_are_found_without_any_shared_tag() -> None:
    """The dimension that answers "have we been here before?" for a brand-new
    developer, on a brand-new venue, telling a story nobody has told."""
    history = FakeHistory(tuple(_lesson(positive=True) for _ in range(15)))
    candidate = await enrich(
        _context(identity=CandidateIdentity(mint=str(_TOKEN.mint))), history
    )
    patterns = candidate.enrichment.by_dimension[HistoryDimension.PATTERNS.value]
    assert patterns.samples > 0
    assert patterns.is_informative


async def test_dissimilar_history_is_not_treated_as_precedent() -> None:
    """A "nearest" neighbour that is nowhere near is a coincidence, not evidence."""
    far = {"pool.depth_usd": 12.0, "basic.liquidity": 50.0}
    history = FakeHistory(tuple(_lesson(positive=True, features=far) for _ in range(15)))
    candidate = await enrich(
        _context(identity=CandidateIdentity(mint=str(_TOKEN.mint))), history
    )
    patterns = candidate.enrichment.by_dimension[HistoryDimension.PATTERNS.value]
    # Too few shared features to compare at all: no neighbourhood is formed.
    assert patterns.samples == 0


async def test_all_eleven_dimensions_are_always_reported() -> None:
    """A dimension that silently disappears is one nobody notices is missing."""
    candidate = await enrich(_context())
    assert {p.dimension for p in candidate.enrichment.priors} == set(HistoryDimension)


async def test_the_tokens_own_history_is_its_own_dimension() -> None:
    history = FakeHistory(
        tuple(_lesson(positive=False, subject=str(_TOKEN.mint)) for _ in range(10))
    )
    candidate = await enrich(_context(), history)
    outcomes = candidate.enrichment.by_dimension[HistoryDimension.OUTCOMES.value]
    assert outcomes.samples == 10
    assert outcomes.raw_positive_rate == 0.0


async def test_wallet_familiarity_is_marked_as_observation_not_track_record() -> None:
    """An opinion about a wallet is not a result from one, and the basis says so."""
    wallets = tuple(f"W{i:0>43}" for i in range(4))
    observations = {
        wallet: (
            HistoricalRecord(
                subject=wallet,
                source="wallet_intelligence",
                kind="assessment",
                features={"trust": 80.0, "risk": 15.0},
            ),
        )
        for wallet in wallets
    }
    history = FakeHistory(observations=observations)
    candidate = await enrich(
        _context(identity=CandidateIdentity(mint=str(_TOKEN.mint), wallets=wallets)), history
    )
    prior = candidate.enrichment.by_dimension[HistoryDimension.WALLETS.value]
    assert prior.basis is EvidenceBasis.OBSERVATIONS
    assert prior.positive_rate > 0.5
    # Observations never count toward "how many comparable examples exist".
    assert candidate.enrichment.sample_support == 0.0


async def test_sample_support_measures_this_candidate_not_the_platform() -> None:
    """The confidence factor documented as "similar historical examples" finally
    answers its own question instead of returning a configured constant."""
    history = FakeHistory(
        tuple(_lesson(positive=i % 2 == 0, tags={"developer": _DEV}) for i in range(30))
    )
    candidate = await enrich(_context(), history)
    assert candidate.enrichment.sample_support > 0.0

    manager = _manager()
    informed = await manager.evaluate(candidate)
    blind = await manager.evaluate(await enrich(_context()))
    assert informed.confidence.sample_support > blind.confidence.sample_support


async def test_history_is_stated_in_the_explanation_not_only_applied() -> None:
    """A prior that moves a number without appearing in the account of why is
    exactly the black box this context refuses to be."""
    history = FakeHistory(
        tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(20))
    )
    prediction = await _manager().evaluate(await enrich(_context(), history))
    assert prediction.explanation is not None
    lines = prediction.explanation.drivers + prediction.explanation.risks
    assert any("history" in line for line in lines)


async def test_a_platform_without_precedent_says_so() -> None:
    prediction = await _manager().evaluate(await enrich(_context()))
    assert prediction.explanation is not None
    assert any("no comparable history" in caveat for caveat in prediction.explanation.caveats)


async def test_history_features_are_injected_only_when_they_mean_something() -> None:
    """A ``history.*`` key at a neutral value would be a fabricated measurement
    that a future training run would learn from."""
    blind = await enrich(_context())
    assert history_feature_values(blind.enrichment) == {}

    history = FakeHistory(
        tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(20))
    )
    informed = await enrich(_context(), history)
    values = history_feature_values(informed.enrichment)
    assert "history.prior" in values
    assert f"history.{HistoryDimension.DEVELOPER.value}.rate" in values
    assert all(0.0 <= v <= 1.0 for v in values.values())


async def test_the_enrichment_survives_persistence() -> None:
    """A verdict is only auditable next to the memory that informed it, so the
    enrichment has to round-trip through the store, not just live in memory."""
    from hades.contexts.learning.infrastructure.serializers import (
        prediction_from_json,
        prediction_to_json,
    )

    history = FakeHistory(
        tuple(_lesson(positive=True, tags={"developer": _DEV}) for _ in range(20))
    )
    prediction = await _manager().evaluate(await enrich(_context(), history))
    restored = prediction_from_json(prediction_to_json(prediction))
    assert restored.enrichment is not None
    assert restored.enrichment.identity.developer == _DEV
    assert restored.enrichment.prior_log_odds == prediction.enrichment.prior_log_odds  # type: ignore[union-attr]
    assert HistoryDimension.DEVELOPER.value in restored.enrichment.informative_dimensions


# --- 4. failure modes --------------------------------------------------------


async def test_an_unreachable_memory_never_stops_a_token_being_judged() -> None:
    candidate = await enrich(_context(), FakeHistory(fail=True))
    assert candidate.enrichment.prior_log_odds == 0.0
    assert candidate.enrichment.evidence_available is False
    # And it is distinguishable from an empty memory, which is the whole point.
    assert any("unavailable" in note for note in candidate.enrichment.notes)
    prediction = await _manager().evaluate(candidate)
    assert prediction is not None


async def test_a_candidate_with_no_identity_still_gets_enriched() -> None:
    """Identity is best-effort upstream; its absence must narrow the enrichment,
    never skip it."""
    candidate = await enrich(_context(identity=None))
    assert candidate.enrichment.identity.mint == str(_TOKEN.mint)
    assert len(candidate.enrichment.priors) == len(HistoryDimension)


# --- narrative classification ------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Baby Doge Coin", "doge"),
        ("PEPE", "pepe"),
        ("Moon Rocket", "space"),
        ("TRUMP 2024", "politics"),
        ("Zqxwv", None),
        ("", None),
    ],
)
def test_narrative_classification(name: str, expected: str | None) -> None:
    assert narrative_of(name, None, None) == expected


def test_narrative_does_not_match_across_word_boundaries() -> None:
    """A substring rule alone makes CATALYST a cat coin — and a wrong cohort is
    worse than a missing one, because nothing downstream can detect it."""
    assert narrative_of("Catalyst Protocol", None, None) != "cat"
