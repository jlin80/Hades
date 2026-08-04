"""Credentials must never reach a log sink.

Written after a live Helius API key was found in container logs: RPC providers
put the credential in the query string and `httpx` logs the full request URL at
INFO. Because the dashboard terminal tails the same stream, that key was visible
to anyone who opened the dashboard.
"""

from __future__ import annotations

import logging

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


def test_request_narrating_clients_are_raised_to_warning() -> None:
    """Belt and braces: the URL never gets logged in the first place."""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    _quiet_noisy_libraries()

    for name in _NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name
