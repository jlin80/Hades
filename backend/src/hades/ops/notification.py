"""Notification process — the single Discord delivery service.

Subscribes the :class:`NotificationService` to the event bus and consumes
``NotificationRequested`` events emitted by any module, delivering them via the
Discord notifier (the only transport). Persists every attempt for audit. This is
the *only* process that talks to Discord.
"""

from __future__ import annotations

from hades.contexts.notification.application.service import NotificationService
from hades.contexts.notification.domain.ports import Notifier, Severity
from hades.contexts.notification.infrastructure.discord_notifier import DiscordNotifier
from hades.contexts.notification.infrastructure.repository import NotificationRepository
from hades.ops.service import ServiceProcess


class NotificationProcess(ServiceProcess):
    """Hosts the Notification Service and its Discord notifier."""

    role = "notification"

    async def setup(self) -> None:
        settings = self._container.settings
        cfg = settings.notification

        notifiers: list[Notifier] = []
        if cfg.discord_enabled and cfg.discord_webhook_url:
            notifiers.append(
                DiscordNotifier(
                    cfg.discord_webhook_url,
                    timeout_seconds=cfg.timeout_seconds,
                    retry_attempts=cfg.retry_attempts,
                    rate_limit_per_minute=cfg.rate_limit_per_minute,
                    alerts_webhook_url=cfg.discord_alerts_webhook_url,
                    trades_webhook_url=cfg.discord_trades_webhook_url,
                )
            )
        else:
            self._log.warning("discord_disabled", note="notifications will be recorded, not sent")

        recorder = (
            NotificationRepository(self._container.database)
            if self._container.database is not None
            else None
        )
        service = NotificationService(
            notifiers,
            min_severity=Severity(cfg.min_severity),
            recorder=recorder,
            dedup_window_seconds=cfg.dedup_window_seconds,
        )
        service.register(self._container.event_bus)
        self._log.info("notification_ready", channels=[n.channel for n in notifiers])
