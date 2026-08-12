"""A metadata lookup must describe the token it was asked about.

DexScreener's ``/tokens/{mint}`` endpoint returns every pair the mint appears in,
on either side, in an order the caller does not control. Reading
``pairs[0]["baseToken"]`` therefore answers a different question — "what leads
the list?" — and gets away with it for as long as the mint happens to be a base.

Wrapped SOL is the quote of nearly every Solana pair, so the live book ended up
holding a position on ``So111...112`` labelled ``FOGO``: right mint, wrong name,
and nothing downstream able to tell. These pin the two properties that make that
impossible — match the requested mint, and answer nothing when it is absent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.scanner.infrastructure.metadata_providers import (
    DexScreenerMetadataProvider,
)

_SOL = "So11111111111111111111111111111111111111112"
_FOGO = "F" * 44
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _side(address: str, symbol: str, name: str) -> dict[str, str]:
    return {"address": address, "symbol": symbol, "name": name}


def _provider(payload: dict[str, Any]) -> DexScreenerMetadataProvider:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    return DexScreenerMetadataProvider(client=httpx.AsyncClient(transport=transport))


def _collect(payload: dict[str, Any], mint: str) -> Any:
    provider = _provider(payload)
    token = TokenRef(mint=TokenMint(address=mint))
    try:
        return asyncio.run(provider.collect(token))
    finally:
        asyncio.run(provider.aclose())


def test_the_symbol_of_a_neighbouring_token_is_never_borrowed() -> None:
    """The production shape: FOGO/SOL leading the list of SOL's pairs."""
    payload = {
        "pairs": [
            {
                "baseToken": _side(_FOGO, "FOGO", "Fogo"),
                "quoteToken": _side(_SOL, "SOL", "Wrapped SOL"),
            }
        ]
    }

    meta = _collect(payload, _SOL)

    assert meta is not None
    assert meta.symbol == "SOL"
    assert meta.name == "Wrapped SOL"


def test_the_matching_pair_is_found_even_when_it_is_not_the_first() -> None:
    payload = {
        "pairs": [
            {
                "baseToken": _side(_FOGO, "FOGO", "Fogo"),
                "quoteToken": _side(_USDC, "USDC", "USD Coin"),
            },
            {
                "baseToken": _side(_SOL, "SOL", "Wrapped SOL"),
                "quoteToken": _side(_USDC, "USDC", "USD Coin"),
            },
        ]
    }

    meta = _collect(payload, _SOL)

    assert meta is not None
    assert meta.symbol == "SOL"


def test_a_base_token_lookup_still_works() -> None:
    """The ordinary case must be untouched; this is a filter, not a rewrite."""
    payload = {
        "pairs": [
            {
                "baseToken": _side(_FOGO, "FOGO", "Fogo"),
                "quoteToken": _side(_SOL, "SOL", "Wrapped SOL"),
                "info": {"imageUrl": "https://img.test/fogo.png"},
            }
        ]
    }

    meta = _collect(payload, _FOGO)

    assert meta is not None
    assert meta.symbol == "FOGO"
    assert meta.logo_uri == "https://img.test/fogo.png"


def test_a_payload_that_never_mentions_the_mint_yields_nothing() -> None:
    """No metadata is a reportable state; a neighbour's metadata is not."""
    payload = {
        "pairs": [
            {
                "baseToken": _side(_FOGO, "FOGO", "Fogo"),
                "quoteToken": _side(_USDC, "USDC", "USD Coin"),
            }
        ]
    }

    assert _collect(payload, _SOL) is None


def test_malformed_pair_entries_are_skipped_rather_than_fatal() -> None:
    payload = {
        "pairs": [
            "not-a-pair",
            {"baseToken": None, "quoteToken": None},
            {
                "baseToken": _side(_SOL, "SOL", "Wrapped SOL"),
                "quoteToken": _side(_USDC, "USDC", "USD Coin"),
            },
        ]
    }

    meta = _collect(payload, _SOL)

    assert meta is not None
    assert meta.symbol == "SOL"
