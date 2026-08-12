"""Credentials must never reach a log sink.

Written after a live Helius API key was found in container logs: RPC providers
put the credential in the query string and `httpx` logs the full request URL at
INFO. Because the dashboard terminal tails the same stream, that key was visible
to anyone who opened the dashboard.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from hades.shared_kernel.logging.setup import (
    _NOISY_LOGGERS,
    _quiet_noisy_libraries,
    _redact_record,
    _SecretRedactingFilter,
    redact_secrets,
)

_KEY = "7ba220fe-1b83-4c5a-84e6-1525d2d013aa"


def test_an_rpc_api_key_in_a_query_string_is_removed() -> None:
    url = f"https://mainnet.helius-rpc.com/?api-key={_KEY}"

    out = redact_secrets(f"HTTP Request: POST {url} 200 OK")

    assert _KEY not in out
    assert "api-key=***" in out
    assert "helius-rpc.com" in out  # the host stays — only the secret goes


def test_the_common_credential_parameter_spellings_are_all_caught() -> None:
    for param in ("api-key", "api_key", "apikey", "token", "access_token", "secret", "password"):
        out = redact_secrets(f"https://x.test/?{param}=supersecretvalue")
        assert "supersecretvalue" not in out, param


def test_matching_is_case_insensitive() -> None:
    assert "SECRETVAL" not in redact_secrets("https://x.test/?API-KEY=SECRETVAL")


def test_a_discord_webhook_token_is_removed() -> None:
    url = "https://discord.com/api/webhooks/123456789/aBcDeF-ghIjKlMnOpQrStUv"

    out = redact_secrets(url)

    assert "aBcDeF-ghIjKlMnOpQrStUv" not in out
    assert "webhooks/123456789/***" in out


def test_other_query_parameters_are_left_alone() -> None:
    """Over-redacting would make logs useless; only credentials are touched."""
    url = "https://api.test/v1/pairs?chain=solana&limit=50"

    assert redact_secrets(url) == url


def test_a_string_without_secrets_is_unchanged() -> None:
    assert redact_secrets("trade_filled mint=abc notional_usd=100.0") == (
        "trade_filled mint=abc notional_usd=100.0"
    )


def test_the_filter_scrubs_a_log_record_message() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"HTTP Request: POST https://rpc.test/?api-key={_KEY}",
        args=None,
        exc_info=None,
    )

    assert _SecretRedactingFilter().filter(record) is True
    assert _KEY not in str(record.msg)


def test_the_filter_scrubs_record_arguments() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling %s",
        args=(f"https://rpc.test/?token={_KEY}",),
        exc_info=None,
    )

    _SecretRedactingFilter().filter(record)

    assert _KEY not in str(record.args)


def test_the_filter_never_raises_into_the_caller() -> None:
    """A logging filter that throws would break the call site it only observes."""

    class _Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=_Hostile(),  # type: ignore[arg-type]
        args=None,
        exc_info=None,
    )

    assert _SecretRedactingFilter().filter(record) is True


def test_the_ring_buffer_record_is_scrubbed() -> None:
    """The dashboard terminal tails this buffer — a wider audience than the logs."""
    record = _redact_record(
        {"event": "rpc_call", "url": f"https://rpc.test/?api-key={_KEY}", "attempt": 1}
    )

    assert _KEY not in record["url"]
    assert record["attempt"] == 1  # non-strings pass through untouched


def test_the_filter_scrubs_a_structlog_event_dict() -> None:
    """A structlog record carries a *dict* in ``msg``, not a string.

    The filter tested only for ``str``, so every line this platform emits itself
    went through untouched — while the ``httpx`` case above passed.
    """
    record = logging.LogRecord(
        name="solana.rpc",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg={  # type: ignore[arg-type]
            "event": "rpc_call_failed",
            "error": f"401 Unauthorized for url 'https://rpc.test/?api-key={_KEY}'",
        },
        args=None,
        exc_info=None,
    )

    _SecretRedactingFilter().filter(record)

    assert _KEY not in str(record.msg)


def test_secrets_nested_below_the_top_level_are_scrubbed() -> None:
    record = _redact_record(
        {
            "event": "rpc_failover",
            "context": {"url": f"https://rpc.test/?api-key={_KEY}"},
            "tried": [f"https://a.test/?token={_KEY}"],
        }
    )

    assert _KEY not in str(record)


def _render_through_configured_logging(tmp_path: Any, to_file: bool) -> tuple[str, str]:
    """Emit one structlog warning through a freshly configured pipeline.

    Returns ``(stdout_text, file_text)``. This is the end-to-end path; every
    other test in this module exercises a helper in isolation, which is precisely
    how a live key reached container logs with all of them green.
    """
    import io

    import structlog

    from hades.shared_kernel.logging import setup as log_setup

    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    log_setup._CONFIGURED = False
    structlog.reset_defaults()
    try:
        log_setup.configure_logging(
            level="INFO", fmt="json", to_file=to_file, directory=str(tmp_path)
        )
        captured = io.StringIO()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.stream = captured

        log_setup.get_logger("solana.rpc").warning(
            "rpc_call_failed",
            endpoint="default",
            error=f"401 Unauthorized for url 'https://mainnet.helius-rpc.com/?api-key={_KEY}'",
        )
        for handler in root.handlers:
            handler.flush()

        combined = tmp_path / "hades.log"
        file_text = combined.read_text(encoding="utf-8") if combined.exists() else ""
        return captured.getvalue(), file_text
    finally:
        for handler in root.handlers:
            # Rotating file handlers hold the file open; leaving them to the
            # garbage collector raises at interpreter shutdown, which this
            # project's warnings-as-errors turns into a failure.
            with contextlib.suppress(Exception):
                handler.close()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
        log_setup._CONFIGURED = False
        structlog.reset_defaults()


def test_a_structured_log_line_reaches_stdout_scrubbed(tmp_path: Any) -> None:
    """The production shape: `rpc_call_failed` with the key inside the error text."""
    stdout_text, _ = _render_through_configured_logging(tmp_path, to_file=False)

    assert _KEY not in stdout_text
    assert "rpc_call_failed" in stdout_text  # the line is still emitted, and still useful
    assert "helius-rpc.com" in stdout_text


def test_the_rotating_file_sink_is_scrubbed_too(tmp_path: Any) -> None:
    """Redaction used to hang off the stdout handler alone; files kept the secret."""
    _, file_text = _render_through_configured_logging(tmp_path, to_file=True)

    assert file_text, "expected the combined log file to have been written"
    assert _KEY not in file_text


def test_request_narrating_clients_are_raised_to_warning() -> None:
    """Belt and braces: the URL never gets logged in the first place."""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    _quiet_noisy_libraries()

    for name in _NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name
