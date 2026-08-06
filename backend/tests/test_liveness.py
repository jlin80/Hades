"""Liveness heartbeat files back the background-service healthchecks."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hades.ops.liveness import Liveness
from hades.ops.service import ServiceProcess
from hades.shared_kernel.events import EventRegistry, RedisEventBus
from hades.shared_kernel.logging import get_logger


def test_touch_makes_file_fresh(tmp_path: Path) -> None:
    liveness = Liveness("worker", directory=str(tmp_path))
    assert liveness.age_seconds() is None  # not written yet
    assert liveness.is_fresh(60) is False

    liveness.touch()
    assert liveness.path.exists()
    assert liveness.is_fresh(60) is True
    age = liveness.age_seconds()
    assert age is not None and age < 5


def test_missing_file_is_not_fresh(tmp_path: Path) -> None:
    assert Liveness("engine", directory=str(tmp_path)).is_fresh(10) is False


# -- the heartbeat is a claim about consuming, not about existing --------------
#
# Twice now this platform has reported perfect health while doing nothing: the
# Worker's consumer loop died and four days passed, and on 2026-08-06 nine of
# twelve lanes came up with no loop and the decision path stopped. Both times the
# process was alive and the heartbeat said so, truthfully and uselessly.
#
# These tests exercise ServiceProcess rather than the bus, deliberately. The
# 2026-08-06 bug lived in the seam between them — bus.run() spawned before
# setup() registered anything — and a suite that only ever tested the bus in
# isolation had nothing to say about it.


class _FakeRedis:
    async def xgroup_create(self, *a: object, **k: object) -> None: ...
    async def xreadgroup(self, *a: object, **k: object) -> list[object]:
        return []

    async def xinfo_groups(self, *a: object) -> list[object]:
        return []


class _FakeProvider:
    def client(self) -> _FakeRedis:
        return _FakeRedis()


def _bus_with_dead_lane(*, dead: bool) -> RedisEventBus:
    """A real bus, because the isinstance check in the seam is part of the test.

    A stub would have passed while the production path returned early — which is
    the same shape of mistake as testing the bus alone and missing the seam.
    """

    async def _noop(_e: object) -> None: ...

    bus = RedisEventBus(_FakeProvider(), EventRegistry(), group="worker")  # type: ignore[arg-type]
    bus.subscribe("NotificationRequested", _noop, lane="scanner")
    if dead:
        # Registered long ago and never turned: the 2026-08-06 state exactly.
        bus._lane("scanner").registered_at = time.time() - 6_000
    else:
        bus._lane("scanner")._last_cycle_at = time.time()
    return bus


def _service(tmp_path: Path, bus: object) -> Any:
    """A ServiceProcess with its container faked down to what liveness touches."""
    service = ServiceProcess.__new__(ServiceProcess)
    service._container = SimpleNamespace(
        event_bus=bus,
        settings=SimpleNamespace(watchdog=SimpleNamespace(heartbeat_interval_seconds=0.01)),
    )
    service._log = get_logger("test")
    service._stop = asyncio.Event()
    service._liveness = Liveness("worker", directory=str(tmp_path))
    return service


async def _run_one_beat(service: Any) -> None:
    task = asyncio.create_task(service._liveness_loop())
    await asyncio.sleep(0.05)
    service._stop.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_a_lane_that_never_started_withholds_the_heartbeat(tmp_path: Path) -> None:
    """The signal that was missing: not consuming must read as not healthy."""
    await _run_one_beat(_service(tmp_path, _bus_with_dead_lane(dead=True)))

    assert not Liveness("worker", directory=str(tmp_path)).path.exists(), (
        "a service that is not consuming still claimed to be alive"
    )


async def test_a_consuming_service_still_heartbeats(tmp_path: Path) -> None:
    """The check must not be able to starve a healthy service."""
    await _run_one_beat(_service(tmp_path, _bus_with_dead_lane(dead=False)))
    assert Liveness("worker", directory=str(tmp_path)).is_fresh(60) is True


async def test_a_non_redis_bus_heartbeats_as_before(tmp_path: Path) -> None:
    """In-process transports have no lanes to stall; dev must not go unhealthy."""
    await _run_one_beat(_service(tmp_path, object()))
    assert Liveness("worker", directory=str(tmp_path)).is_fresh(60) is True


async def test_a_broken_stall_check_falls_back_to_heartbeating(tmp_path: Path) -> None:
    """Health reporting must never be able to break the thing it reports on."""
    bus = _bus_with_dead_lane(dead=False)

    def _explode(**_: object) -> tuple[str, ...]:
        raise RuntimeError("redis is having a day")

    bus.stalled_lanes = _explode  # type: ignore[method-assign]
    await _run_one_beat(_service(tmp_path, bus))

    assert Liveness("worker", directory=str(tmp_path)).is_fresh(60) is True
