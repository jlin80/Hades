"""Event bus + event store behave to contract."""

from __future__ import annotations

import pytest

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.notification.domain.events import NotificationRequested
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.scanner.domain.events import TokenDiscovered
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.errors import ConcurrencyError
from hades.shared_kernel.events import EventRegistry, InMemoryEventBus, InMemoryEventStore

_MINT = "So11111111111111111111111111111111111111112"


def _event() -> TokenDiscovered:
    token = TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")
    return TokenDiscovered(
        aggregate_id=new_id(),
        token=token,
        source="pumpfun",
        initial_liquidity=Money(amount="10000"),
    )


async def test_bus_delivers_to_subscriber() -> None:
    bus = InMemoryEventBus()
    received: list[str] = []

    async def handler(event: object) -> None:
        received.append(type(event).__name__)

    bus.subscribe("TokenDiscovered", handler)
    await bus.publish(_event())
    assert received == ["TokenDiscovered"]


async def test_store_appends_and_versions() -> None:
    store = InMemoryEventStore()
    aggregate_id = new_id()
    event = _event().model_copy(update={"aggregate_id": aggregate_id})

    await store.append(aggregate_id, "token", [event], expected_version=0)
    stream = await store.load_stream(aggregate_id)
    assert len(stream) == 1
    assert stream[0].version == 1


async def test_store_rejects_stale_version() -> None:
    store = InMemoryEventStore()
    aggregate_id = new_id()
    event = _event().model_copy(update={"aggregate_id": aggregate_id})
    await store.append(aggregate_id, "token", [event], expected_version=0)

    with pytest.raises(ConcurrencyError):
        await store.append(aggregate_id, "token", [event], expected_version=0)


# --- the Redis bus: bounded stream and a consumer that watches itself ---------
#
# Both added after a live deployment ran 59 hours behind on its consumer group.
# The decision path was judging tokens from two days earlier, the portfolio's
# mark-to-market events had not been reached so its unrealised PnL sat at exactly
# zero, and every container reported healthy throughout. The stream had grown to
# 172k entries and 155 MB of Redis with no cap and no measurement.


class _FakeRedisClient:
    """Records xadd kwargs and serves a canned xinfo_groups."""

    def __init__(self, lag: int = 0, pending: int = 0, group: str = "worker") -> None:
        self.adds: list[dict[str, object]] = []
        self._lag = lag
        self._pending = pending
        self._group = group

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs: object) -> str:
        self.adds.append({"stream": stream, **kwargs})
        return "1-0"

    async def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
        return [{"name": self._group, "lag": self._lag, "pending": self._pending}]


class _FakeProvider:
    def __init__(self, client: _FakeRedisClient) -> None:
        self._client = client

    def client(self) -> _FakeRedisClient:
        return self._client


def _redis_bus(client: _FakeRedisClient, **kwargs: object):
    from hades.shared_kernel.events.redis_bus import RedisEventBus

    registry = EventRegistry()
    registry.register(NotificationRequested)
    return RedisEventBus(_FakeProvider(client), registry, group="worker", **kwargs)  # type: ignore[arg-type]


async def test_publishing_trims_the_stream() -> None:
    """Unbounded growth ends in an eviction or an OOM, not in a warning."""
    client = _FakeRedisClient()
    bus = _redis_bus(client, max_len=1_000)

    await bus.publish(
        NotificationRequested(aggregate_id=new_id(), title="t", body="b", severity=Severity.INFO)
    )

    assert client.adds, "nothing was published"
    assert client.adds[0]["maxlen"] == 1_000
    assert client.adds[0]["approximate"] is True, (
        "exact trimming would make every publish pay for a full-stream scan"
    )


def _spy_errors(monkeypatch) -> list[str]:  # type: ignore[no-untyped-def]
    """Capture the bus's error events. structlog does not go through caplog."""
    from hades.shared_kernel.events import redis_bus as module

    seen: list[str] = []

    def record(event: str, **_: object) -> None:
        seen.append(event)

    monkeypatch.setattr(module._logger, "error", record)
    return seen


async def test_a_backlogged_consumer_logs_an_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The 59-hour silence is what this exists to break."""
    errors = _spy_errors(monkeypatch)
    bus = _redis_bus(
        _FakeRedisClient(lag=50_000, pending=765),
        lag_warn_threshold=5_000,
        lag_check_interval_seconds=0.0,
    )

    await bus._check_lag()

    assert "redis_bus_consumer_behind" in errors


async def test_a_current_consumer_does_not_cry_wolf(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    errors = _spy_errors(monkeypatch)
    bus = _redis_bus(
        _FakeRedisClient(lag=3, pending=1),
        lag_warn_threshold=5_000,
        lag_check_interval_seconds=0.0,
    )

    await bus._check_lag()

    assert errors == []


async def test_the_lag_check_is_rate_limited() -> None:
    """One XINFO per interval, not one per pass of the read loop."""

    class _Counting(_FakeRedisClient):
        def __init__(self) -> None:
            super().__init__(lag=0)
            self.info_calls = 0

        async def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
            self.info_calls += 1
            return await super().xinfo_groups(stream)

    client = _Counting()
    bus = _redis_bus(client, lag_check_interval_seconds=3600.0)

    await bus._check_lag()
    await bus._check_lag()
    await bus._check_lag()

    assert client.info_calls == 1


async def test_a_failing_lag_check_never_breaks_the_bus() -> None:
    """Observability must not be able to stall the thing it observes."""

    class _Broken(_FakeRedisClient):
        async def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
            raise RuntimeError("redis is down")

    bus = _redis_bus(_Broken(), lag_check_interval_seconds=0.0)
    await bus._check_lag()  # must not raise


# --- reclaiming what a dead consumer never acked ------------------------------
#
# A group's pending list is per-consumer, and the consumer name is the process's
# stable instance id. A container killed mid-dispatch — a deploy, an OOM, a
# recreate — leaves its in-flight messages assigned to a name that will never ack
# them, and nothing redelivers those: XREADGROUP ">" returns only new entries.
# The live deployment had accumulated 765 such orphans across restarts, and they
# were invisible: published, still in the stream, and never handled.


class _ClaimingClient(_FakeRedisClient):
    """Serves one batch of stale pending messages, then nothing."""

    def __init__(self, claimed: list[tuple[str, dict[str, str]]]) -> None:
        super().__init__()
        self._claimed = claimed
        self.autoclaims: list[dict[str, object]] = []
        self.acked: list[str] = []

    async def xautoclaim(self, stream: str, group: str, consumer: str, **kwargs: object):  # type: ignore[no-untyped-def]
        self.autoclaims.append(kwargs)
        batch, self._claimed = self._claimed, []
        return ["0-0", batch, []]

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append(message_id)
        return 1


def _envelope_fields(title: str) -> dict[str, str]:
    import orjson

    event = NotificationRequested(
        aggregate_id=new_id(), title=title, body="b", severity=Severity.INFO
    )
    return {"type": "NotificationRequested", "data": orjson.dumps(event.to_envelope()).decode()}


async def test_orphaned_messages_are_reclaimed_and_handled() -> None:
    """The regression: without this they are simply never processed."""
    client = _ClaimingClient([("1-1", _envelope_fields("orphan"))])
    bus = _redis_bus(client, reclaim_after_seconds=60.0)

    seen: list[str] = []

    async def handler(event) -> None:  # type: ignore[no-untyped-def]
        seen.append(event.title)

    bus.subscribe("NotificationRequested", handler)
    await bus._reclaim_stale()

    assert seen == ["orphan"], "a reclaimed message must go through the normal handlers"
    assert client.acked == ["1-1"], "and must be acked, or it is reclaimed forever"


async def test_reclaiming_respects_the_idle_threshold() -> None:
    """It must not steal work from a consumer that is merely mid-dispatch."""
    client = _ClaimingClient([])
    bus = _redis_bus(client, reclaim_after_seconds=300.0)

    await bus._reclaim_stale()

    assert client.autoclaims[0]["min_idle_time"] == 300_000


async def test_nothing_pending_is_a_silent_no_op() -> None:
    client = _ClaimingClient([])
    bus = _redis_bus(client, reclaim_after_seconds=60.0)

    await bus._reclaim_stale()

    assert client.acked == []


async def test_a_failing_reclaim_never_breaks_the_bus() -> None:
    class _Broken(_FakeRedisClient):
        async def xautoclaim(self, *a: object, **k: object):  # type: ignore[no-untyped-def]
            raise RuntimeError("redis is down")

    bus = _redis_bus(_Broken())
    await bus._reclaim_stale()  # must not raise
