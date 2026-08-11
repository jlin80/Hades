"""DEX/aggregator source adapters: response parsing (no network)."""

from __future__ import annotations

from hades.contexts.scanner.infrastructure.sources import build_sources
from hades.contexts.scanner.infrastructure.sources.base import pick_base_mint
from hades.contexts.scanner.infrastructure.sources.dexscreener import DexScreenerSource
from hades.contexts.scanner.infrastructure.sources.jupiter import JupiterSource
from hades.contexts.scanner.infrastructure.sources.meteora import MeteoraSource
from hades.contexts.scanner.infrastructure.sources.orca import OrcaSource
from hades.contexts.scanner.infrastructure.sources.pumpfun import PumpFunSource
from hades.contexts.scanner.infrastructure.sources.raydium import RaydiumSource

_MINT = "A" * 44
_SOL = "So11111111111111111111111111111111111111112"


def test_dexscreener_parse() -> None:
    payload = {
        "pairs": [
            {
                "chainId": "solana",
                "baseToken": {"address": _MINT, "symbol": "X", "name": "Y"},
                "liquidity": {"usd": 12345},
            },
            {"chainId": "ethereum", "baseToken": {"address": "0xabc"}},
        ]
    }
    out = list(DexScreenerSource().parse(payload))
    assert len(out) == 1
    assert str(out[0].token.mint) == _MINT
    assert float(out[0].liquidity.amount) == 12345


def test_pumpfun_parse() -> None:
    payload = [
        {
            "mint": _MINT,
            "symbol": "X",
            "name": "Y",
            "usd_market_cap": 8000,
            "created_timestamp": 1_700_000_000_000,
            "creator": "B" * 44,
        }
    ]
    out = list(PumpFunSource().parse(payload))
    assert len(out) == 1
    assert out[0].deployer is not None
    assert out[0].created_at is not None


def test_raydium_parse() -> None:
    pool = {"id": "pool1", "mintA": {"address": _MINT}, "mintB": {"address": _SOL}, "tvl": 50000}
    payload = {"data": {"data": [pool]}}
    out = list(RaydiumSource().parse(payload))
    assert len(out) == 1
    assert str(out[0].token.mint) == _MINT  # base picked over SOL quote
    assert out[0].pool is not None
    assert out[0].pool.dex == "raydium"


def test_orca_parse() -> None:
    payload = {
        "whirlpools": [
            {"address": "pool", "tokenA": {"mint": _MINT}, "tokenB": {"mint": _SOL}, "tvl": 30000}
        ]
    }
    out = list(OrcaSource().parse(payload))
    assert len(out) == 1
    assert str(out[0].token.mint) == _MINT


def test_meteora_parse() -> None:
    pair = {"address": "pool", "mint_x": _MINT, "mint_y": _SOL, "liquidity": 20000, "name": "X"}
    out = list(MeteoraSource().parse([pair]))
    assert len(out) == 1
    assert float(out[0].liquidity.amount) == 20000


def test_jupiter_parse() -> None:
    payload = [{"mint": _MINT, "symbol": "X", "name": "Y", "liquidity": 15000}]
    out = list(JupiterSource().parse(payload))
    assert len(out) == 1


def test_malformed_items_are_skipped() -> None:
    assert list(DexScreenerSource().parse({"pairs": [None, {}, 5]})) == []
    assert list(PumpFunSource().parse([None, {}, "x"])) == []


def test_pick_base_mint_prefers_non_quote() -> None:
    assert pick_base_mint(_SOL, _MINT) == _MINT
    assert pick_base_mint(_MINT, _SOL) == _MINT


def test_a_pair_of_quote_assets_yields_no_candidate() -> None:
    """SOL/USDC is not a token discovery, and the old fallback said it was.

    ``return mint_a or mint_b`` answered "which of these is the token?" with a
    pricing asset whenever neither side was one. Wrapped SOL was ingested that
    way, screened, judged and bought: the live book still carries a position on
    it opened at 75.408 USD — SOL's own price.
    """
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    usdt = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    assert pick_base_mint(_SOL, usdc) is None
    assert pick_base_mint(usdc, usdt) is None
    # A quote mint with nothing opposite it is still not a token of interest.
    assert pick_base_mint(_SOL, None) is None
    assert pick_base_mint(None, None) is None


def test_factory_builds_known_sources_and_skips_unknown() -> None:
    sources = build_sources(["pumpfun", "raydium", "nope"])
    assert {s.name for s in sources} == {"pumpfun", "raydium"}
