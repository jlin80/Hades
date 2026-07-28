"""The Committee's half of the learning loop.

Three behaviours that were missing and are now pinned:

* a ``LessonLearned`` reaches the outcome ledger, using the lesson's own
  features rather than a fresh feature-store read (the leakage guard again, this
  time on the consuming side);
* a promotion takes effect **without a restart** — ``set_active`` used to run
  only at startup, so the entire human-gated promotion machinery worked
  perfectly and changed nothing until the worker was bounced;
* the confidence engine's quality signals are derived from the dataset instead
  of being read once from configuration. They were presented as measurements and
  were in fact the constants 0.5 and 0.35, forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.knowledge.domain.events import LessonLearned
from hades.contexts.knowledge.domain.models import Decision, Lesson, Outcome
from hades.contexts.learning.domain.models import Dataset, TrainingSample
from hades.ops.committee_runtime import CommitteeRuntime
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry

_MINT = "So11111111111111111111111111111111111111112"
_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_TOKEN = TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


class _FakeRedis:
    async def get(self, key: str) -> Any:
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None


@dataclass
class _Container:
    settings: Any
    event_bus: InMemoryEventBus
    metrics: MetricsRegistry
    redis: Any
    database: None = None


@pytest.fixture
def committee() -> tuple[CommitteeRuntime, InMemoryEventBus]:
    from hades.shared_kernel.config import Settings

    bus = InMemoryEventBus()
    container = _Container(
        settings=Settings(),
        event_bus=bus,
        metrics=MetricsRegistry(),
        redis=_FakeRedis(),
    )
    return CommitteeRuntime(container), bus  # type: ignore[arg-type]


def _lesson(*, roi: float, features: dict[str, float] | None = None) -> Lesson:
    return Lesson.join(
        Decision(
            ref=str(new_id()),
            subject=_MINT,
            decided_at=_AT,
            features=features if features is not None else {"basic.liquidity": 0.8},
        ),
        Outcome(ref="ignored", settled_at=_AT, realized_roi=roi, hit_take_profit=roi > 0),
    )


@pytest.mark.asyncio
async def test_a_lesson_reaches_the_outcome_ledger(
    committee: tuple[CommitteeRuntime, InMemoryEventBus],
) -> None:
    """The write path that did not exist before Phase 1."""
    runtime, bus = committee
    store = runtime._outcome_store

    assert await store.count() == 0
    await bus.publish(LessonLearned(aggregate_id=new_id(), lesson=_lesson(roi=0.3)))

    assert await store.count() == 1
    samples = await store.load()
    assert samples[0].was_executed is True
    assert samples[0].label_roi_positive is True
    assert samples[0].weight == 1.0, "a settled trade is full-weight, unlike a rejection"


@pytest.mark.asyncio
async def test_the_ledger_sample_keeps_the_lessons_own_features(
    committee: tuple[CommitteeRuntime, InMemoryEventBus],
) -> None:
    """No feature-store lookup at settle time — that would read the present."""
    runtime, bus = committee
    entry = {"basic.liquidity": 0.81, "tech.rsi_14": 0.33}

    await bus.publish(LessonLearned(aggregate_id=new_id(), lesson=_lesson(roi=0.3, features=entry)))

    samples = await runtime._outcome_store.load()
    assert samples[0].features == entry


@pytest.mark.asyncio
async def test_both_classes_reach_the_ledger(
    committee: tuple[CommitteeRuntime, InMemoryEventBus],
) -> None:
    """The condition that makes validation possible at all."""
    runtime, bus = committee
    await bus.publish(LessonLearned(aggregate_id=new_id(), lesson=_lesson(roi=0.3)))
    await bus.publish(LessonLearned(aggregate_id=new_id(), lesson=_lesson(roi=-0.2)))

    samples = await runtime._outcome_store.load()
    labels = {sample.label_roi_positive for sample in samples}
    assert labels == {True, False}, "a single-class ledger can never train a model"


@pytest.mark.asyncio
async def test_a_promotion_swaps_the_active_committee_without_a_restart(
    committee: tuple[CommitteeRuntime, InMemoryEventBus],
) -> None:
    from hades.contexts.learning.domain.events import ModelPromoted

    runtime, bus = committee
    reloads: list[int] = []

    async def counting_load() -> None:
        reloads.append(1)

    runtime._load_active_committee = counting_load  # type: ignore[method-assign]

    await bus.publish(
        ModelPromoted(
            aggregate_id=new_id(),
            model_id=str(new_id()),
            name="meta_model",
            model_version="3",
        )
    )

    assert reloads, "a promoted model must take effect in the running process"


def test_quality_signals_are_derived_from_the_dataset_not_from_config(
    committee: tuple[CommitteeRuntime, InMemoryEventBus],
) -> None:
    runtime, _ = committee

    def dataset(positives: int, negatives: int) -> Dataset:
        samples = tuple(
            TrainingSample(
                token_mint=_MINT,
                at=_AT,
                features={"basic.liquidity": 0.5},
                label_roi_positive=index < positives,
                label_hit_tp=index < positives,
                label_hit_sl=index >= positives,
                realized_roi=0.1 if index < positives else -0.1,
                was_executed=True,
                was_rejected=False,
            )
            for index in range(positives + negatives)
        )
        return Dataset(dataset_id=str(new_id()), name="t", samples=samples)

    # A single-class dataset must score zero quality — that is the honest input
    # to a confidence calculation, and the state the platform actually sat in.
    runtime._refresh_quality(dataset(positives=0, negatives=230))
    assert runtime._manager._quality.dataset_quality == pytest.approx(0.0)

    # A balanced dataset scores full quality.
    runtime._refresh_quality(dataset(positives=50, negatives=50))
    assert runtime._manager._quality.dataset_quality == pytest.approx(1.0)

    # Support saturates at the configured training minimum, never above 1.
    small = dataset(positives=1, negatives=1)
    runtime._refresh_quality(small)
    assert 0.0 < runtime._manager._quality.sample_support <= 1.0
