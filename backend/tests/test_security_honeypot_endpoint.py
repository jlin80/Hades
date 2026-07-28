"""The honeypot probe's quote endpoint must be live and operator-overridable.

A deployment spent hours approving nothing at all. Jupiter had retired
``quote-api.jup.ag`` and the hostname stopped resolving, so every sellability
probe failed. Nothing crashed: the probe is deliberately forgiving ("doubt, not
crash") and an unreachable route reads as an unproven token, so the Security
Engine rejected every candidate while every component reported itself healthy.

The URL was also hardcoded, which made a third party's routine endpoint
retirement into something only a new release could fix.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.security.infrastructure.swap_simulator import (
    DEFAULT_QUOTE_URL,
    JupiterSwapSimulator,
)
from hades.shared_kernel.config.settings import SecuritySettings

_MINT = "E" * 44


def test_the_default_endpoint_is_not_the_retired_host() -> None:
    """`quote-api.jup.ag` no longer resolves — it must never be the default."""
    assert "quote-api.jup.ag" not in DEFAULT_QUOTE_URL
    assert DEFAULT_QUOTE_URL.startswith("https://")


def test_settings_default_matches_the_simulator_default() -> None:
    """The literal is duplicated to keep layering clean; it must not drift."""
    assert SecuritySettings().honeypot_quote_url == DEFAULT_QUOTE_URL


def test_the_quote_url_is_operator_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint retirement must be fixable from .env, not by a release.

    Exercised through the environment rather than a constructor kwarg, because
    the env var is the mechanism an operator actually has.
    """
    override = "https://api.jup.ag/swap/v1/quote"
    monkeypatch.setenv("SECURITY_HONEYPOT_QUOTE_URL", override)

    assert SecuritySettings().honeypot_quote_url == override


def test_the_simulator_requests_the_url_it_was_given() -> None:
    """A configured URL must actually reach the wire."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).split("?")[0])
        return httpx.Response(200, json={"outAmount": "1000", "priceImpactPct": "0.01"})

    custom = "https://api.jup.ag/swap/v1/quote"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    simulator = JupiterSwapSimulator(base_url=custom, client=client)

    probe = asyncio.run(
        simulator.probe(TokenRef(mint=TokenMint(address=_MINT)), amount_usd=50.0)
    )
    asyncio.run(client.aclose())

    assert seen and all(url == custom for url in seen)
    assert probe.buyable is True


def test_an_unreachable_endpoint_yields_doubt_rather_than_raising() -> None:
    """The forgiving behaviour is intended — it is the silence that was the bug."""

    def handler(request: httpx.Request) -> Any:
        raise httpx.ConnectError("[Errno -5] No address associated with hostname")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    simulator = JupiterSwapSimulator(client=client)

    probe = asyncio.run(
        simulator.probe(TokenRef(mint=TokenMint(address=_MINT)), amount_usd=50.0)
    )
    asyncio.run(client.aclose())

    # Unproven, not "safe" — a dead endpoint must never read as an approval.
    assert probe.buyable is None
    assert probe.sellable is None
