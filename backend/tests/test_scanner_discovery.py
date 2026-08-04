"""Discovery Engine: dedup, coarse gating, source-health events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.scanner.application.discovery_engine import DiscoveryEngine
from hades.contexts.scanner.application.metrics import ScannerMetrics
from hades.contexts.scanner.domain.events import SourceHealthChanged
from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.contexts.scanner.infrastructure.seen_registry import InMemorySeenTokenRegistry
from hades.shared_kernel.config.settings import ScannerSettings
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.events.bus import EventHandler
from hades.shared_kernel.observability import MetricsRegistry

_MINT_A = "A" * 44
_MINT_B = "B" * 44
_MINT_C = "C" * 44


def _candidate(mint: str, liquidity: float, source: str = "test") -> RawTokenCandidate:
    return RawTokenCandidate(
        token=TokenRef(mint=TokenMint(address=mint)),
        source=source,
        liquidity=Money(amount=liquidity),
    )


class FakeSource:
    def __init__(self, name: str, candidates: list[RawTokenCandidate]) -> None:
        self._name = name
        self._candidates = candidates

    @property
    def name(self) -> str:
        return self._name

    async def stream(self) -> AsyncIterator[RawTokenCandidate]:
        for candidate in self._candidates:
            yield candidate
        await asyncio.Event().wait()  # keep the stream open until cancelled


class FailingSource:
    """A source that fails every poll, counting how many times it was retried."""

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def name(self) -> str:
        return "boom"

    async def stream(self) -> AsyncIterator[RawTokenCandidate]:
        self.attempts += 1
        raise RuntimeError("down")
        yield  # pragma: no cover — makes this an async generator


class ExplodingBus:
    """A bus whose publish always fails — the broker itself being the sick one.

    This is not a contrived fault. Health is announced over the same Redis the
    sources depend on, so the realistic failure is precisely the one where the
    thing you would use to report the problem is the problem.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """No subscribers — nothing here ever gets delivered."""

    async def publish(self, event: DomainEvent) -> None:
        self.attempts += 1
        raise RuntimeError("broker down")

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)


def _settings(**overrides: object) -> ScannerSettings:
    return ScannerSettings().model_copy(update=overrides)


async def _drive(engine: DiscoveryEngine, seconds: float = 0.05) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await task


async def _collect(
    candidates: list[RawTokenCandidate], settings: ScannerSettings
) -> tuple[list[RawTokenCandidate], InMemoryEventBus]:
    sink_items: list[RawTokenCandidate] = []

    async def sink(c: RawTokenCandidate) -> None:
        sink_items.append(c)

    bus = InMemoryEventBus()
    engine = DiscoveryEngine(
        [FakeSource("test", candidates)],
        InMemorySeenTokenRegistry(),
        sink,
        settings=settings,
        event_bus=bus,
        metrics=ScannerMetrics(MetricsRegistry()),
    )
    await _drive(engine)
    return sink_items, bus


async def test_passes_new_candidate_over_min_liquidity() -> None:
    items, _ = await _collect([_candidate(_MINT_A, 9000)], _settings(min_liquidity_usd=5000))
    assert [str(c.token.mint) for c in items] == [_MINT_A]


async def test_rejects_low_liquidity() -> None:
    items, _ = await _collect([_candidate(_MINT_A, 100)], _settings(min_liquidity_usd=5000))
    assert items == []


async def test_dedups_repeated_mint() -> None:
    items, _ = await _collect(
        [_candidate(_MINT_A, 9000), _candidate(_MINT_A, 9000)],
        _settings(min_liquidity_usd=5000),
    )
    assert len(items) == 1


async def test_blacklist_blocks_token() -> None:
    items, _ = await _collect(
        [_candidate(_MINT_A, 9000)],
        _settings(min_liquidity_usd=5000, blacklist_tokens=_MINT_A),
    )
    assert items == []


async def test_whitelist_bypasses_liquidity_gate() -> None:
    items, _ = await _collect(
        [_candidate(_MINT_A, 1)],
        _settings(min_liquidity_usd=5000, whitelist_tokens=_MINT_A),
    )
    assert [str(c.token.mint) for c in items] == [_MINT_A]


async def test_failing_source_emits_source_down_event() -> None:
    events: list[SourceHealthChanged] = []
    bus = InMemoryEventBus()

    async def on_event(event: object) -> None:
        assert isinstance(event, SourceHealthChanged)
        events.append(event)

    bus.subscribe(SourceHealthChanged.__name__, on_event)

    async def sink(c: RawTokenCandidate) -> None:  # pragma: no cover — never called
        pass

    engine = DiscoveryEngine(
        [FailingSource()],
        InMemorySeenTokenRegistry(),
        sink,
        settings=_settings(),
        event_bus=bus,
        metrics=ScannerMetrics(MetricsRegistry()),
    )
    await _drive(engine)
    assert any(not e.available for e in events)


async def test_publish_failure_does_not_kill_the_source_loop() -> None:
    """A broker that cannot take the news must not silence the reporter.

    Live defect: ``_mark_source`` published *before* the error was logged, and the
    publish raised from inside the ``except`` block that keeps the source alive.
    The exception escaped the loop, the source stopped polling, and — because the
    log line came after the publish — it left no trace at all. Three of four
    discovery sources went dark this way; only one of them logged anything.
    """
    source = FailingSource()
    bus = ExplodingBus()

    async def sink(c: RawTokenCandidate) -> None:  # pragma: no cover — never called
        pass

    engine = DiscoveryEngine(
        [source],
        InMemorySeenTokenRegistry(),
        sink,
        settings=_settings(),
        event_bus=bus,
        metrics=ScannerMetrics(MetricsRegistry()),
    )
    await _drive(engine, seconds=1.3)  # first poll fails, then one _BACKOFF_MIN retry

    # The publish was attempted and failed...
    assert bus.attempts >= 1
    # ...and the loop survived it: the source was retried after the failed publish.
    assert source.attempts >= 2, "the source loop died when announcing its own failure"
    # Health is still recorded locally even though nobody could be told.
    assert engine.source_health()["boom"] is False


async def test_a_dead_source_task_is_not_reported_as_up() -> None:
    """A source whose loop ends must stop being advertised as available.

    The health map is what ``/api/v1/scanner/status`` serves. A source that is no
    longer being polled but still reads ``true`` there is the worst of both: the
    funnel starves and the dashboard says the mouth of it is fine.
    """
    bus = InMemoryEventBus()

    async def sink(c: RawTokenCandidate) -> None:  # pragma: no cover — never called
        pass

    engine = DiscoveryEngine(
        [FailingSource()],
        InMemorySeenTokenRegistry(),
        sink,
        settings=_settings(),
        event_bus=bus,
        metrics=ScannerMetrics(MetricsRegistry()),
    )

    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    await asyncio.sleep(0.05)
    # Kill the source's loop the way a stray exception would, then let the
    # done-callback run.
    for pending in asyncio.all_tasks():
        if pending.get_name() == "discovery:boom":
            pending.cancel()
    await asyncio.sleep(0.05)

    assert engine.source_health()["boom"] is False
    stop.set()
    await task


async def test_source_recovers_to_up_after_a_transient_failure() -> None:
    """Down is a state, not a latch: one good poll puts a source back up."""
    bus = InMemoryEventBus()
    sink_items: list[RawTokenCandidate] = []

    async def sink(c: RawTokenCandidate) -> None:
        sink_items.append(c)

    class FlakySource:
        def __init__(self) -> None:
            self.attempts = 0

        @property
        def name(self) -> str:
            return "flaky"

        async def stream(self) -> AsyncIterator[RawTokenCandidate]:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("first poll fails")
            yield _candidate(_MINT_A, 9000, source="flaky")
            await asyncio.Event().wait()

    engine = DiscoveryEngine(
        [FlakySource()],
        InMemorySeenTokenRegistry(),
        sink,
        settings=_settings(min_liquidity_usd=5000),
        event_bus=bus,
        metrics=ScannerMetrics(MetricsRegistry()),
    )
    await _drive(engine, seconds=1.5)  # first poll fails, backoff is 1.0s, second wins

    assert engine.source_health()["flaky"] is True
    assert [str(c.token.mint) for c in sink_items] == [_MINT_A]
