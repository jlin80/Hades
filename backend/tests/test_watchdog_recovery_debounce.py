"""Recovery must not fire on a blip, and must not narrate every success.

A deployment sent a "Recovered: postgres" message to Discord every few seconds
for a database that was never down. Two defects compounded:

* Recovery fired on the *first* non-healthy probe. The remedy for Postgres is
  `engine.dispose()` — it tears down the connection pool — so one timed-out
  probe against a healthy database dropped every pooled connection and forced a
  reconnect storm. On a contended host that manufactures the outage it exists to
  repair; the same logs carried `FATAL: sorry, too many clients already`.

* Every success notified. Success also zeroes the attempt counter, so the
  "already exhausted" guard — which only engages after repeated *failures* —
  could never fire for a component that flaps.

The second is the one that does lasting damage: an operator who learns their
alert channel is noise stops reading it, and the alert that matters lands in a
channel nobody trusts.
"""

from __future__ import annotations

import asyncio

from hades.contexts.monitoring.application.recovery import RecoveryOrchestrator


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify(self, *, title: str, **_: object) -> None:
        self.sent.append(title)


class _Emergency:
    async def activate(self, *_: object, **__: object) -> bool:
        return False


class _Action:
    """A remedy that always reports success — like reconnecting a healthy DB."""

    def __init__(self) -> None:
        self.runs = 0

    @property
    def name(self) -> str:
        return "postgres_reconnect"

    def handles(self, component: str) -> bool:
        return "postgres" in component.lower()

    async def attempt(self) -> bool:
        self.runs += 1
        return True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _orchestrator(clock: _Clock, notifier: _Notifier, action: _Action) -> RecoveryOrchestrator:
    return RecoveryOrchestrator(
        [action],  # type: ignore[list-item]
        emergency=_Emergency(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        recovery_notice_interval_seconds=900.0,
        clock=clock,
    )


def test_a_flapping_component_is_announced_once_not_every_cycle() -> None:
    clock, notifier, action = _Clock(), _Notifier(), _Action()
    orch = _orchestrator(clock, notifier, action)

    async def flap() -> None:
        for _ in range(20):
            await orch.recover("postgres", "probe timed out")
            clock.now += 10.0  # ten seconds between cycles

    asyncio.run(flap())

    assert action.runs == 20, "the remedy still runs; only the narration is throttled"
    assert notifier.sent == ["Recovered: postgres"], notifier.sent


def test_a_recurrence_after_the_quiet_period_is_announced_again() -> None:
    """Throttling must not become silence — a later episode is still news."""
    clock, notifier, action = _Clock(), _Notifier(), _Action()
    orch = _orchestrator(clock, notifier, action)

    async def two_episodes() -> None:
        await orch.recover("postgres", "probe timed out")
        clock.now += 1000.0  # past the 900s quiet period
        await orch.recover("postgres", "probe timed out")

    asyncio.run(two_episodes())

    assert len(notifier.sent) == 2


def test_components_are_throttled_independently() -> None:
    """Postgres going quiet must not mute a different component's first alert."""
    clock, notifier = _Clock(), _Notifier()

    class _Any(_Action):
        def handles(self, component: str) -> bool:
            return True

    orch = _orchestrator(clock, notifier, _Any())

    async def two_components() -> None:
        await orch.recover("postgres", "x")
        await orch.recover("redis", "x")

    asyncio.run(two_components())

    assert sorted(notifier.sent) == ["Recovered: postgres", "Recovered: redis"]
