"""Discord embeds are uniform: category colours + only-present fields."""

from __future__ import annotations

from hades.contexts.notification.domain.ports import Notification, Severity
from hades.contexts.notification.infrastructure.discord_notifier import DiscordNotifier
from hades.contexts.notification.infrastructure.embed_builder import (
    _CATEGORY_COLORS,
    Category,
    EmbedBuilder,
)


def _note(severity: Severity, tags: dict[str, str]) -> Notification:
    return Notification(title="t", body="b", severity=severity, tags=tags)


def test_success_category_tag_overrides_severity_colour() -> None:
    builder = EmbedBuilder()
    embed = builder.build(_note(Severity.INFO, {"category": "SUCCESS"}))
    assert embed["color"] == _CATEGORY_COLORS[Category.SUCCESS]


def test_critical_severity_defaults_to_critical_category() -> None:
    builder = EmbedBuilder()
    note = _note(Severity.CRITICAL, {})
    assert builder.category_of(note) == Category.CRITICAL
    assert builder.build(note)["color"] == _CATEGORY_COLORS[Category.CRITICAL]


def test_only_present_fields_are_rendered() -> None:
    tags = {"mode": "paper", "pnl": "+5.0", "category": "INFO"}
    embed = EmbedBuilder().build(_note(Severity.INFO, tags))
    field_names = {f["name"] for f in embed["fields"]}  # type: ignore[index,union-attr]
    assert "Mode" in field_names
    assert "PnL" in field_names
    # Absent operational fields and meta tags are not rendered as columns.
    assert "ROI" not in field_names
    assert "Category" not in field_names


def test_footer_and_timestamp_present() -> None:
    embed = EmbedBuilder().build(_note(Severity.WARNING, {"container": "worker"}))
    assert "WARNING" in embed["footer"]["text"]  # type: ignore[index]
    assert embed["timestamp"]


# -- routing ------------------------------------------------------------------


def _notifier() -> DiscordNotifier:
    return DiscordNotifier(
        "https://discord.test/main",
        alerts_webhook_url="https://discord.test/alerts",
        trades_webhook_url="https://discord.test/trades",
    )


def test_trade_notifications_reach_the_dedicated_trades_channel() -> None:
    # Every emitter tags "trades" (plural). The router only matched the singular,
    # so a configured trades webhook silently received nothing.
    notification = Notification(
        title="BUY filled · BONK",
        body="100 USD @ 0.42",
        severity=Severity.INFO,
        tags={"topic": "trades", "mode": "paper"},
    )

    assert _notifier()._route(notification) == "https://discord.test/trades"


def test_warnings_reach_the_alerts_channel() -> None:
    notification = Notification(
        title="RPC degraded", body="latency", severity=Severity.WARNING, tags={"topic": "health"}
    )

    assert _notifier()._route(notification) == "https://discord.test/alerts"


def test_everything_else_falls_back_to_the_main_channel() -> None:
    notification = Notification(
        title="Study finished", body="", severity=Severity.INFO, tags={"topic": "research"}
    )

    assert _notifier()._route(notification) == "https://discord.test/main"


def test_unset_optional_webhooks_fall_back_to_the_main_one() -> None:
    # A single-webhook deployment must still receive trade notifications.
    notifier = DiscordNotifier("https://discord.test/only")
    notification = Notification(
        title="SELL filled", body="", severity=Severity.INFO, tags={"topic": "trades"}
    )

    assert notifier._route(notification) == "https://discord.test/only"
