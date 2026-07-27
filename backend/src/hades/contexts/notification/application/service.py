"""Notification Service — the single delivery authority.

Subscribes to :class:`NotificationRequested` events and decides whether and how
to deliver each (severity gating + channel routing). No other component ever
talks to a transport. Every notification is optionally persisted so delivery is
auditable.
"""

from __future__ import annotations

from typing import Protocol

from hades.contexts.notification.domain.events import NotificationRequested
from hades.contexts.notification.domain.ports import Notification, Notifier, Severity
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("notification")

_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.DEBUG: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.CRITICAL: 3,
}


class NotificationRecorder(Protocol):
    """Optional persistence for delivered/failed notifications (audit trail)."""

    async def record_pending(self, event: NotificationRequested) -> str: ...
    async def mark_sent(self, record_id: str) -> None: ...
    async def mark_failed(self, record_id: str, error: str) -> None: ...


class NotificationService:
    """Routes requested notifications to the right notifier, once, gated."""

    def __init__(
        self,
        notifiers: list[Notifier],
        *,
        min_severity: Severity = Severity.INFO,
        recorder: NotificationRecorder | None = None,
    ) -> None:
        self._notifiers = {n.channel: n for n in notifiers}
        self._min_severity = min_severity
        self._recorder = recorder
        # A missing notifier is a *configuration* fact, not a per-event failure:
        # it does not change between events, so warning on every one buried the
        # real log under thousands of identical lines. Warn once per channel.
        self._warned_channels: set[str] = set()

    def register(self, event_bus: EventBus) -> None:
        """Wire the service to the bus (call once at startup)."""
        event_bus.subscribe(NotificationRequested.__name__, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, NotificationRequested):
            return
        if _SEVERITY_ORDER[event.severity] < _SEVERITY_ORDER[self._min_severity]:
            _logger.debug("notification_below_threshold", title=event.title)
            return

        notifier = self._notifiers.get(event.channel)
        record_id: str | None = None
        if self._recorder is not None:
            record_id = await self._recorder.record_pending(event)

        if notifier is None:
            if event.channel not in self._warned_channels:
                self._warned_channels.add(event.channel)
                _logger.warning(
                    "no_notifier_for_channel",
                    channel=event.channel,
                    note=(
                        "notifications are recorded but not delivered — check "
                        "NOTIFY_DISCORD_ENABLED and NOTIFY_DISCORD_WEBHOOK_URL "
                        "reach the notification service. Logged once per channel."
                    ),
                )
            if self._recorder is not None and record_id is not None:
                await self._recorder.mark_failed(record_id, "no notifier for channel")
            return

        notification = Notification(
            title=event.title,
            body=event.body,
            severity=event.severity,
            tags=event.tags,
        )
        try:
            await notifier.send(notification)
        except Exception as exc:  # one delivery failure is not fatal
            _logger.error("notification_delivery_failed", title=event.title, error=str(exc))
            if self._recorder is not None and record_id is not None:
                await self._recorder.mark_failed(record_id, str(exc))
            return

        if self._recorder is not None and record_id is not None:
            await self._recorder.mark_sent(record_id)
