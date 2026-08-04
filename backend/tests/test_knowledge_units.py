"""Unit tests for the Knowledge domain, recorder, journal and stores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hades.contexts.knowledge.application.journal import DecisionJournal
from hades.contexts.knowledge.application.recorder import KnowledgeRecorder
from hades.contexts.knowledge.domain.events import KnowledgeRejected, LessonLearned
from hades.contexts.knowledge.domain.models import (
    Decision,
    KnowledgeEnvelope,
    KnowledgeKind,
    KnowledgeQuery,
    KnowledgeSource,
    KnowledgeStats,
    Lesson,
    Observation,
    Outcome,
    Verification,
)
from hades.contexts.knowledge.infrastructure.stores import (
    InMemoryDecisionJournalStore,
    InMemoryKnowledgeStore,
    InMemoryLessonStore,
)
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import InMemoryEventBus

_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_MINT = "So11111111111111111111111111111111111111112"


def _envelope(**overrides: object) -> KnowledgeEnvelope:
    base: dict[str, object] = {
        "source": KnowledgeSource.SCANNER,
        "kind": KnowledgeKind.OBSERVATION,
        "verification": Verification.REPORTED,
        "subject": _MINT,
        "occurred_at": _AT,
    }
    base.update(overrides)
    return KnowledgeEnvelope(**base)  # type: ignore[arg-type]


def _decision(ref: str = "pos-1", **overrides: object) -> Decision:
    base: dict[str, object] = {
        "ref": ref,
        "subject": _MINT,
        "decided_at": _AT,
        "features": {"basic.liquidity": 0.7, "tech.rsi_14": 0.4},
        "beliefs": {"prob_roi_positive": 0.61},
    }
    base.update(overrides)
    return Decision(**base)  # type: ignore[arg-type]


# --- domain ------------------------------------------------------------------


def test_envelope_rejects_a_blank_subject() -> None:
    with pytest.raises(ValueError, match="subject"):
        _envelope(subject="   ")


def test_envelope_rejects_non_finite_features() -> None:
    """NaN in a feature would poison every model trained on it afterwards."""
    with pytest.raises(ValueError, match="finite"):
        _envelope(features={"tech.rsi_14": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        _envelope(features={"tech.rsi_14": float("inf")})


def test_outcome_rejects_non_finite_roi() -> None:
    with pytest.raises(ValueError, match="finite"):
        Outcome(ref="pos-1", settled_at=_AT, realized_roi=float("nan"))


def test_lesson_join_labels_from_the_outcome_and_features_from_the_decision() -> None:
    """The join is the anti-leakage guarantee, expressed as a type."""
    decision = _decision()
    outcome = Outcome(
        ref="pos-1",
        settled_at=_AT + timedelta(minutes=30),
        realized_roi=0.25,
        hit_take_profit=True,
        reason="take_profit",
    )
    lesson = Lesson.join(decision, outcome)

    assert lesson.features == decision.features
    assert lesson.beliefs == decision.beliefs
    assert lesson.label_roi_positive is True
    assert lesson.label_hit_tp is True
    assert lesson.label_hit_sl is False
    assert lesson.holding_seconds == pytest.approx(1800.0)


def test_lesson_join_marks_a_loss_negative() -> None:
    lesson = Lesson.join(
        _decision(),
        Outcome(ref="pos-1", settled_at=_AT, realized_roi=-0.2, hit_stop_loss=True),
    )
    assert lesson.label_roi_positive is False
    assert lesson.label_hit_sl is True


def test_a_zero_return_is_not_positive() -> None:
    """Exactly break-even must not be counted as a win: it would inflate the
    positive class with trades that taught nothing."""
    lesson = Lesson.join(_decision(), Outcome(ref="pos-1", settled_at=_AT, realized_roi=0.0))
    assert lesson.label_roi_positive is False


def test_verification_floor_orders_by_strength_not_alphabetically() -> None:
    """ "realised" sorts before "reported" alphabetically but outranks it."""
    spec = KnowledgeQuery(min_verification=Verification.SIMULATED)
    strong = Observation.from_envelope(_envelope(verification=Verification.REALISED))
    weak = Observation.from_envelope(_envelope(verification=Verification.REPORTED))
    assert spec.accepts(strong)
    assert not spec.accepts(weak)


def test_stats_reports_a_single_class_memory_as_untrainable() -> None:
    """The exact condition that kept the platform in cold start."""
    all_losses = KnowledgeStats(lessons=230, positive_lessons=0)
    assert all_losses.positive_rate == 0.0
    assert all_losses.is_trainable is False

    all_wins = KnowledgeStats(lessons=50, positive_lessons=50)
    assert all_wins.is_trainable is False

    mixed = KnowledgeStats(lessons=100, positive_lessons=37)
    assert mixed.is_trainable is True
    assert mixed.positive_rate == pytest.approx(0.37)


# --- recorder ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_stores_and_counts_accepted_records() -> None:
    store = InMemoryKnowledgeStore()
    recorder = KnowledgeRecorder(store)

    accepted = await recorder.record([_envelope(), _envelope(subject="wallet-abc")])

    assert len(accepted) == 2
    assert len(store.rows) == 2


@pytest.mark.asyncio
async def test_recorder_announces_a_rejection_instead_of_swallowing_it() -> None:
    """A silent ingestion boundary is indistinguishable from an idle platform."""
    bus = InMemoryEventBus()
    seen: list[DomainEvent] = []

    async def capture(event: DomainEvent) -> None:
        seen.append(event)

    bus.subscribe(KnowledgeRejected.__name__, capture)
    recorder = KnowledgeRecorder(InMemoryKnowledgeStore(), event_bus=bus)

    # Constructed through the store's own boundary: a blank subject is invalid,
    # so this exercises rejection rather than a type error at call time.
    bad = _envelope()
    object.__setattr__(bad, "subject", "")  # bypass frozen VO for the test
    await recorder.record([bad])

    assert len(seen) == 1
    assert isinstance(seen[0], KnowledgeRejected)


@pytest.mark.asyncio
async def test_recorder_reraises_a_store_failure() -> None:
    """A broken store is a platform fault; the caller must get to decide."""

    class Broken(InMemoryKnowledgeStore):
        async def append(self, observations: object) -> None:  # type: ignore[override]
            raise RuntimeError("database is down")

    recorder = KnowledgeRecorder(Broken())
    with pytest.raises(RuntimeError, match="database is down"):
        await recorder.record([_envelope()])


@pytest.mark.asyncio
async def test_query_filters_and_returns_newest_first() -> None:
    store = InMemoryKnowledgeStore()
    recorder = KnowledgeRecorder(store)
    await recorder.record(
        [
            _envelope(occurred_at=_AT, source=KnowledgeSource.SCANNER),
            _envelope(occurred_at=_AT + timedelta(hours=1), source=KnowledgeSource.SECURITY),
            _envelope(occurred_at=_AT + timedelta(hours=2), source=KnowledgeSource.SCANNER),
        ]
    )

    newest_first = await store.query(KnowledgeQuery())
    assert [o.occurred_at for o in newest_first] == [
        _AT + timedelta(hours=2),
        _AT + timedelta(hours=1),
        _AT,
    ]

    scanner_only = await store.query(KnowledgeQuery(source=KnowledgeSource.SCANNER))
    assert len(scanner_only) == 2


# --- journal -----------------------------------------------------------------


def _journal(bus: InMemoryEventBus | None = None) -> tuple[DecisionJournal, InMemoryLessonStore]:
    lessons = InMemoryLessonStore()
    return DecisionJournal(InMemoryDecisionJournalStore(), lessons, event_bus=bus), lessons


@pytest.mark.asyncio
async def test_settling_a_decision_produces_a_lesson_and_announces_it() -> None:
    bus = InMemoryEventBus()
    learned: list[LessonLearned] = []

    async def capture(event: DomainEvent) -> None:
        assert isinstance(event, LessonLearned)
        learned.append(event)

    bus.subscribe(LessonLearned.__name__, capture)
    journal, lessons = _journal(bus)

    await journal.record_decision(_decision())
    assert await journal.open_count() == 1

    lesson = await journal.settle(
        Outcome(ref="pos-1", settled_at=_AT + timedelta(minutes=5), realized_roi=0.4)
    )

    assert lesson is not None
    assert lesson.features == {"basic.liquidity": 0.7, "tech.rsi_14": 0.4}
    assert await lessons.count() == 1
    assert await journal.open_count() == 0
    assert len(learned) == 1


@pytest.mark.asyncio
async def test_a_redelivered_settlement_does_not_double_the_lesson() -> None:
    """At-least-once delivery must not silently double a trade's weight."""
    journal, lessons = _journal()
    await journal.record_decision(_decision())
    outcome = Outcome(ref="pos-1", settled_at=_AT, realized_roi=0.4)

    first = await journal.settle(outcome)
    second = await journal.settle(outcome)

    assert first is not None
    assert second is None
    assert await lessons.count() == 1


@pytest.mark.asyncio
async def test_an_outcome_with_no_decision_is_ignored_not_fabricated() -> None:
    journal, lessons = _journal()
    result = await journal.settle(Outcome(ref="unknown", settled_at=_AT, realized_roi=1.0))
    assert result is None
    assert await lessons.count() == 0


@pytest.mark.asyncio
async def test_the_lesson_store_is_idempotent_on_ref() -> None:
    lessons = InMemoryLessonStore()
    lesson = Lesson.join(_decision(), Outcome(ref="pos-1", settled_at=_AT, realized_roi=0.1))
    await lessons.append([lesson])
    await lessons.append([lesson])
    assert await lessons.count() == 1


@pytest.mark.asyncio
async def test_lessons_load_oldest_first_so_chronology_survives() -> None:
    """A walk-forward split over a reversed set would train on the future."""
    lessons = InMemoryLessonStore()
    for index, offset in enumerate((2, 0, 1)):
        await lessons.append(
            [
                Lesson.join(
                    _decision(ref=f"pos-{index}", decided_at=_AT + timedelta(hours=offset)),
                    Outcome(ref=f"pos-{index}", settled_at=_AT, realized_roi=0.1),
                )
            ]
        )
    loaded = await lessons.load()
    assert [row.decided_at for row in loaded] == sorted(row.decided_at for row in loaded)


@pytest.mark.asyncio
async def test_the_journal_store_takes_a_decision_only_once() -> None:
    store = InMemoryDecisionJournalStore()
    await store.save(_decision())
    assert await store.take("pos-1") is not None
    assert await store.take("pos-1") is None
    assert await store.count_open() == 0
