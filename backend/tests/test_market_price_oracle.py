"""The price oracle grounds paper fills and marks positions to market."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.market.infrastructure.price_oracle import DexScreenerPriceOracle

_MINT_A = "So11111111111111111111111111111111111111112"
_MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _token(mint: str) -> TokenRef:
    return TokenRef(mint=TokenMint(address=mint))


def _pair(mint: str, price: str, liquidity: float) -> dict[str, Any]:
    return {
        "baseToken": {"address": mint},
        "priceUsd": price,
        "liquidity": {"usd": liquidity},
    }


def _oracle(
    payload: object, *, status: int = 200, ttl: float = 15.0
) -> tuple[DexScreenerPriceOracle, list[str]]:
    """An oracle wired to a transport that records the URLs it was asked for."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, content=json.dumps(payload))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        DexScreenerPriceOracle(
            url="https://example.test/tokens/", cache_ttl_seconds=ttl, client=client
        ),
        seen,
    )


async def test_it_reads_the_price_of_a_single_token() -> None:
    oracle, _ = _oracle({"pairs": [_pair(_MINT_A, "0.0432", 50_000)]})

    price = await oracle.price_usd(_token(_MINT_A))

    assert price == Decimal("0.0432")


async def test_it_prefers_the_deepest_pair() -> None:
    """A thin pool quotes an unreliable price; depth is the tiebreaker."""
    oracle, _ = _oracle(
        {
            "pairs": [
                _pair(_MINT_A, "0.99", 1_000),
                _pair(_MINT_A, "1.00", 900_000),
                _pair(_MINT_A, "1.20", 12_000),
            ]
        }
    )

    assert await oracle.price_usd(_token(_MINT_A)) == Decimal("1.00")


async def test_it_prices_several_tokens_in_one_request() -> None:
    oracle, seen = _oracle(
        {"pairs": [_pair(_MINT_A, "1.50", 10_000), _pair(_MINT_B, "1.00", 10_000)]}
    )

    prices = await oracle.prices_usd([_token(_MINT_A), _token(_MINT_B)])

    assert prices == {_MINT_A: Decimal("1.50"), _MINT_B: Decimal("1.00")}
    assert len(seen) == 1  # batched, not one request per token
    assert _MINT_A in seen[0] and _MINT_B in seen[0]


async def test_a_cached_quote_is_reused_within_its_ttl() -> None:
    oracle, seen = _oracle({"pairs": [_pair(_MINT_A, "2.00", 10_000)]}, ttl=60.0)

    await oracle.price_usd(_token(_MINT_A))
    await oracle.price_usd(_token(_MINT_A))
    await oracle.price_usd(_token(_MINT_A))

    assert len(seen) == 1


async def test_an_expired_quote_is_refetched() -> None:
    oracle, seen = _oracle({"pairs": [_pair(_MINT_A, "2.00", 10_000)]}, ttl=0.0)

    await oracle.price_usd(_token(_MINT_A))
    await oracle.price_usd(_token(_MINT_A))

    assert len(seen) == 2


async def test_an_unknown_mint_yields_no_price_rather_than_a_guess() -> None:
    oracle, _ = _oracle({"pairs": [_pair(_MINT_B, "1.00", 10_000)]})

    assert await oracle.price_usd(_token(_MINT_A)) is None


async def test_a_failing_endpoint_degrades_to_no_price() -> None:
    oracle, _ = _oracle({"error": "boom"}, status=503)

    assert await oracle.price_usd(_token(_MINT_A)) is None


async def test_a_malformed_payload_is_survived() -> None:
    oracle, _ = _oracle({"pairs": ["not-a-dict", {"baseToken": None}, {}]})

    assert await oracle.price_usd(_token(_MINT_A)) is None


async def test_a_non_positive_price_is_rejected() -> None:
    """A zero price would make every stop-loss fire at once."""
    oracle, _ = _oracle({"pairs": [_pair(_MINT_A, "0", 10_000)]})

    assert await oracle.price_usd(_token(_MINT_A)) is None


async def test_pricing_nothing_makes_no_request() -> None:
    oracle, seen = _oracle({"pairs": []})

    assert await oracle.prices_usd([]) == {}
    assert seen == []
