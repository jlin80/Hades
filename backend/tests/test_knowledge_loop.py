"""The learning loop, end to end — the test that says Phase 1 actually landed.

These drive real domain events through a real bus into the real
:class:`KnowledgeRuntime` and assert that a paper trade's realised result comes
out the other side as a ground-truth training sample. That path did not exist
before: ``KnowledgeFeedback.record_outcome`` had no callers, so the outcome
ledger only ever held weak negatives from security rejections, the dataset was
single-class, its AUC was undefined, and no candidate model could pass
validation however long the platform ran.

The most important assertion in this file is
:func:`test_the_lesson_uses_features_from_entry_not_from_exit`. Closing the loop
the obvious way — read the feature store when the position closes — would have
produced a dataset that trains beautifully and predicts nothing, because it
labels the state of the world at the moment of *sale* with the result of the
trade. Everything else here can regress and be caught in review; that one would
regress silently and look like success.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hades.contexts.common.domain.value_objects import Money, Percentage, TokenMint, TokenRef
from hades.contexts.features.domain.events import FeaturesComputed
from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.knowledge.domain.events import LessonLearned
from hades.contexts.knowledge.domain.models import KnowledgeSource, Verification
from hades.contexts.portfolio.domain.events import PositionClosed, PositionOpened
from hades.contexts.risk.domain.events import TradeApproved
from hades.contexts.risk.domain.models import PositionSizing
from hades.contexts.security.domain.events import TokenRejected
from hades.ops.knowledge_runtime import KnowledgeRuntime
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry

_MINT = "So11111111111111111111111111111111111111112"
_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_TOKEN = TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")

#: Position aggregate ids are real UUIDs (``EntityId``), so the tests mint one
#: per logical position and refer to it by name.
_REFS: dict[str, str] = {}


def _ref(name: str) -> str:
    return _REFS.setdefault(name, str(new_id()))


_ENTRY_FEATURES = {"basic.liquidity": 0.81, "tech.rsi_14": 0.33, "holders.count": 0.5}
_EXIT_FEATURES = {"basic.liquidity": 0.02, "tech.rsi_14": 0.99, "holders.count": 0.01}


class _FakeRedis:
    """The runtime publishes a status snapshot; nothing here reads it back."""

    async def get(self, key: str) -> Any:
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None


@dataclass
class _Container:
    """Just enough container for the runtime: no database → in-memory stores."""

    settings: Any
    event_bus: InMemoryEventBus
    redis: Any
    metrics: MetricsRegistry
    database: None = None


@pytest.fixture
def runtime() -> tuple[KnowledgeRuntime, InMemoryEventBus]:
    from hades.shared_kernel.config import Settings

    bus = InMemoryEventBus()
    container = _Container(
        settings=Settings(),
        event_bus=bus,
        redis=_FakeRedis(),
        metrics=MetricsRegistry(),
    )
    return KnowledgeRuntime(container), bus  # type: ignore[arg-type]


def _features_event(values: dict[str, float], at: datetime = _AT) -> FeaturesComputed:
    return FeaturesComputed(
        aggregate_id=new_id(),
        occurred_at=at,
        token=_TOKEN,
        features=FeatureSet(token=_TOKEN, computed_at=at, values=values),
        feature_count=len(values),
    )


def _approval(at: datetime = _AT) -> TradeApproved:
    return TradeApproved(
        aggregate_id=new_id(),
        occurred_at=at,
        token=_TOKEN,
        sizing=PositionSizing(
            notional=Money(amount=Decimal("61.53")),
            take_profit=Percentage(value=40.0),
            stop_loss=Percentage(value=20.0),
            trailing_enabled=True,
            trailing_activation=Percentage(value=15.0),
            trailing_distance=Percentage(value=10.0),
        ),
        conviction=0.62,
        risk_amount_usd=12.31,
        rationale="test",
        strategy="momentum_breakout",
        regime="expansion",
    )


def _opened(ref: str, notional: float = 100.0, at: datetime = _AT) -> PositionOpened:
    return PositionOpened(
        aggregate_id=_ref(ref),
        occurred_at=at,
        token=_TOKEN,
        entry_price=Money(amount=Decimal("0.5")),
        quantity=Decimal("200"),
        notional=Money(amount=Decimal(str(notional))),
    )


def _closed(
    ref: str, pnl: float, reason: str = "take_profit", at: datetime | None = None
) -> PositionClosed:
    return PositionClosed(
        aggregate_id=_ref(ref),
        occurred_at=at or (_AT + timedelta(minutes=20)),
        exit_price=Money(amount=Decimal("0.6")),
        realized_pnl=Money(amount=Decimal(str(pnl))),
        reason=reason,
    )


async def _run_a_winning_trade(
    bus: InMemoryEventBus, ref: str = "pos-1", pnl: float = 25.0
) -> None:
    await bus.publish(_features_event(_ENTRY_FEATURES))
    await bus.publish(_approval())
    await bus.publish(_opened(ref))
    await bus.publish(_closed(ref, pnl))


# --- the loop ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_closed_paper_trade_becomes_a_ground_truth_lesson(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    _knowledge, bus = runtime
    learned: list[LessonLearned] = []

    async def capture(event: DomainEvent) -> None:
        assert isinstance(event, LessonLearned)
        learned.append(event)

    bus.subscribe(LessonLearned.__name__, capture)

    await _run_a_winning_trade(bus)

    assert len(learned) == 1, "a closed trade must produce exactly one lesson"
    lesson = learned[0].lesson
    assert lesson.subject == _MINT
    # 25 realised on a 100 notional.
    assert lesson.realized_roi == pytest.approx(0.25)
    assert lesson.label_roi_positive is True
    assert lesson.label_hit_tp is True
    assert lesson.verification is Verification.REALISED


@pytest.mark.asyncio
async def test_the_lesson_uses_features_from_entry_not_from_exit(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """The anti-leakage guarantee, pinned.

    Between the approval and the close, the token's features change completely.
    The lesson must carry the vector as it stood when the decision was taken. If
    this ever fails, the platform is training on the future: offline metrics will
    look excellent and the model will be worthless in production.
    """
    _knowledge, bus = runtime
    learned: list[LessonLearned] = []

    async def capture(event: DomainEvent) -> None:
        assert isinstance(event, LessonLearned)
        learned.append(event)

    bus.subscribe(LessonLearned.__name__, capture)

    await bus.publish(_features_event(_ENTRY_FEATURES))
    await bus.publish(_approval())
    await bus.publish(_opened("pos-1"))
    # The world moves on while the position is held.
    await bus.publish(_features_event(_EXIT_FEATURES, at=_AT + timedelta(minutes=10)))
    await bus.publish(_closed("pos-1", 25.0))

    assert len(learned) == 1
    assert learned[0].lesson.features == _ENTRY_FEATURES
    assert learned[0].lesson.features != _EXIT_FEATURES


@pytest.mark.asyncio
async def test_a_losing_trade_is_recorded_as_a_negative(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """Both classes must reach memory, or the dataset stays untrainable."""
    knowledge, bus = runtime
    await bus.publish(_features_event(_ENTRY_FEATURES))
    await bus.publish(_approval())
    await bus.publish(_opened("pos-loss"))
    await bus.publish(_closed("pos-loss", -18.0, reason="stop_loss"))

    lessons = await knowledge.lessons.load()
    assert len(lessons) == 1
    assert lessons[0].label_roi_positive is False
    assert lessons[0].label_hit_sl is True
    assert lessons[0].realized_roi == pytest.approx(-0.18)


@pytest.mark.asyncio
async def test_the_memory_reports_itself_trainable_only_with_both_classes(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """The single most useful field on the status snapshot."""
    knowledge, bus = runtime

    await _run_a_winning_trade(bus, ref="win-1", pnl=25.0)
    only_wins = await knowledge.snapshot()
    assert only_wins["lessons"] == 1
    assert only_wins["is_trainable"] is False, "one class is not a trainable dataset"

    await bus.publish(_features_event(_ENTRY_FEATURES))
    await bus.publish(_approval())
    await bus.publish(_opened("loss-1"))
    await bus.publish(_closed("loss-1", -30.0, reason="stop_loss"))

    both = await knowledge.snapshot()
    assert both["lessons"] == 2
    assert both["is_trainable"] is True
    assert both["positive_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_a_redelivered_close_does_not_produce_a_second_lesson(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    knowledge, bus = runtime
    await _run_a_winning_trade(bus)
    await bus.publish(_closed("pos-1", 25.0))

    assert await knowledge.lessons.count() == 1


@pytest.mark.asyncio
async def test_a_close_without_a_known_notional_records_nothing_rather_than_guessing(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """A fabricated return in permanent memory is worse than a missing one."""
    knowledge, bus = runtime
    # No PositionOpened, so the runtime never learned the base of the return.
    await bus.publish(_closed("orphan", 25.0))
    assert await knowledge.lessons.count() == 0


@pytest.mark.asyncio
async def test_the_committee_beliefs_travel_with_the_lesson(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """So the memory can answer where the brain is systematically wrong."""
    from hades.contexts.learning.domain.events import CommitteePredictionGenerated

    knowledge, bus = runtime
    prediction = _committee_prediction()
    await bus.publish(_features_event(_ENTRY_FEATURES))
    await bus.publish(prediction)
    await bus.publish(_approval())
    await bus.publish(_opened("pos-1"))
    await bus.publish(_closed("pos-1", 25.0))

    lessons = await knowledge.lessons.load()
    assert lessons[0].beliefs.get("prob_roi_positive") == pytest.approx(0.61)
    assert isinstance(prediction, CommitteePredictionGenerated)


def _committee_prediction() -> DomainEvent:
    from hades.contexts.learning.domain.events import CommitteePredictionGenerated
    from hades.contexts.learning.domain.models import (
        CommitteePrediction,
        ConfidenceFactors,
        MetaPrediction,
        RegimeAssessment,
    )

    return CommitteePredictionGenerated(
        aggregate_id=new_id(),
        occurred_at=_AT,
        token=_TOKEN,
        prediction=CommitteePrediction(
            token=_TOKEN,
            at=_AT,
            meta=MetaPrediction(
                token=_TOKEN,
                prob_roi_positive=0.61,
                prob_hit_tp=0.55,
                prob_hit_sl=0.30,
                confidence=0.48,
            ),
            confidence=ConfidenceFactors(final=0.48),
            regime=RegimeAssessment(),
        ),
    )


# --- provenance --------------------------------------------------------------


@pytest.mark.asyncio
async def test_observations_are_recorded_with_their_provenance(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """Every listed producer must actually reach memory, tagged correctly."""
    from hades.contexts.knowledge.domain.models import KnowledgeQuery

    knowledge, bus = runtime
    await bus.publish(
        TokenRejected(
            aggregate_id=new_id(),
            occurred_at=_AT,
            token=_TOKEN,
            score=12.0,
            reasons=["CANNOT_SELL"],
        )
    )
    rows = await knowledge.store.query(KnowledgeQuery(source=KnowledgeSource.SECURITY))
    assert len(rows) == 1
    assert rows[0].subject == _MINT
    # A security verdict is a model's judgement, not settled reality.
    assert rows[0].verification is Verification.SIMULATED


@pytest.mark.asyncio
async def test_a_closed_position_is_both_an_observation_and_a_lesson(
    runtime: tuple[KnowledgeRuntime, InMemoryEventBus],
) -> None:
    """Two concerns, deliberately not collapsed: what happened, and what it teaches."""
    from hades.contexts.knowledge.domain.models import KnowledgeQuery

    knowledge, bus = runtime
    await _run_a_winning_trade(bus)

    observations = await knowledge.store.query(KnowledgeQuery(source=KnowledgeSource.PAPER_TRADING))
    assert len(observations) == 1
    assert observations[0].verification is Verification.REALISED
    assert await knowledge.lessons.count() == 1
