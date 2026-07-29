"""Knowledge runtime — the anti-corruption layer, and the only place it lives.

The Knowledge context deliberately imports nothing from the contexts it learns
from. This module is why that is possible: it subscribes to the platform's
events and translates each into a :class:`KnowledgeEnvelope`, the one shape
Knowledge accepts.

**Subscription is by event *name*, not by class.** Nothing here imports
``execution``, ``portfolio`` or ``risk`` either, so the isolation the context
promises is not quietly reintroduced by its own wiring. The obvious objection —
that a renamed event would silently stop being recorded — is answered by
``tests/test_knowledge_isolation.py``, which imports every event class in the
platform (a test may) and asserts that each name below still resolves. Drift
breaks the build; it does not break production silently.

**The decision pipeline is the part that matters.** Four events, in order,
close the learning loop:

    FeaturesComputed        → remember the vector for this mint
    TradeApproved           → FREEZE that vector: this is the evidence
    PositionOpened          → the frozen evidence gets its reference
    PositionClosed          → settle → a Lesson with ground-truth labels

Freezing at approval rather than reading features at settlement is the whole
game. The lazy version — wait for the close, then ask the feature store what the
token looks like — trains on the state of the world at the moment of *sale*
labelled with the result of the trade, which is temporal leakage: excellent
offline metrics, a model that cannot work. Here there is no feature store to ask
at settle time, so the leaking implementation is not available to write.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Final

from hades.bootstrap import Container
from hades.contexts.knowledge.application.journal import DecisionJournal
from hades.contexts.knowledge.application.metrics import KnowledgeMetrics
from hades.contexts.knowledge.application.recorder import KnowledgeRecorder
from hades.contexts.knowledge.domain.models import (
    Decision,
    KnowledgeEnvelope,
    KnowledgeKind,
    KnowledgeSource,
    Outcome,
    SubjectType,
    Verification,
)
from hades.contexts.knowledge.domain.ports import (
    DecisionJournalStore,
    KnowledgeStore,
    LessonStore,
)
from hades.contexts.knowledge.infrastructure.stores import (
    InMemoryDecisionJournalStore,
    InMemoryKnowledgeStore,
    InMemoryLessonStore,
    PostgresDecisionJournalStore,
    PostgresKnowledgeStore,
    PostgresLessonStore,
)
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

_logger = get_logger("knowledge.runtime")

KNOWLEDGE_STATUS_NAMESPACE: Final = "knowledge"
STATUS_KEY: Final = "status"
_STATUS_INTERVAL_SECONDS: Final = 5.0
_STATUS_TTL_SECONDS: Final = 30

#: Event names the decision pipeline needs, kept as constants so the isolation
#: test can assert every one of them still names a real event class.
EVT_FEATURES_COMPUTED: Final = "FeaturesComputed"
EVT_COMMITTEE_PREDICTION: Final = "CommitteePredictionGenerated"
EVT_TRADE_APPROVED: Final = "TradeApproved"
EVT_POSITION_OPENED: Final = "PositionOpened"
EVT_POSITION_CLOSED: Final = "PositionClosed"

#: How many mints keep a pending feature vector / approval in memory. Bounded
#: because the Scanner is a firehose: an unbounded cache here would be a slow
#: memory leak in the one process that hosts the entire pipeline.
_CACHE_LIMIT: Final = 4_096


class _BoundedCache:
    """Small LRU keyed by mint. Eviction is silent and safe: a miss simply
    means the decision is recorded without that evidence, never a crash."""

    def __init__(self, limit: int = _CACHE_LIMIT) -> None:
        self._limit = limit
        self._rows: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._rows[key] = value
        self._rows.move_to_end(key)
        while len(self._rows) > self._limit:
            self._rows.popitem(last=False)

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._rows.get(key)
        if row is not None:
            self._rows.move_to_end(key)
        return row

    def take(self, key: str) -> dict[str, Any] | None:
        return self._rows.pop(key, None)

    def __len__(self) -> int:
        return len(self._rows)


def _mint_of(payload: dict[str, Any]) -> str | None:
    """Pull a mint address out of a serialised ``TokenRef``, defensively.

    Reaching into a payload by key is the price of not importing the producing
    context. It is guarded rather than trusted: a shape change yields ``None``
    and a skipped record, which the ingestion counters make visible.
    """
    token = payload.get("token")
    if isinstance(token, dict):
        mint = token.get("mint")
        if isinstance(mint, dict):
            address = mint.get("address")
            if isinstance(address, str) and address:
                return address
        if isinstance(mint, str) and mint:
            return mint
    return None


def _money(payload: dict[str, Any], key: str) -> float | None:
    """Read a serialised ``Money`` as a float, or ``None`` if it is not one."""
    raw = payload.get(key)
    if isinstance(raw, dict):
        raw = raw.get("amount")
    if isinstance(raw, (int, float, str)):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value == value else None  # reject NaN
    return None


def _numbers(payload: dict[str, Any]) -> dict[str, float]:
    """Flatten the numeric leaves of a payload into named features."""
    out: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, bool):
            out[key] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)) and value == value:
            out[key] = float(value)
    return out


class KnowledgeRuntime:
    """Wires permanent memory to every producer on the platform."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._metrics = KnowledgeMetrics(container.metrics)
        self._cache = CacheService(container.redis, namespace=KNOWLEDGE_STATUS_NAMESPACE)

        self._store: KnowledgeStore = self._build_store()
        self._lessons: LessonStore = self._build_lesson_store()
        self._journal_store: DecisionJournalStore = self._build_journal_store()

        self._recorder = KnowledgeRecorder(
            self._store, event_bus=container.event_bus, metrics=self._metrics
        )
        self._journal = DecisionJournal(
            self._journal_store,
            self._lessons,
            event_bus=container.event_bus,
            metrics=self._metrics,
        )

        # Decision-pipeline working memory (see the module docstring).
        self._features = _BoundedCache()
        self._beliefs = _BoundedCache()
        self._approved = _BoundedCache()
        self._positions = _BoundedCache()
        # Cohort keys (developer / launchpad / narrative / cluster) as the
        # committee established them, so a settled lesson can be attributed and
        # therefore learned from as a cohort later.
        self._identities = _BoundedCache()

        self._register()

    # -- construction ---------------------------------------------------------

    def _build_store(self) -> KnowledgeStore:
        if self._c.database is not None:
            return PostgresKnowledgeStore(self._c.database)
        return InMemoryKnowledgeStore()

    def _build_lesson_store(self) -> LessonStore:
        if self._c.database is not None:
            return PostgresLessonStore(self._c.database)
        return InMemoryLessonStore()

    def _build_journal_store(self) -> DecisionJournalStore:
        if self._c.database is not None:
            return PostgresDecisionJournalStore(self._c.database)
        return InMemoryDecisionJournalStore()

    # -- the translation table ------------------------------------------------
    #
    # (event name) → (source, kind, verification). A row here is the entire
    # statement "this event is knowledge of that sort, from that producer, known
    # that strongly". Everything else about ingestion is generic.

    _OBSERVED: Final[
        dict[str, tuple[KnowledgeSource, KnowledgeKind, Verification, SubjectType]]
    ] = {
        # Scanner — what the market said. Third-party assertions, believed.
        "TokenDiscovered": (
            KnowledgeSource.SCANNER,
            KnowledgeKind.OBSERVATION,
            Verification.REPORTED,
            SubjectType.TOKEN,
        ),
        "TokenMetadataCollected": (
            KnowledgeSource.SCANNER,
            KnowledgeKind.OBSERVATION,
            Verification.REPORTED,
            SubjectType.TOKEN,
        ),
        "DataQualityAnomalyDetected": (
            KnowledgeSource.SCANNER,
            KnowledgeKind.OBSERVATION,
            Verification.UNVERIFIED,
            SubjectType.TOKEN,
        ),
        # Security Engine — judgements derived from on-chain measurement.
        "SecurityScoreComputed": (
            KnowledgeSource.SECURITY,
            KnowledgeKind.ASSESSMENT,
            Verification.SIMULATED,
            SubjectType.TOKEN,
        ),
        "TokenApproved": (
            KnowledgeSource.SECURITY,
            KnowledgeKind.ASSESSMENT,
            Verification.SIMULATED,
            SubjectType.TOKEN,
        ),
        "TokenRejected": (
            KnowledgeSource.SECURITY,
            KnowledgeKind.ASSESSMENT,
            Verification.SIMULATED,
            SubjectType.TOKEN,
        ),
        # Wallet Intelligence — the on-chain knowledge base's own conclusions.
        "WalletIntelligenceComputed": (
            KnowledgeSource.WALLET_INTELLIGENCE,
            KnowledgeKind.ASSESSMENT,
            Verification.SIMULATED,
            SubjectType.TOKEN,
        ),
        "SmartMoneyDetected": (
            KnowledgeSource.WALLET_INTELLIGENCE,
            KnowledgeKind.ASSESSMENT,
            Verification.SIMULATED,
            SubjectType.WALLET,
        ),
        "ReputationUpdated": (
            KnowledgeSource.WALLET_INTELLIGENCE,
            KnowledgeKind.ASSESSMENT,
            Verification.SIMULATED,
            SubjectType.WALLET,
        ),
        # AI Committee — statements about the future. Never ground truth.
        "CommitteePredictionGenerated": (
            KnowledgeSource.COMMITTEE,
            KnowledgeKind.PREDICTION,
            Verification.SIMULATED,
            SubjectType.TOKEN,
        ),
        # Shadow trading — virtual, zero capital. True about the model only.
        "ShadowStrategyUpdated": (
            KnowledgeSource.SHADOW_TRADING,
            KnowledgeKind.SIMULATION,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        # Research Lab — findings about the platform, not about a token.
        "ExperimentFinished": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.PLATFORM,
        ),
        "ResearchReportGenerated": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.PLATFORM,
        ),
        "CandidateProposed": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.MODEL,
        ),
        "FeatureProposed": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.PLATFORM,
        ),
        "BacktestCompleted": (
            KnowledgeSource.BACKTEST,
            KnowledgeKind.SIMULATION,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        "WalkForwardCompleted": (
            KnowledgeSource.WALK_FORWARD,
            KnowledgeKind.SIMULATION,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        "MonteCarloCompleted": (
            KnowledgeSource.MONTE_CARLO,
            KnowledgeKind.SIMULATION,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        "ReplayCompleted": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.SIMULATION,
            Verification.SIMULATED,
            SubjectType.PLATFORM,
        ),
        # Comparisons and promotion decisions are findings too — arguably the
        # most valuable ones, because they are the lab saying which of its own
        # results it believes. Leaving them unrecorded meant the memory held the
        # experiments but not the conclusions drawn from them.
        "StrategyCompared": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        "ModelCompared": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.MODEL,
        ),
        # A promotion decision is governance, not deployment: the event says a
        # human cleared a bar, and nothing is deployed by recording it.
        "ResearchStrategyPromoted": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        "PromotionRejected": (
            KnowledgeSource.RESEARCH_LAB,
            KnowledgeKind.EXPERIMENT,
            Verification.SIMULATED,
            SubjectType.STRATEGY,
        ),
        # Executed operations — settled reality. Paper fills are simulated, but
        # the price path they settled against was the real market, so a closed
        # paper trade is ground truth about the market's behaviour.
        "OrderFilled": (
            KnowledgeSource.EXECUTED_TRADE,
            KnowledgeKind.OUTCOME,
            Verification.REALISED,
            SubjectType.TOKEN,
        ),
        "PositionClosed": (
            KnowledgeSource.PAPER_TRADING,
            KnowledgeKind.OUTCOME,
            Verification.REALISED,
            SubjectType.TOKEN,
        ),
    }

    def _register(self) -> None:
        bus = self._c.event_bus
        for name in self._OBSERVED:
            bus.subscribe(name, self._on_observed)
        # The decision pipeline. PositionClosed is subscribed twice on purpose:
        # once as an observation (what happened) and once as a settlement (what
        # it teaches). They are different concerns with different failure modes
        # and collapsing them would couple the memory to the loop.
        bus.subscribe(EVT_FEATURES_COMPUTED, self._on_features)
        bus.subscribe(EVT_COMMITTEE_PREDICTION, self._on_prediction)
        bus.subscribe(EVT_TRADE_APPROVED, self._on_trade_approved)
        bus.subscribe(EVT_POSITION_OPENED, self._on_position_opened)
        bus.subscribe(EVT_POSITION_CLOSED, self._on_position_closed)

    # -- generic ingestion ----------------------------------------------------

    async def _on_observed(self, event: DomainEvent) -> None:
        row = self._OBSERVED.get(event.event_type)
        if row is None:
            return
        source, kind, verification, subject_type = row
        try:
            envelope = event.to_envelope()
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                payload = {"value": payload}
            subject = _mint_of(payload) or str(envelope["aggregate_id"])
            await self._recorder.record(
                [
                    KnowledgeEnvelope(
                        source=source,
                        kind=kind,
                        verification=verification,
                        subject=subject,
                        subject_type=subject_type,
                        occurred_at=event.occurred_at,
                        payload=payload,
                        features=_numbers(payload),
                        correlation_id=str(envelope["correlation_id"] or "") or None,
                    )
                ]
            )
        except Exception as exc:
            # Ingestion must never take down a producer: this handler runs on the
            # publisher's task. It is logged, not swallowed — a memory that stops
            # recording without saying so is the exact failure this context was
            # built to end.
            _logger.warning("knowledge_ingest_failed", event_type=event.event_type, error=str(exc))

    # -- the decision pipeline ------------------------------------------------

    async def _on_features(self, event: DomainEvent) -> None:
        """Remember the latest vector per mint. Not yet evidence — just current."""
        try:
            payload = event.to_envelope()["payload"]
            if not isinstance(payload, dict):
                return
            mint = _mint_of(payload)
            features = payload.get("features")
            if mint is None or not isinstance(features, dict):
                return
            values = features.get("values")
            if isinstance(values, dict):
                self._features.put(mint, {k: float(v) for k, v in values.items() if _ok(v)})
        except Exception as exc:
            _logger.warning("knowledge_features_failed", error=str(exc))

    async def _on_trade_approved(self, event: DomainEvent) -> None:
        """**Freeze** the evidence. This is the anti-leakage moment.

        The vector captured here is the one that will label this trade, whatever
        happens to the token afterwards. It is deliberately taken before the
        fill, not after the close.
        """
        try:
            envelope = event.to_envelope()
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                return
            mint = _mint_of(payload)
            if mint is None:
                return
            frozen = dict(self._features.get(mint) or {})
            if not frozen:
                _logger.info("approval_without_features", mint=mint)
            beliefs = dict(self._beliefs.get(mint) or {})
            # The approval's own tags win: they are what the Risk Manager
            # actually acted on. The committee's cohort keys fill the gaps.
            tags = {str(k): str(v) for k, v in (self._identities.get(mint) or {}).items()}
            tags.update(_tags_of(payload))
            self._approved.put(
                mint,
                {
                    "features": frozen,
                    "beliefs": beliefs,
                    "decided_at": event.occurred_at,
                    "tags": tags,
                    "correlation_id": str(envelope["correlation_id"] or "") or None,
                },
            )
        except Exception as exc:
            _logger.warning("knowledge_approval_failed", error=str(exc))

    async def _on_position_opened(self, event: DomainEvent) -> None:
        """Give the frozen evidence the reference its outcome will quote."""
        try:
            envelope = event.to_envelope()
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                return
            mint = _mint_of(payload)
            if mint is None:
                return
            ref = str(envelope["aggregate_id"])
            notional = _money(payload, "notional")
            # Remember the notional: PositionClosed reports realised PnL in
            # dollars, and a return needs the base it was earned on.
            self._positions.put(ref, {"mint": mint, "notional": notional or 0.0})

            pending = self._approved.take(mint) or {}
            decision = Decision(
                ref=ref,
                subject=mint,
                decided_at=_as_datetime(pending.get("decided_at")) or event.occurred_at,
                features=dict(pending.get("features") or {}),
                beliefs=dict(pending.get("beliefs") or {}),
                tags=dict(pending.get("tags") or _tags_of(payload)),
                correlation_id=pending.get("correlation_id"),
            )
            await self._journal.record_decision(decision)
        except Exception as exc:
            _logger.warning("knowledge_open_failed", error=str(exc))

    async def _on_position_closed(self, event: DomainEvent) -> None:
        """Settle the decision: this is where a Lesson is born."""
        try:
            envelope = event.to_envelope()
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                return
            ref = str(envelope["aggregate_id"])
            opened = self._positions.take(ref) or {}
            realized = _money(payload, "realized_pnl")
            if realized is None:
                _logger.warning("close_without_realized_pnl", ref=ref)
                return
            notional = float(opened.get("notional") or 0.0)
            if notional <= 0.0:
                # Without the base there is no return, and inventing one would
                # put a fabricated label into permanent memory — worse than
                # recording nothing. The decision stays open and is visible in
                # the open-decision gauge.
                _logger.warning("close_without_notional", ref=ref)
                return
            reason = str(payload.get("reason") or "")
            await self._journal.settle(
                Outcome(
                    ref=ref,
                    settled_at=event.occurred_at,
                    realized_roi=realized / notional,
                    hit_take_profit=reason == "take_profit",
                    hit_stop_loss=reason in ("stop_loss", "trailing"),
                    reason=reason,
                )
            )
        except Exception as exc:
            _logger.warning("knowledge_settle_failed", error=str(exc))

    # -- committee beliefs ----------------------------------------------------

    async def _on_prediction(self, event: DomainEvent) -> None:
        """Remember what the committee believed, so a lesson can be read as
        "what we thought" against "what happened".

        Without this, a lesson records the outcome but not the expectation, and
        the most valuable question the memory can answer — *where is the brain
        systematically wrong?* — becomes unanswerable.
        """
        try:
            payload = event.to_envelope()["payload"]
            if not isinstance(payload, dict):
                return
            mint = _mint_of(payload)
            if mint is None:
                return
            prediction = payload.get("prediction")
            beliefs: dict[str, float] = {}
            if isinstance(prediction, dict):
                meta = prediction.get("meta")
                if isinstance(meta, dict):
                    beliefs.update(
                        {
                            key: float(value)
                            for key, value in meta.items()
                            if key.startswith("prob_") and _ok(value)
                        }
                    )
                confidence = prediction.get("confidence")
                if isinstance(confidence, dict) and _ok(confidence.get("final")):
                    beliefs["confidence"] = float(confidence["final"])
            if beliefs:
                self._beliefs.put(mint, beliefs)
            self._remember_identity(mint, prediction)
        except Exception as exc:
            _logger.warning("knowledge_prediction_failed", error=str(exc))

    def _remember_identity(self, mint: str, prediction: object) -> None:
        """Keep the cohort keys the committee established for this candidate.

        This is the loop closing on itself. The Candidate Enricher answers
        "how have tokens by this developer / on this launchpad / telling this
        narrative worked out?" by matching **tags on settled lessons** — so a
        lesson recorded without those tags is a trade the platform can never
        learn a cohort from. The approval event does not carry them (the Risk
        Manager knows a candidate, not its provenance), but the committee does,
        and it publishes them on the prediction that immediately precedes the
        approval.

        Read defensively by key, like everything else here: Knowledge does not
        import the Learning context, and a payload-shape change must degrade to
        an untagged lesson rather than to a crash on the publisher's task.
        """
        if not isinstance(prediction, dict):
            return
        enrichment = prediction.get("enrichment")
        if not isinstance(enrichment, dict):
            return
        identity = enrichment.get("identity")
        if not isinstance(identity, dict):
            return
        tags = {
            key: str(value)
            for key in ("developer", "launchpad", "narrative", "cluster_id", "strategy")
            if isinstance(value := identity.get(key), str) and value
        }
        # ``cluster_id`` is the committee's name for it; the tag the enricher
        # and the Risk Manager's correlation engine both match on is ``cluster``.
        if "cluster_id" in tags:
            tags["cluster"] = tags.pop("cluster_id")
        if tags:
            self._identities.put(mint, tags)

    # -- status ---------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        stats = await self._store.stats()
        lessons = await self._lessons.count()
        recent = await self._lessons.load(limit=10_000)
        positives = sum(1 for lesson in recent if lesson.label_roi_positive)
        open_decisions = await self._journal.open_count()

        self._metrics.total.set(stats.total)
        self._metrics.lessons.set(lessons)
        self._metrics.open_decisions.set(open_decisions)
        self._metrics.positive_rate.set((positives / lessons) if lessons else 0.0)

        return {
            "enabled": True,
            "total": stats.total,
            "by_source": stats.by_source,
            "by_kind": stats.by_kind,
            "lessons": lessons,
            "positive_lessons": positives,
            "positive_rate": (positives / lessons) if lessons else 0.0,
            # The single most useful field on this snapshot: whether the memory
            # holds both classes. False means no model can be validated against
            # it, whatever the thresholds say.
            "is_trainable": lessons > 0 and 0 < positives < lessons,
            "open_decisions": open_decisions,
            "pending_approvals": len(self._approved),
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._cache.set(
                    STATUS_KEY, await self.snapshot(), ttl_seconds=_STATUS_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("knowledge_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STATUS_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        open_decisions = 0
        try:
            open_decisions = await self._journal.open_count()
        except Exception as exc:  # a cold store must not stop startup
            _logger.warning("knowledge_journal_load_failed", error=str(exc))
        _logger.info("knowledge_runtime_started", open_decisions=open_decisions)
        return [asyncio.create_task(self._publish_status_loop(), name="knowledge-status")]

    async def stop(self) -> None:
        self._stop.set()
        _logger.info("knowledge_runtime_stopped")

    # -- accessors for the API ------------------------------------------------

    @property
    def store(self) -> KnowledgeStore:
        return self._store

    @property
    def lessons(self) -> LessonStore:
        return self._lessons

    @property
    def journal(self) -> DecisionJournal:
        return self._journal


def _ok(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _tags_of(payload: dict[str, Any]) -> dict[str, str]:
    """Attribution travelling with an approval or an opened position."""
    tags = payload.get("tags")
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items() if v is not None}
    out: dict[str, str] = {}
    for key in ("strategy", "developer", "cluster", "narrative", "regime"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


__all__ = ["KNOWLEDGE_STATUS_NAMESPACE", "STATUS_KEY", "KnowledgeRuntime"]
