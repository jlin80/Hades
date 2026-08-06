"""Lanes: independent consumer groups inside one process.

Why these exist. On 2026-08-06 the live Worker sat exactly one stream-window
behind — 50,004 events, about eight hours — and stayed there. Across 21 minutes
of sampling it read 2.51 events/s against 2.81 produced and its lag did not move,
while every other service sat at lag 0. The cause was structural: one consumer
group per process, one read loop, and twelve runtimes dispatched through it in
series, so the process could only go as fast as its slowest storage-bound
handler. Events were being trimmed away unread while every container reported
healthy.

So the property under test is not "lanes exist". It is **a slow handler in one
lane cannot stall another lane**, plus the two things that make lanes safe to
deploy onto a running system: the default lane keeps the old group name, and a
lane that has never turned must never read as healthy.
"""

from __future__ import annotations

import asyncio

import orjson

from hades.contexts.notification.domain.events import NotificationRequested
from hades.contexts.notification.domain.ports import Severity
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventRegistry
from hades.shared_kernel.events.bus import LaneBus
from hades.shared_kernel.events.redis_bus import DEFAULT_LANE, RedisEventBus


class _FakeRedisClient:
    """Serves each group its own canned batch, then nothing."""

    def __init__(self, batches: dict[str, list[tuple[str, dict[str, str]]]] | None = None) -> None:
        self.groups_created: list[tuple[str, str]] = []
        self.acked: list[tuple[str, str]] = []
        self._batches = batches or {}

    async def xgroup_create(self, stream: str, group: str, id: str = "$", **_: object) -> None:
        self.groups_created.append((group, id))

    async def xreadgroup(
        self, group: str, consumer: str, streams: dict[str, str], **_: object
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        pending = self._batches.pop(group, None)
        if not pending:
            # Nothing left for this group: yield so a starved lane cannot spin.
            await asyncio.sleep(0)
            return []
        return [("hades.events:stream", pending)]

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append((group, message_id))
        return 1

    async def xautoclaim(self, *a: object, **k: object) -> tuple[str, list[object]]:
        return ("0-0", [])

    async def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
        return []

    async def xadd(self, stream: str, fields: dict[str, str], **_: object) -> str:
        return "1-0"


class _FakeProvider:
    def __init__(self, client: _FakeRedisClient) -> None:
        self._client = client

    def client(self) -> _FakeRedisClient:
        return self._client


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(NotificationRequested)
    return registry


def _bus(client: _FakeRedisClient, **kwargs: object) -> RedisEventBus:
    return RedisEventBus(  # type: ignore[arg-type]
        _FakeProvider(client),
        _registry(),
        group="worker",
        block_ms=0,
        **kwargs,
    )


async def _noop(_event: object) -> None:
    return None


def _fields() -> dict[str, str]:
    """A real envelope: dispatch only reaches handlers if the registry rebuilds it."""
    event = NotificationRequested(
        aggregate_id=new_id(), title="t", body="b", severity=Severity.INFO
    )
    return {
        "type": NotificationRequested.__name__,
        "data": orjson.dumps(event.to_envelope()).decode(),
    }


# -- group naming: safe to deploy onto a running system ------------------------


def test_the_default_lane_keeps_the_bare_role_group() -> None:
    """An existing deployment's group must not be renamed out from under it.

    Renaming ``worker`` to ``worker.main`` would create a fresh group at ``$``
    and silently abandon whatever the old one had not yet acked. The default
    lane picks up exactly where the single group left off.
    """
    bus = _bus(_FakeRedisClient())
    assert bus.group_for(DEFAULT_LANE) == "worker"


def test_a_named_lane_gets_its_own_group() -> None:
    bus = _bus(_FakeRedisClient())
    assert bus.group_for("scanner") == "worker.scanner"
    assert bus.group_for("committee") == "worker.committee"


def test_subscribing_on_a_lane_creates_only_that_lane() -> None:
    bus = _bus(_FakeRedisClient())
    bus.subscribe(NotificationRequested.__name__, _noop, lane="scanner")
    bus.subscribe(NotificationRequested.__name__, _noop, lane="committee")
    assert bus.lanes == ("committee", "scanner")


async def test_a_new_lane_starts_at_the_present_not_at_the_backlog() -> None:
    """``id="$"``: a lane added today must not replay history it never saw.

    The Worker's backlog was 50,004 events deep when lanes were introduced. A
    lane created at ``0`` would have inherited all of it and reproduced the
    stall it was built to fix.
    """
    client = _FakeRedisClient()
    bus = _bus(client, supervise_interval_seconds=0.05)
    bus.subscribe(NotificationRequested.__name__, _noop, lane="scanner")

    runner = asyncio.create_task(bus.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if any(group == "worker.scanner" for group, _ in client.groups_created):
            break
    bus.stop()
    await asyncio.wait_for(runner, timeout=2.0)

    assert ("worker.scanner", "$") in client.groups_created


# -- startup ordering: the bug that reached production -------------------------


async def test_a_lane_registered_after_run_still_gets_a_loop() -> None:
    """The regression, and it is not hypothetical — it was deployed.

    ``ServiceProcess.run`` spawns ``bus.run()`` *before* ``await self.setup()``,
    and ``setup()`` is where the twelve runtimes subscribe. The first version of
    ``run`` gathered the lanes it could see at call time, which was none: the
    Worker came up with one default-lane loop holding no handlers — reading and
    acking every event without dispatching it — and twelve lanes with no loop.
    The decision path stopped for the duration.

    It passed a suite of twelve lane tests because every one of them subscribed
    first and ran second. This one reverses that order, which is the order the
    platform actually uses.
    """
    client = _FakeRedisClient()
    bus = _bus(client, supervise_interval_seconds=0.05)

    runner = asyncio.create_task(bus.run())
    await asyncio.sleep(0.05)  # the bus is up, and nothing has subscribed yet

    bus.subscribe(NotificationRequested.__name__, _noop, lane="scanner")
    bus.subscribe(NotificationRequested.__name__, _noop, lane="committee")

    for _ in range(40):  # give the supervisor a few turns
        await asyncio.sleep(0.05)
        if {"worker.scanner", "worker.committee"} <= {g for g, _ in client.groups_created}:
            break

    bus.stop()
    await asyncio.wait_for(runner, timeout=2.0)

    created = {group for group, _ in client.groups_created}
    assert "worker.scanner" in created, "a lane registered after run() never got a loop"
    assert "worker.committee" in created


async def test_the_supervisor_shuts_every_lane_down() -> None:
    """Stop must reach the lanes it started, not just the supervisor."""
    client = _FakeRedisClient()
    bus = _bus(client, supervise_interval_seconds=0.05)
    bus.subscribe(NotificationRequested.__name__, _noop, lane="scanner")

    runner = asyncio.create_task(bus.run())
    await asyncio.sleep(0.15)
    bus.stop()
    await asyncio.wait_for(runner, timeout=2.0)

    assert runner.done() and not runner.cancelled()


# -- the actual point: one slow lane must not stall another --------------------


async def test_a_slow_lane_does_not_block_a_fast_one() -> None:
    """The 8-hour backlog, reproduced in miniature and then not reproduced.

    ``slow`` blocks on an event that is only set after ``fast`` has run. Under
    the old single-loop design the two handlers shared one serial dispatch, so
    this arrangement could not complete: whichever ran first would wait forever
    for the other. It completes only because the lanes turn independently.
    """
    fields = _fields()
    client = _FakeRedisClient(
        batches={"worker.slow": [("1-0", fields)], "worker.fast": [("1-0", fields)]}
    )
    bus = _bus(client)

    fast_ran = asyncio.Event()
    order: list[str] = []

    async def slow(_event: object) -> None:
        await asyncio.wait_for(fast_ran.wait(), timeout=2.0)
        order.append("slow")

    async def fast(_event: object) -> None:
        order.append("fast")
        fast_ran.set()

    bus.subscribe(NotificationRequested.__name__, slow, lane="slow")
    bus.subscribe(NotificationRequested.__name__, fast, lane="fast")

    await asyncio.wait_for(
        asyncio.gather(
            bus._lane("slow")._dispatch(fields),
            bus._lane("fast")._dispatch(fields),
        ),
        timeout=3.0,
    )
    assert order == ["fast", "slow"], "the lanes did not run concurrently"


async def test_each_lane_acks_under_its_own_group() -> None:
    """Pending lists are per group; acking under the wrong one strands work."""
    fields = _fields()
    client = _FakeRedisClient(
        batches={"worker.scanner": [("7-0", fields)], "worker.risk": [("9-0", fields)]}
    )
    bus = _bus(client)
    bus.subscribe(NotificationRequested.__name__, _noop, lane="scanner")
    bus.subscribe(NotificationRequested.__name__, _noop, lane="risk")

    await bus._lane("scanner")._consume_once()
    await bus._lane("risk")._consume_once()

    assert ("worker.scanner", "7-0") in client.acked
    assert ("worker.risk", "9-0") in client.acked


# -- health: a stalled lane must not hide behind healthy ones ------------------


async def test_last_cycle_reports_the_oldest_lane_not_the_freshest() -> None:
    """Eleven healthy loops must not mask one that stopped.

    ``last_cycle_at`` is what ``/health`` reads to assert the bus is consuming
    rather than assume it. Reporting the freshest lane would rebuild exactly the
    blind spot the property was added to remove.
    """
    client = _FakeRedisClient()
    bus = _bus(client)
    bus.subscribe(NotificationRequested.__name__, _noop, lane="a")
    bus.subscribe(NotificationRequested.__name__, _noop, lane="b")

    await bus._lane("a")._consume_once()
    await asyncio.sleep(0.01)
    await bus._lane("b")._consume_once()

    oldest = bus._lane("a").last_cycle_at
    newest = bus._lane("b").last_cycle_at
    assert oldest is not None and newest is not None and oldest < newest
    assert bus.last_cycle_at == oldest


async def test_a_lane_that_never_turned_makes_the_bus_unknown_not_healthy() -> None:
    client = _FakeRedisClient()
    bus = _bus(client)
    bus.subscribe(NotificationRequested.__name__, _noop, lane="a")
    bus.subscribe(NotificationRequested.__name__, _noop, lane="b")

    await bus._lane("a")._consume_once()

    assert bus._lane("b").last_cycle_at is None
    assert bus.last_cycle_at is None, "one dead lane must not read as healthy"


async def test_a_bus_with_no_subscriptions_still_holds_a_loop() -> None:
    """A service that consumes nothing must not *look* like it is consuming."""
    client = _FakeRedisClient()
    bus = _bus(client)
    bus.stop()
    await bus.run()
    assert bus.lanes == (DEFAULT_LANE,)


# -- LaneBus: the contexts never learn what a lane is --------------------------


def test_the_lane_view_pins_every_subscription_to_its_lane() -> None:
    bus = _bus(_FakeRedisClient())
    view = LaneBus(bus, "scanner")
    view.subscribe(NotificationRequested.__name__, _noop)
    assert bus.lanes == ("scanner",)


def test_an_explicit_lane_still_wins_over_the_view() -> None:
    """The view supplies a default; it does not overrule a caller who means it."""
    bus = _bus(_FakeRedisClient())
    view = LaneBus(bus, "scanner")
    view.subscribe(NotificationRequested.__name__, _noop, lane="committee")
    assert bus.lanes == ("committee",)


async def test_the_lane_view_forwards_publishing_untouched() -> None:
    """Lanes partition consumption only — there is still exactly one stream."""
    published: list[object] = []

    class _Recorder:
        def subscribe(self, *a: object, **k: object) -> None: ...

        async def publish(self, event: object) -> None:
            published.append(event)

        async def publish_many(self, events: list[object]) -> None:
            published.extend(events)

    view = LaneBus(_Recorder(), "scanner")  # type: ignore[arg-type]
    await view.publish("one")  # type: ignore[arg-type]
    await view.publish_many(["two", "three"])  # type: ignore[arg-type]
    assert published == ["one", "two", "three"]
