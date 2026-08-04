"""Notification Service — the single delivery authority.

Subscribes to :class:`NotificationRequested` events and decides whether and how
to deliver each (severity gating + channel routing). No other component ever
talks to a transport. Every notification is optionally persisted so delivery is
auditable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
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
        dedup_window_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notifiers = {n.channel: n for n in notifiers}
        self._min_severity = min_severity
        self._recorder = recorder
        self._dedup_window = max(0.0, dedup_window_seconds)
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        # A missing notifier is a *configuration* fact, not a per-event failure:
        # it does not change between events, so warning on every one buried the
        # real log under thousands of identical lines. Warn once per channel.
        self._warned_channels: set[str] = set()

    def _suppressed(self, event: NotificationRequested) -> bool:
        """True if this alert is a repeat of one delivered inside the window.

        `NotificationRequested` has carried a ``dedup_key`` since it was defined
        and nothing ever read it — the same shape of defect this audit keeps
        finding: a field that looks like a feature and gates nothing. Meanwhile a
        deployment sent "Recovered: postgres" every few seconds for a database
        that was never down, because the situation was re-detected on every probe
        tick and each detection was treated as news.

        Suppression is per key and per severity, so an INFO that escalates to
        CRITICAL is always delivered — an alert getting worse is new information
        even when its text is identical. When no ``dedup_key`` is given the title
        stands in for one, which makes the default behaviour useful without
        every caller having to opt in.

        CRITICAL is never suppressed. The cost of one repeated critical alert is
        noise; the cost of swallowing one is the thing this platform exists to
        avoid.
        """
        if self._dedup_window <= 0 or event.severity is Severity.CRITICAL:
            return False
        key = f"{event.channel}:{event.severity.value}:{event.dedup_key or event.title}"
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < self._dedup_window:
            _logger.debug(
                "notification_deduplicated",
                title=event.title,
                key=key,
                seconds_since_last=round(now - last, 1),
            )
            return True
        self._last_sent[key] = now
        # Bound the memory: this dict is keyed by alert text, and a caller that
        # interpolates an id into a title would otherwise grow it forever.
        if len(self._last_sent) > 512:
            cutoff = now - self._dedup_window
            self._last_sent = {k: t for k, t in self._last_sent.items() if t > cutoff}
        return False

    def register(self, event_bus: EventBus) -> None:
        """Wire the service to the bus (call once at startup)."""
        event_bus.subscribe(NotificationRequested.__name__, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        if not isinstance(event, NotificationRequested):
            return
        if _SEVERITY_ORDER[event.severity] < _SEVERITY_ORDER[self._min_severity]:
            _logger.debug("notification_below_threshold", title=event.title)
            return

        if self._suppressed(event):
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
