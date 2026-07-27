"""Discord webhook notifier — the only notification transport (Phase 2).

Implements the :class:`Notifier` port. Discord is deliberately the sole channel
(no Telegram, Slack or email). Delivery is resilient: severity-coloured embeds,
bounded retries with backoff, and a per-minute rate limit so a burst of alerts
can never hammer the webhook.
"""

from __future__ import annotations

import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from hades.contexts.notification.domain.ports import Notification, Notifier, Severity
from hades.contexts.notification.infrastructure.embed_builder import EmbedBuilder
from hades.shared_kernel.logging import get_logger

_logger = get_logger("discord")


class DiscordNotifier(Notifier):
    """Delivers notifications to a Discord channel via an incoming webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_seconds: float = 10.0,
        retry_attempts: int = 3,
        rate_limit_per_minute: int = 30,
        alerts_webhook_url: str = "",
        trades_webhook_url: str = "",
    ) -> None:
        self._webhook_url = webhook_url
        self._alerts_webhook_url = alerts_webhook_url or webhook_url
        self._trades_webhook_url = trades_webhook_url or webhook_url
        self._timeout = timeout_seconds
        self._retry_attempts = retry_attempts
        self._rate_limit = rate_limit_per_minute
        self._window_start = time.monotonic()
        self._sent_in_window = 0
        self._embeds = EmbedBuilder()

    @property
    def channel(self) -> str:
        return "discord"

    #: Topic tags that belong in the dedicated trades channel. Both spellings are
    #: accepted: every emitter tags ``"trades"``, but this router only ever matched
    #: ``"trade"``, so a configured trades webhook silently received nothing and
    #: fills fell back to the main channel.
    _TRADE_TOPICS = frozenset({"trade", "trades"})

    def _route(self, notification: Notification) -> str:
        topic = notification.tags.get("topic", "")
        if topic in self._TRADE_TOPICS:
            return self._trades_webhook_url
        if notification.severity in (Severity.WARNING, Severity.CRITICAL):
            return self._alerts_webhook_url
        return self._webhook_url

    def _allow(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= 60.0:
            self._window_start = now
            self._sent_in_window = 0
        if self._sent_in_window >= self._rate_limit:
            return False
        self._sent_in_window += 1
        return True

    async def send(self, notification: Notification) -> None:
        if not self._webhook_url:
            _logger.warning("discord_not_configured", title=notification.title)
            return
        if not self._allow():
            _logger.warning("discord_rate_limited", title=notification.title)
            return

        payload = {"embeds": [self._embeds.build(notification)]}

        @retry(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=0.5, max=8),
            reraise=True,
        )
        async def _post() -> None:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._route(notification), json=payload)
                resp.raise_for_status()

        try:
            await _post()
            _logger.info("discord_sent", title=notification.title, severity=notification.severity)
        except httpx.HTTPError as exc:
            _logger.error("discord_send_failed", title=notification.title, error=str(exc))
            raise
