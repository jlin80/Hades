"""Centralised structlog configuration.

Every service configures logging once at startup via :func:`configure_logging`,
then obtains bound loggers via :func:`get_logger`. Output is:

- **stdout** — JSON in production (ships straight to a log pipeline / Docker),
  coloured console in development;
- **rotating files** — a combined ``hades.log`` plus one file per functional
  domain (engine, api, dashboard, research, trading, risk, discord, watchdog),
  each size-rotated;
- an **in-memory ring buffer** — the live feed behind the dashboard terminal.

structlog is routed through the stdlib logging machinery so third-party loggers
(uvicorn, sqlalchemy) share the exact same handlers and formatting.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from hades.shared_kernel.logging.ring_buffer import get_log_buffer

_CONFIGURED = False

# logger-name prefix → domain log file. First match wins; everything also goes
# to the combined ``hades.log`` and to stdout.
_DOMAIN_PREFIXES: dict[str, tuple[str, ...]] = {
    "engine": ("engine",),
    "api": ("api",),
    "dashboard": ("dashboard",),
    "research": ("research",),
    "trading": ("execution", "portfolio", "trade", "market"),
    "risk": ("risk",),
    "discord": ("discord", "notification"),
    "watchdog": ("watchdog", "monitoring", "healthcheck"),
}


def _domain_for(name: str) -> str | None:
    for domain, prefixes in _DOMAIN_PREFIXES.items():
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            return domain
    return None


class _DomainFilter(logging.Filter):
    """Passes only records whose logger name maps to ``domain``."""

    def __init__(self, domain: str) -> None:
        super().__init__()
        self._domain = domain

    def filter(self, record: logging.LogRecord) -> bool:
        return _domain_for(record.name) == self._domain


#: Optional cross-process shipper. Set by a process that wants its lines visible
#: in the dashboard's terminal; ``None`` (the default, and every unit test) keeps
#: logging purely local.
_shipper: Any | None = None


def set_log_shipper(shipper: Any | None) -> None:
    """Install (or clear) the shipper the ring-buffer processor also feeds.

    ``shipper`` needs only a ``submit(record)`` method that never blocks or
    raises — see :mod:`hades.shared_kernel.logging.shipper`.
    """
    global _shipper
    _shipper = shipper


def _ring_buffer_processor(
    _logger: Any, _method: str, event_dict: Mapping[str, Any]
) -> Mapping[str, Any]:
    """structlog processor: mirror each record into the terminal ring buffer.

    Also hands the record to the cross-process shipper when one is installed, so
    the dashboard can show the Worker's pipeline and not just the API's own lines.
    Shipping failures are swallowed here as a last resort: a logging processor
    that raises would break the very call site it was only meant to observe.
    """
    # Scrub before the record leaves this process. The ring buffer is tailed by
    # the dashboard terminal and the shipper republishes to Redis, so anything
    # unredacted here is visible to every dashboard viewer — a wider audience
    # than container logs, and the reason redaction cannot live only in a
    # stdout handler.
    record = _redact_record(dict(event_dict))
    get_log_buffer().append(record)
    if _shipper is not None:
        with contextlib.suppress(Exception):
            _shipper.submit(record)
    return event_dict


def _redact_record(record: dict[str, Any]) -> dict[str, Any]:
    for key, value in record.items():
        if isinstance(value, str):
            record[key] = redact_secrets(value)
    return record


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    to_file: bool = False,
    directory: str = "/var/log/hades",
    rotation_max_bytes: int = 50_000_000,
    rotation_backups: int = 10,
    ring_buffer_size: int = 2000,
) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    get_log_buffer(maxlen=ring_buffer_size)
    log_level = getattr(logging, level.upper(), logging.INFO)

    pre_chain: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *pre_chain,
            _ring_buffer_processor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(_SecretRedactingFilter())
    root.addHandler(stdout_handler)

    _quiet_noisy_libraries()

    if to_file:
        _attach_file_handlers(root, formatter, directory, rotation_max_bytes, rotation_backups)

    _CONFIGURED = True


# -- secret redaction ---------------------------------------------------------
#
# RPC providers put the credential in the query string, and `httpx` logs the full
# request URL at INFO. That single line put a live Helius API key into container
# logs, into the Redis log stream and — because the dashboard terminal tails that
# stream — onto anyone's screen who opened the dashboard.
#
# Two defences, because either alone is brittle: the noisy client loggers are
# raised to WARNING (they have no business narrating every request), and every
# record that still reaches a handler is scrubbed. The scrub is last-resort and
# deliberately pattern-based rather than key-name-based: a secret leaks through
# whatever string happens to carry it, so matching the *shape* catches URLs we
# never anticipated.

_SECRET_PATTERNS = (
    # `?api-key=…`, `&apikey=…`, `?token=…`, `&access_token=…` in any URL.
    re.compile(
        r"((?:api[-_]?key|access[-_]?token|token|secret|password|auth)=)[^&\s\"']+",
        re.IGNORECASE,
    ),
    # Discord webhooks: the trailing segment is the credential.
    re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+", re.IGNORECASE),
)

_REDACTED = "***"


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with a placeholder."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(rf"\1{_REDACTED}", text)
    return text


class _SecretRedactingFilter(logging.Filter):
    """Scrubs credentials out of every record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact_secrets(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact_secrets(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # logging must never raise into the caller
            return True
        return True


#: Clients that narrate every request at INFO. The request URL is exactly where
#: credentials live, so these are raised to WARNING rather than merely scrubbed.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "websockets.client")


def _quiet_noisy_libraries() -> None:
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _attach_file_handlers(
    root: logging.Logger,
    formatter: logging.Formatter,
    directory: str,
    max_bytes: int,
    backups: int,
) -> None:
    """Add the combined + per-domain rotating file handlers (best effort)."""
    try:
        log_dir = Path(directory)
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Filesystem not writable (e.g. dev host without the volume) — stdout is
        # still active, so logging keeps working; just skip file sinks.
        return

    combined = RotatingFileHandler(
        log_dir / "hades.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    combined.setFormatter(formatter)
    root.addHandler(combined)

    for domain in _DOMAIN_PREFIXES:
        handler = RotatingFileHandler(
            log_dir / f"{domain}.log",
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.addFilter(_DomainFilter(domain))
        root.addHandler(handler)


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, optionally pre-bound with context key/values."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger
