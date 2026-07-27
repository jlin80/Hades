"""Auto-recovery heals what it can and escalates to Emergency Mode otherwise."""

from __future__ import annotations

from hades.contexts.monitoring.application.recovery import (
    EmergencyMode,
    RecoveryOrchestrator,
)
from hades.contexts.monitoring.infrastructure.recovery_actions import InMemoryEmergencyFlagStore
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.risk.domain.events import ACTION_ENTER_EMERGENCY, RiskControlCommandIssued
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import InMemoryEventBus


class _Action:
    def __init__(self, name: str, component: str, result: bool, *, raises: bool = False) -> None:
        self._name = name
        self._component = component
        self._result = result
        self._raises = raises

    @property
    def name(self) -> str:
        return self._name

    def handles(self, component: str) -> bool:
        return self._component in component

    async def attempt(self) -> bool:
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def _emergency() -> tuple[EmergencyMode, InMemoryEmergencyFlagStore, list[DomainEvent]]:
    bus = InMemoryEventBus()
    captured: list[DomainEvent] = []
    bus.subscribe(RiskControlCommandIssued.__name__, lambda e: _collect(captured, e))
    flags = InMemoryEmergencyFlagStore()
    emergency = EmergencyMode(event_bus=bus, notifier=NotificationPublisher(bus), flags=flags)
    return emergency, flags, captured


async def _collect(sink: list[DomainEvent], event: DomainEvent) -> None:
    sink.append(event)


async def test_emergency_activate_publishes_command_and_is_idempotent() -> None:
    emergency, flags, captured = _emergency()

    assert await emergency.activate("catastrophe") is True
    assert await flags.is_active() is True
    assert len(captured) == 1
    assert isinstance(captured[0], RiskControlCommandIssued)
    assert captured[0].action == ACTION_ENTER_EMERGENCY

    # Already active → no-op, no second command.
    assert await emergency.activate("again") is False
    assert len(captured) == 1


async def test_orchestrator_recovers_via_first_working_action() -> None:
    emergency, flags, _ = _emergency()
    orch = RecoveryOrchestrator(
        [_Action("bad", "redis", False), _Action("good", "redis", True)],
        emergency=emergency,
        notifier=NotificationPublisher(InMemoryEventBus()),
        max_attempts=3,
    )
    assert await orch.recover("redis", "no reply") is True
    assert await flags.is_active() is False


async def test_orchestrator_escalates_after_max_attempts() -> None:
    emergency, flags, _ = _emergency()
    orch = RecoveryOrchestrator(
        [_Action("always_fails", "postgres", False)],
        emergency=emergency,
        notifier=NotificationPublisher(InMemoryEventBus()),
        max_attempts=2,
    )
    assert await orch.recover("postgres", "down") is False
    assert await flags.is_active() is False  # not yet — one attempt left
    assert await orch.recover("postgres", "down") is False
    assert await flags.is_active() is True  # exhausted → Emergency Mode


async def test_raising_action_counts_as_failure() -> None:
    emergency, _, _ = _emergency()
    orch = RecoveryOrchestrator(
        [_Action("explodes", "rpc", True, raises=True)],
        emergency=emergency,
        notifier=NotificationPublisher(InMemoryEventBus()),
        max_attempts=5,
    )
    assert await orch.recover("rpc", "") is False


# -- escalation requires attempts to have actually been made ------------------


async def test_a_component_with_no_action_never_escalates_to_emergency() -> None:
    """The Watchdog can only reconnect its own Redis/Postgres and reload config.
    `api`, `dashboard`, `resources` and `rpc` have no action wired there, so
    counting "there was nothing to try" as a failed attempt tripped Emergency Mode
    after three cycles — halting risk-taking for a reason no recovery could clear.
    """
    emergency, flags, captured = _emergency()
    orchestrator = RecoveryOrchestrator(
        [_Action("redis_reconnect", "redis", True)], emergency=emergency, notifier=_publisher()
    )

    for _ in range(10):
        assert await orchestrator.recover("resources") is False

    assert await flags.is_active() is False
    assert captured == []


async def test_a_component_with_a_failing_action_still_escalates() -> None:
    # The guard above must not disarm real escalation.
    emergency, flags, _ = _emergency()
    orchestrator = RecoveryOrchestrator(
        [_Action("redis_reconnect", "redis", False)],
        emergency=emergency,
        notifier=_publisher(),
        max_attempts=3,
    )

    for _ in range(3):
        assert await orchestrator.recover("redis") is False

    assert await flags.is_active() is True


async def test_recovery_stops_retrying_once_exhausted() -> None:
    # Re-running actions every cycle produced "attempt=49 max_attempts=3", which
    # reads as nonsense and buries real events.
    action = _CountingAction("redis_reconnect", "redis")
    emergency, _flags, _ = _emergency()
    orchestrator = RecoveryOrchestrator(
        [action], emergency=emergency, notifier=_publisher(), max_attempts=3
    )

    for _ in range(20):
        await orchestrator.recover("redis")

    assert action.calls == 3


async def test_reset_lets_a_recovered_component_be_retried_again() -> None:
    action = _CountingAction("redis_reconnect", "redis")
    emergency, _flags, _ = _emergency()
    orchestrator = RecoveryOrchestrator(
        [action], emergency=emergency, notifier=_publisher(), max_attempts=3
    )

    for _ in range(5):
        await orchestrator.recover("redis")
    orchestrator.reset("redis")
    await orchestrator.recover("redis")

    assert action.calls == 4


class _CountingAction(_Action):
    def __init__(self, name: str, component: str) -> None:
        super().__init__(name, component, False)
        self.calls = 0

    async def attempt(self) -> bool:
        self.calls += 1
        return False


def _publisher() -> NotificationPublisher:
    return NotificationPublisher(InMemoryEventBus())
