"""Notifications flow through events only, and the service gates by severity."""

from __future__ import annotations

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.application.service import NotificationService
from hades.contexts.notification.domain.ports import Notification, Notifier, Severity
from hades.shared_kernel.events import InMemoryEventBus


class _FakeNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    @property
    def channel(self) -> str:
        return "discord"

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


async def test_publisher_and_service_deliver_via_events() -> None:
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    service = NotificationService([notifier], min_severity=Severity.INFO)
    service.register(bus)

    publisher = NotificationPublisher(bus)
    await publisher.notify(title="hello", body="world", severity=Severity.WARNING)

    assert len(notifier.sent) == 1
    assert notifier.sent[0].title == "hello"
    assert notifier.sent[0].severity is Severity.WARNING


async def test_severity_below_threshold_is_dropped() -> None:
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    service = NotificationService([notifier], min_severity=Severity.WARNING)
    service.register(bus)

    await NotificationPublisher(bus).notify(title="debug", body="x", severity=Severity.INFO)
    assert notifier.sent == []


# --- deduplication: the alert channel has to stay worth reading -----------------
#
# `NotificationRequested` carried a `dedup_key` from the day it was defined and
# nothing ever read it. Meanwhile a deployment sent "Recovered: postgres" every
# few seconds for a database that was never down, because the situation was
# re-detected on every probe tick and each detection was delivered as news.


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _dedup_service(notifier: Notifier, clock: _Clock, window: float = 300.0) -> NotificationService:
    return NotificationService(
        [notifier], min_severity=Severity.INFO, dedup_window_seconds=window, clock=clock
    )


async def test_an_identical_alert_inside_the_window_is_sent_once() -> None:
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock).register(bus)
    publisher = NotificationPublisher(bus)

    for _ in range(20):
        clock.now += 5  # a probe tick
        await publisher.notify(title="Recovered: postgres", body="x", severity=Severity.INFO)

    assert len(notifier.sent) == 1, "the storm this was written to stop"


async def test_the_same_alert_is_sent_again_once_the_window_passes() -> None:
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock, window=300.0).register(bus)
    publisher = NotificationPublisher(bus)

    await publisher.notify(title="Recovered: postgres", body="x", severity=Severity.INFO)
    clock.now += 301
    await publisher.notify(title="Recovered: postgres", body="x", severity=Severity.INFO)

    assert len(notifier.sent) == 2, "suppression must be a quiet period, not a mute button"


async def test_an_escalating_alert_is_never_suppressed() -> None:
    """Same text, worse severity, is new information."""
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock).register(bus)
    publisher = NotificationPublisher(bus)

    await publisher.notify(title="postgres", body="x", severity=Severity.INFO)
    await publisher.notify(title="postgres", body="x", severity=Severity.WARNING)

    assert len(notifier.sent) == 2


async def test_critical_alerts_are_never_suppressed() -> None:
    """Repeating a critical costs noise; swallowing one costs the platform."""
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock).register(bus)
    publisher = NotificationPublisher(bus)

    for _ in range(5):
        await publisher.notify(title="KILL SWITCH", body="x", severity=Severity.CRITICAL)

    assert len(notifier.sent) == 5


async def test_dedup_key_groups_alerts_whose_text_differs() -> None:
    """A title carrying a changing number is still one situation."""
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock).register(bus)
    publisher = NotificationPublisher(bus)

    for i in range(10):
        clock.now += 5
        await publisher.notify(
            title=f"postgres unhealthy ({i} probes)",
            body="x",
            severity=Severity.WARNING,
            dedup_key="postgres-unhealthy",
        )

    assert len(notifier.sent) == 1


async def test_distinct_alerts_are_not_collapsed_into_each_other() -> None:
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock).register(bus)
    publisher = NotificationPublisher(bus)

    await publisher.notify(title="postgres unhealthy", body="x", severity=Severity.WARNING)
    await publisher.notify(title="redis unhealthy", body="x", severity=Severity.WARNING)

    assert len(notifier.sent) == 2


async def test_a_zero_window_disables_suppression_entirely() -> None:
    bus = InMemoryEventBus()
    notifier = _FakeNotifier()
    clock = _Clock()
    _dedup_service(notifier, clock, window=0.0).register(bus)
    publisher = NotificationPublisher(bus)

    for _ in range(3):
        await publisher.notify(title="same", body="x", severity=Severity.INFO)

    assert len(notifier.sent) == 3
