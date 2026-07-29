"""The Candidate Enricher — the committee never starts from zero again.

Until now, every token arrived at the AI Committee as if the platform had never
seen a token before. The specialists read a feature vector, the meta-model fused
their opinions from a fixed bias, and *everything the platform had lived through
— every settled trade, every developer it had learned to distrust, every
narrative that had never once worked — was absent from the calculation*. The
memory existed (Phase 1) and was fed by research (Phase 2); nothing read it on
the decision path.

That is the gap this module closes. Between the Decision Context Builder and the
committee sits one mandatory stage: for each candidate it establishes *who this
token is* (developer, wallets, clusters, narrative, launchpad, liquidity band,
volatility band, holder profile, similar past patterns), asks the Knowledge
Engine what happened the last time the platform met something like it, and
attaches the answer.

Four rules keep it honest — each one exists because the obvious version of this
component is dangerous:

1. **An empty memory is exactly neutral.** With no evidence, every prior has
   ``strength`` 0, ``prior_log_odds`` is 0.0, and the committee produces the
   number it produced before this module existed. Enrichment can only ever be
   the platform *using* what it knows; it can never be a disguised recalibration
   of thresholds.
2. **Evidence is shrunk toward ignorance.** A cohort of one winning trade is not
   a 100% cohort. Every rate is pulled toward 0.5 by a pseudo-count, so a prior
   earns influence only in proportion to how much it is actually built on.
3. **The nudge is bounded.** ``max_prior_log_odds`` caps the total influence of
   history on the fused probabilities. History informs the committee; it must
   never be able to overrule the present state of a token, because the one thing
   a memory cannot know is what has changed since.
4. **Consulted-and-empty is recorded as such.** ``evidence_available=False`` is
   a distinct, visible state from "never enriched". The first is a young
   platform; the second is a wiring defect — and the entire cost of the last
   audit was that the two looked identical.

The enricher takes no decision, sizes nothing, and cannot reject a candidate.
Its whole output is context.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hades.contexts.learning.application.feature_catalog import FeatureNormalizer
from hades.contexts.learning.application.mathx import clamp, logit, mean
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.domain.models import (
    CandidateEnrichment,
    CandidateIdentity,
    DecisionContext,
    EnrichedCandidate,
    EvidenceBasis,
    HistoricalLesson,
    HistoricalPrior,
    HistoricalRecord,
    HistoryDimension,
)
from hades.contexts.learning.domain.ports import CandidateHistoryPort
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.enricher")

_D = HistoryDimension

#: Feature keys used to describe a candidate's holder profile. Deliberately a
#: short, named list: a holder cohort built from "whatever holder.* keys happen
#: to exist" would silently change meaning whenever the Feature Engine gained a
#: feature, and cohorts must stay comparable across years.
_HOLDER_KEYS: tuple[str, ...] = (
    "holders.count",
    "holders.top10_share_pct",
    "holders.gini",
)
_LIQUIDITY_KEY = "basic.liquidity"
_VOLATILITY_KEY = "tech.volatility"


@dataclass(frozen=True)
class EnrichmentPolicy:
    """Every tunable of the enricher, in one auditable place.

    ``weights`` is the relative say each dimension has in the combined nudge.
    Ground-truth-heavy dimensions (a developer's settled trades, the token's own
    prior outcomes, the nearest historical patterns) outrank the softer cohorts,
    and no single dimension can dominate because the total is bounded anyway.
    """

    #: How many settled lessons to consider. The whole history is small for a
    #: long time; when it stops being small this bounds the work per candidate.
    lesson_window: int = 5_000
    #: Pseudo-count pulling every cohort rate toward 0.5. Higher = more sceptical.
    shrinkage: float = 8.0
    #: Hard cap on the total historical nudge, in logits (~±0.24 in probability
    #: around the middle of the curve at 1.0).
    max_prior_log_odds: float = 1.0
    #: Comparable samples at which per-candidate ``sample_support`` saturates.
    support_target: int = 60
    #: Neighbourhood size for the "similar patterns" dimension.
    neighbours: int = 25
    #: A cohort smaller than this is reported but contributes nothing.
    min_cohort: int = 3
    #: Relative half-width of the liquidity / volatility bands (log space).
    liquidity_band: float = 0.75
    volatility_band: float = 0.50
    #: How many of the candidate's wallets to look up. Bounded: the enricher is
    #: on the hot path of a firehose.
    max_wallet_lookups: int = 12
    weights: dict[str, float] = field(
        default_factory=lambda: {
            _D.OUTCOMES.value: 1.0,
            _D.PATTERNS.value: 1.0,
            _D.DEVELOPER.value: 0.9,
            _D.CLUSTERS.value: 0.7,
            _D.NARRATIVE.value: 0.6,
            _D.LAUNCHPAD.value: 0.5,
            _D.STRATEGIES.value: 0.5,
            _D.LIQUIDITY.value: 0.4,
            _D.VOLATILITY.value: 0.4,
            _D.HOLDERS.value: 0.4,
            _D.WALLETS.value: 0.3,
        }
    )


class KnowledgeCandidateEnricher:
    """Enriches a candidate with everything the memory already knows about it."""

    def __init__(
        self,
        history: CandidateHistoryPort,
        normalizer: FeatureNormalizer,
        policy: EnrichmentPolicy | None = None,
        metrics: LearningMetrics | None = None,
    ) -> None:
        self._history = history
        self._normalizer = normalizer
        self._policy = policy or EnrichmentPolicy()
        self._metrics = metrics

    async def enrich(self, context: DecisionContext) -> EnrichedCandidate:
        """Attach historical context to a candidate. Never raises, never rejects."""
        started = time.perf_counter()
        identity = context.identity or CandidateIdentity(mint=str(context.token.mint))
        try:
            lessons = await self._history.lessons(limit=self._policy.lesson_window)
            records = await self._wallet_records(identity)
        except Exception as exc:
            # The memory being unreachable must not stop a token being judged —
            # but it must also never look like an empty memory, because "we know
            # nothing about this token" and "we could not ask" call for opposite
            # reactions from whoever reads the audit trail.
            _logger.warning("enrichment_unavailable", mint=identity.mint, error=str(exc))
            self._meter("unavailable", started, None)
            return EnrichedCandidate(
                context=context,
                enrichment=CandidateEnrichment.empty(
                    identity, note=f"knowledge unavailable: {exc}"
                ),
            )

        priors = self._priors(context, identity, lessons, records)
        informative = [p for p in priors if p.is_informative]
        enrichment = CandidateEnrichment(
            identity=identity,
            priors=tuple(priors),
            prior_log_odds=self._combine(informative),
            sample_support=self._support(informative),
            lessons_considered=len(lessons),
            observations_considered=len(records),
            evidence_available=bool(informative),
            notes=self._notes(identity, informative),
            enriched_at=datetime.now(UTC),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
        self._meter("found" if informative else "empty", started, enrichment)
        return EnrichedCandidate(context=context, enrichment=enrichment)

    def _meter(self, evidence: str, started: float, enrichment: CandidateEnrichment | None) -> None:
        """Make the enrichment visible.

        ``evidence`` separates the three states that look alike from outside and
        mean entirely different things: *found* (the memory had precedent),
        *empty* (it was asked and had none — a young platform), and
        *unavailable* (it could not be asked — a broken one).
        """
        if self._metrics is None:
            return
        self._metrics.enrichments.labels(evidence=evidence).inc()
        self._metrics.enrichment_seconds.observe(time.perf_counter() - started)
        if enrichment is not None:
            self._metrics.last_prior_log_odds.set(enrichment.prior_log_odds)
            self._metrics.last_enrichment_samples.set(enrichment.total_samples)

    # -- the eleven dimensions ------------------------------------------------

    def _priors(
        self,
        context: DecisionContext,
        identity: CandidateIdentity,
        lessons: Sequence[HistoricalLesson],
        records: Sequence[HistoricalRecord],
    ) -> tuple[HistoricalPrior, ...]:
        neighbourhood = self._neighbourhood(context, lessons)
        return (
            self._tag_prior(_D.DEVELOPER, "developer", identity.developer, lessons),
            self._wallet_prior(identity, records),
            self._tag_prior(_D.CLUSTERS, "cluster", identity.cluster_id, lessons),
            self._tag_prior(_D.NARRATIVE, "narrative", identity.narrative, lessons),
            self._tag_prior(_D.LAUNCHPAD, "launchpad", identity.launchpad, lessons),
            self._band_prior(
                _D.LIQUIDITY, _LIQUIDITY_KEY, self._policy.liquidity_band, context, lessons
            ),
            self._band_prior(
                _D.VOLATILITY, _VOLATILITY_KEY, self._policy.volatility_band, context, lessons
            ),
            self._token_prior(identity, lessons),
            self._strategy_prior(identity, lessons, neighbourhood),
            self._holder_prior(context, lessons),
            self._pattern_prior(neighbourhood),
        )

    def _tag_prior(
        self,
        dimension: HistoryDimension,
        tag: str,
        value: str | None,
        lessons: Sequence[HistoricalLesson],
    ) -> HistoricalPrior:
        """Outcomes of past decisions sharing one cohort key."""
        if not value:
            return HistoricalPrior(dimension=dimension, detail=f"{tag} unknown for this candidate")
        cohort = [lesson for lesson in lessons if lesson.tags.get(tag) == value]
        return self._from_outcomes(dimension, value, cohort, f"prior trades sharing this {tag}")

    def _wallet_prior(
        self, identity: CandidateIdentity, records: Sequence[HistoricalRecord]
    ) -> HistoricalPrior:
        """How much of this token's wallet crowd the platform already knows.

        This one is observation-based and says so. Wallet outcomes are not
        settled trades of *ours* — the platform has opinions about wallets, not
        results from them — so the prior reports familiarity and the recorded
        trust/risk balance, and is marked :attr:`EvidenceBasis.OBSERVATIONS` so
        it is never mistaken for a track record.
        """
        if not identity.wallets:
            return HistoricalPrior(
                dimension=_D.WALLETS, detail="no wallets identified for this candidate"
            )
        if not records:
            return HistoricalPrior(
                dimension=_D.WALLETS,
                key=f"{len(identity.wallets)} wallets",
                detail="none of this token's wallets has been seen before",
            )
        known = {record.subject for record in records}
        trust = [
            record.features["trust"] / 100.0
            for record in records
            if isinstance(record.features.get("trust"), float)
        ]
        risk = [
            record.features["risk"] / 100.0
            for record in records
            if isinstance(record.features.get("risk"), float)
        ]
        # A crowd the memory knows and trusts reads above 0.5; one it knows and
        # distrusts reads below. With neither signal recorded it stays at 0.5 —
        # familiarity alone is not a verdict.
        balance = 0.5
        if trust or risk:
            balance = clamp(0.5 + 0.5 * (mean(trust) - mean(risk)))
        familiarity = len(known) / max(1, len(identity.wallets))
        samples = len(records)
        return HistoricalPrior(
            dimension=_D.WALLETS,
            key=f"{len(known)}/{len(identity.wallets)} known",
            basis=EvidenceBasis.OBSERVATIONS,
            samples=samples,
            positive_rate=round(clamp(0.5 + (balance - 0.5) * familiarity), 4),
            raw_positive_rate=round(balance, 4),
            avg_roi=0.0,
            strength=round(self._strength(samples) * familiarity, 4),
            detail=f"{len(known)} of {len(identity.wallets)} wallets already in the memory",
        )

    def _band_prior(
        self,
        dimension: HistoryDimension,
        key: str,
        band: float,
        context: DecisionContext,
        lessons: Sequence[HistoricalLesson],
    ) -> HistoricalPrior:
        """Outcomes of past decisions taken in the same liquidity/volatility band.

        The band is relative and computed in log space: "twice as deep" and
        "half as deep" are the same distance, which is the right geometry for a
        quantity that spans four orders of magnitude across meme coins.
        """
        value = context.vector.raw.get(key)
        if value is None:
            return HistoricalPrior(dimension=dimension, detail=f"{key} not measured")
        reference = math.log1p(max(0.0, value))
        cohort = [
            lesson
            for lesson in lessons
            if (other := lesson.features.get(key)) is not None
            and abs(math.log1p(max(0.0, other)) - reference) <= band
        ]
        return self._from_outcomes(
            dimension,
            f"{key}≈{round(value, 4)}",
            cohort,
            "prior trades in a comparable band",
        )

    def _token_prior(
        self, identity: CandidateIdentity, lessons: Sequence[HistoricalLesson]
    ) -> HistoricalPrior:
        """The token's own settled history. The most direct evidence there is."""
        cohort = [lesson for lesson in lessons if lesson.subject == identity.mint]
        return self._from_outcomes(
            _D.OUTCOMES, identity.mint, cohort, "this exact token's own settled trades"
        )

    def _strategy_prior(
        self,
        identity: CandidateIdentity,
        lessons: Sequence[HistoricalLesson],
        neighbourhood: Sequence[tuple[float, HistoricalLesson]],
    ) -> HistoricalPrior:
        """How the strategy behind comparable decisions has actually performed.

        When the candidate arrives without a strategy tag (the committee runs
        before any strategy claims it), the question is answered the only honest
        way available: take the strategy that dominates the *similar* past
        decisions and report that strategy's record.
        """
        strategy = identity.strategy
        if not strategy:
            counts: dict[str, int] = {}
            for _, lesson in neighbourhood:
                tag = lesson.tags.get("strategy")
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
            if not counts:
                return HistoricalPrior(
                    dimension=_D.STRATEGIES,
                    detail="no strategy attribution on comparable decisions",
                )
            strategy = max(counts.items(), key=lambda kv: kv[1])[0]
        cohort = [lesson for lesson in lessons if lesson.tags.get("strategy") == strategy]
        return self._from_outcomes(
            _D.STRATEGIES, strategy, cohort, "settled trades from a similar strategy"
        )

    def _holder_prior(
        self, context: DecisionContext, lessons: Sequence[HistoricalLesson]
    ) -> HistoricalPrior:
        """Outcomes of past decisions with a similar holder structure."""
        target = {k: v for k in _HOLDER_KEYS if (v := context.vector.values.get(k)) is not None}
        if not target:
            return HistoricalPrior(dimension=_D.HOLDERS, detail="holder structure not measured")
        cohort: list[HistoricalLesson] = []
        for lesson in lessons:
            other = self._normalized(lesson)
            shared = [k for k in target if k in other]
            if not shared:
                continue
            distance = mean(abs(target[k] - other[k]) for k in shared)
            if distance <= 0.15:
                cohort.append(lesson)
        return self._from_outcomes(
            _D.HOLDERS, "similar holder structure", cohort, "prior trades on similar holder bases"
        )

    def _pattern_prior(
        self, neighbourhood: Sequence[tuple[float, HistoricalLesson]]
    ) -> HistoricalPrior:
        """What happened the last times the platform saw a token *like this one*.

        This is the dimension that most directly answers "have we been here
        before?". Similarity is mean absolute distance over the features the two
        vectors share, in the normalised space the models themselves use.
        """
        if not neighbourhood:
            return HistoricalPrior(
                dimension=_D.PATTERNS, detail="no comparable decision in memory yet"
            )
        cohort = [lesson for _, lesson in neighbourhood]
        prior = self._from_outcomes(
            _D.PATTERNS,
            f"{len(cohort)} nearest",
            cohort,
            "the most similar decisions the platform has settled",
        )
        similarity = clamp(1.0 - mean(distance for distance, _ in neighbourhood))
        # A "nearest" neighbourhood that is not actually near says little, so the
        # prior's influence is scaled by how close the neighbours really are.
        return prior.model_copy(
            update={
                "strength": round(prior.strength * similarity, 4),
                "detail": f"{prior.detail} (mean similarity {round(similarity, 3)})",
            }
        )

    # -- shared machinery -----------------------------------------------------

    def _neighbourhood(
        self, context: DecisionContext, lessons: Sequence[HistoricalLesson]
    ) -> tuple[tuple[float, HistoricalLesson], ...]:
        target = context.vector.values
        if not target:
            return ()
        scored: list[tuple[float, HistoricalLesson]] = []
        for lesson in lessons:
            other = self._normalized(lesson)
            shared = [k for k in target if k in other]
            # Fewer than a handful of shared features is not a comparison, it is
            # a coincidence; such a "neighbour" would import an unrelated
            # outcome as if it were precedent.
            if len(shared) < 5:
                continue
            scored.append((mean(abs(target[k] - other[k]) for k in shared), lesson))
        scored.sort(key=lambda kv: kv[0])
        return tuple(scored[: self._policy.neighbours])

    def _from_outcomes(
        self,
        dimension: HistoryDimension,
        key: str,
        cohort: Sequence[HistoricalLesson],
        detail: str,
    ) -> HistoricalPrior:
        """Fold a cohort of settled lessons into a shrunk, bounded prior."""
        samples = len(cohort)
        if samples == 0:
            return HistoricalPrior(
                dimension=dimension, key=key, detail=f"no {detail.lower()} recorded yet"
            )
        positives = sum(1 for lesson in cohort if lesson.label_roi_positive)
        raw_rate = positives / samples
        shrunk = (positives + 0.5 * self._policy.shrinkage) / (samples + self._policy.shrinkage)
        strength = self._strength(samples)
        return HistoricalPrior(
            dimension=dimension,
            key=key,
            basis=EvidenceBasis.OUTCOMES,
            samples=samples,
            positive_rate=round(clamp(shrunk), 4),
            raw_positive_rate=round(clamp(raw_rate), 4),
            avg_roi=round(mean(lesson.realized_roi for lesson in cohort), 6),
            strength=round(strength, 4),
            detail=f"{samples} {detail} · {positives} profitable",
        )

    def _normalized(self, lesson: HistoricalLesson) -> dict[str, float]:
        """The lesson's vector in model space, normalising only if the adapter
        did not already (which is the case in tests and for hand-built lessons)."""
        if lesson.normalized:
            return lesson.normalized
        return self._normalizer.normalize_values(lesson.features)

    def _strength(self, samples: int) -> float:
        """Evidence-proportional influence: 0 at no samples, →1 asymptotically.

        Below ``min_cohort`` the prior is *reported* but silent. A three-trade
        cohort is worth showing an operator and is not worth moving a
        probability with.
        """
        if samples < self._policy.min_cohort:
            return 0.0
        return clamp(samples / (samples + self._policy.shrinkage))

    def _combine(self, priors: Sequence[HistoricalPrior]) -> float:
        """Fuse the informative priors into one bounded log-odds nudge."""
        if not priors:
            return 0.0
        weights = self._policy.weights
        total_weight = 0.0
        acc = 0.0
        for prior in priors:
            weight = weights.get(prior.dimension.value, 0.0) * prior.strength
            if weight <= 0.0:
                continue
            acc += weight * logit(prior.positive_rate)
            total_weight += weight
        if total_weight <= 0.0:
            return 0.0
        # The mean (not the sum) of the per-dimension log-odds: eleven agreeing
        # cohorts are *more certain*, not eleven times more extreme, and summing
        # would let a broad but weak consensus overwhelm the token in front of us.
        combined = acc / total_weight
        cap = self._policy.max_prior_log_odds
        return round(max(-cap, min(cap, combined)), 6)

    def _support(self, priors: Sequence[HistoricalPrior]) -> float:
        """Per-candidate ``sample_support`` — comparable examples, not a constant.

        The confidence engine documents this factor as "how many similar
        historical examples exist". It was a configured number; here it finally
        answers its own question, counting only ground-truth cohorts (an
        observation is not an example of an outcome).
        """
        samples = sum(prior.samples for prior in priors if prior.basis is EvidenceBasis.OUTCOMES)
        return round(clamp(samples / max(1, self._policy.support_target)), 4)

    @staticmethod
    def _notes(identity: CandidateIdentity, priors: Sequence[HistoricalPrior]) -> tuple[str, ...]:
        if not priors:
            return (f"memory consulted for {identity.mint}: nothing comparable recorded yet",)
        return tuple(f"{prior.dimension.value}: {prior.detail}" for prior in priors)

    # -- wallets --------------------------------------------------------------

    async def _wallet_records(self, identity: CandidateIdentity) -> tuple[HistoricalRecord, ...]:
        out: list[HistoricalRecord] = []
        for wallet in identity.wallets[: self._policy.max_wallet_lookups]:
            out.extend(await self._history.observations(wallet, limit=5))
        return tuple(out)


__all__ = ["EnrichmentPolicy", "KnowledgeCandidateEnricher"]
