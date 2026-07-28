"""Uniform Discord embed builder — one professional look for every alert.

Phase 10 gives every notification a single, consistent presentation. Instead of
each call site hand-rolling an embed, they pass structured facts in
``Notification.tags`` and this builder renders them into a professional embed:

- a **category** (INFO / WARNING / ERROR / CRITICAL / SUCCESS) that drives the
  side colour, derived from the severity but overridable via a ``category`` tag
  (so a successful fill can be green even though the severity enum has no
  ``SUCCESS`` member);
- a fixed, readable ordering of the *known* operational fields (mode, token,
  pnl, roi, wallet, security score, AI confidence, strategy, execution time,
  host, container, timestamp) — each shown only when present;
- any remaining tags appended as extra fields;
- a uniform footer and an ISO-8601 timestamp.

The builder is pure (no I/O) and backward compatible: a notification with no
special tags still renders a clean title/description embed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Final

from hades.contexts.notification.domain.ports import Notification, Severity

# Discord embed limits (documented so the truncation below is self-explanatory).
_MAX_FIELDS: Final = 25
_TITLE_MAX: Final = 256
_DESC_MAX: Final = 4000
_FIELD_NAME_MAX: Final = 256
_FIELD_VALUE_MAX: Final = 1024


class Category:
    """The five notification categories the platform standardises on."""

    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Side colour per category (decimal RGB).
_CATEGORY_COLORS: Final[dict[str, int]] = {
    Category.INFO: 0x3B82F6,  # blue
    Category.SUCCESS: 0x22C55E,  # green
    Category.WARNING: 0xF59E0B,  # amber
    Category.ERROR: 0xEF4444,  # red
    Category.CRITICAL: 0xDC2626,  # deep red
}

# Default category inferred from the severity enum when no ``category`` tag is set.
_SEVERITY_CATEGORY: Final[dict[Severity, str]] = {
    Severity.DEBUG: Category.INFO,
    Severity.INFO: Category.INFO,
    Severity.WARNING: Category.WARNING,
    Severity.CRITICAL: Category.CRITICAL,
}

# Known operational tags, in the order they should appear, with display labels
# and whether Discord should render them inline (compact) or full-width.
_KNOWN_FIELDS: Final[tuple[tuple[str, str, bool], ...]] = (
    ("mode", "Mode", True),
    ("token", "Token", True),
    ("strategy", "Strategy", True),
    ("pnl", "PnL", True),
    ("roi", "ROI", True),
    ("security_score", "Security", True),
    ("ai_confidence", "AI Confidence", True),
    ("execution_time", "Exec Time", True),
    ("wallet", "Wallet", False),
)

# Tags that are metadata for the builder itself, not fields to render as columns.
_META_TAGS: Final[frozenset[str]] = frozenset({"category", "timestamp", "host", "container"})


class EmbedBuilder:
    """Renders a :class:`Notification` into a uniform Discord embed payload."""

    def __init__(self, *, host: str | None = None, container: str | None = None) -> None:
        # Host/container default to the running environment so every embed is
        # attributable, but a tag always wins (useful for cross-host relays).
        self._host = host or os.getenv("HOSTNAME", "")
        self._container = container or os.getenv("HADES_ROLE", "")

    def category_of(self, notification: Notification) -> str:
        raw = notification.tags.get("category", "").upper()
        if raw in _CATEGORY_COLORS:
            return raw
        return _SEVERITY_CATEGORY.get(notification.severity, Category.INFO)

    def color_of(self, notification: Notification) -> int:
        return _CATEGORY_COLORS[self.category_of(notification)]

    def build(self, notification: Notification) -> dict[str, object]:
        """Return a single Discord embed dict for ``notification``."""
        category = self.category_of(notification)
        fields = self._fields(notification)
        timestamp = notification.tags.get("timestamp") or datetime.now(UTC).isoformat()

        footer_bits = [f"Hades · {category}"]
        host = notification.tags.get("host") or self._host
        container = notification.tags.get("container") or self._container
        if container:
            footer_bits.append(container)
        if host:
            footer_bits.append(host)

        return {
            "title": notification.title[:_TITLE_MAX],
            "description": notification.body[:_DESC_MAX],
            "color": _CATEGORY_COLORS[category],
            "fields": fields,
            "footer": {"text": " · ".join(footer_bits)[:_FIELD_VALUE_MAX]},
            "timestamp": timestamp,
        }

    def _fields(self, notification: Notification) -> list[dict[str, object]]:
        fields: list[dict[str, object]] = []
        tags = notification.tags

        # 1) Known operational fields first, in their canonical order.
        for key, label, inline in _KNOWN_FIELDS:
            value = tags.get(key)
            if value:
                fields.append(self._field(label, value, inline))

        # 2) Any remaining, non-meta tags as extra inline fields.
        known = {k for k, _, _ in _KNOWN_FIELDS}
        for key, value in tags.items():
            if key in known or key in _META_TAGS or not value:
                continue
            fields.append(self._field(key.replace("_", " ").title(), value, True))

        return fields[:_MAX_FIELDS]

    @staticmethod
    def _field(name: str, value: str, inline: bool) -> dict[str, object]:
        return {
            "name": name[:_FIELD_NAME_MAX],
            "value": str(value)[:_FIELD_VALUE_MAX],
            "inline": inline,
        }
