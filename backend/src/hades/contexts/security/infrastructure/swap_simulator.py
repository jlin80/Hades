"""Swap simulator — honeypot detection via a real buy+sell route quote.

Asks a DEX aggregator (Jupiter) for a route to *buy* the token with SOL and,
crucially, a route to *sell* it back. A token you can buy but cannot route a sale
for is the definition of a honeypot. Price impact on each leg approximates the
effective buy/sell tax. When the aggregator cannot be reached the probe reports
``sellable=None`` (doubt) — never a false all-clear.

Two no-network implementations back the tests and the "clustering/simulation
disabled" configuration.
"""

from __future__ import annotations

from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.security.domain.models import HoneypotProbe
from hades.shared_kernel.logging import describe, get_logger

_logger = get_logger("security.honeypot")

# Wrapped SOL — the quote currency for both legs.
_WSOL = "So11111111111111111111111111111111111111112"
_SOL_PRICE_USD = 150.0  # coarse fallback for sizing the probe (not a price feed)


#: Jupiter's current public quote route.
#:
#: The previous default, ``https://quote-api.jup.ag/v6/quote``, was retired by
#: Jupiter and its hostname no longer resolves — ``quote-api.jup.ag`` returns
#: EAI_NODATA while ``lite-api.jup.ag`` and ``api.jup.ag`` answer normally. The
#: failure was quiet in the worst way: the probe returns "doubt, not crash", the
#: Security Engine reads an unavailable buy route as an unproven token, and a
#: deployment sat for hours rejecting *every* candidate on a dead URL while each
#: component reported itself healthy.
DEFAULT_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"


class JupiterSwapSimulator:
    """Probes sellability with Jupiter quote routes (buy then sell)."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_QUOTE_URL,
        timeout_seconds: float = 8.0,
        slippage_bps: int = 500,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url
        self._timeout = timeout_seconds
        self._slippage = slippage_bps
        self._client = client
        self._owns_client = client is None

    async def probe(self, token: TokenRef, *, amount_usd: float) -> HoneypotProbe:
        client = self._ensure_client()
        mint = str(token.mint)
        lamports = max(1, int(amount_usd / _SOL_PRICE_USD * 1_000_000_000))
        buy = await self._quote(client, _WSOL, mint, lamports)
        if buy is None:
            return HoneypotProbe(sellable=None, buyable=None, note="buy route unavailable")
        out_amount = self._out_amount(buy)
        if out_amount <= 0:
            return HoneypotProbe(sellable=None, buyable=True, note="empty buy route")
        sell = await self._quote(client, mint, _WSOL, out_amount)
        if sell is None:
            return HoneypotProbe(sellable=False, buyable=True, note="no sell route — honeypot")
        buy_tax = self._impact_bps(buy)
        sell_tax = self._impact_bps(sell)
        return HoneypotProbe(
            sellable=True,
            buyable=True,
            buy_tax_bps=buy_tax,
            sell_tax_bps=sell_tax,
            note="buy+sell routes ok",
        )

    async def _quote(
        self, client: httpx.AsyncClient, input_mint: str, output_mint: str, amount: int
    ) -> dict[str, Any] | None:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(self._slippage),
        }
        try:
            resp = await client.get(self._base, params=params, timeout=self._timeout)
            if resp.status_code != 200:
                return None
            body = resp.json()
        except Exception as exc:  # never raise — doubt, not crash
            _logger.warning("jupiter_quote_failed", error=describe(exc))
            return None
        return body if isinstance(body, dict) and body.get("outAmount") else None

    @staticmethod
    def _out_amount(quote: dict[str, Any]) -> int:
        try:
            return int(quote.get("outAmount", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _impact_bps(quote: dict[str, Any]) -> int | None:
        raw = quote.get("priceImpactPct")
        if raw is None:
            return None
        try:
            return int(abs(float(raw)) * 10_000)
        except (TypeError, ValueError):
            return None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"accept": "application/json"})
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class NullSwapSimulator:
    """Disabled simulator — always reports doubt (used when probing is off)."""

    async def probe(self, token: TokenRef, *, amount_usd: float) -> HoneypotProbe:
        return HoneypotProbe(sellable=None, note="honeypot simulation disabled")


class StubSwapSimulator:
    """Deterministic simulator for tests — returns a pre-set probe per mint."""

    def __init__(self, probes: dict[str, HoneypotProbe] | None = None) -> None:
        self._probes = probes or {}

    async def probe(self, token: TokenRef, *, amount_usd: float) -> HoneypotProbe:
        return self._probes.get(str(token.mint), HoneypotProbe(sellable=True, note="stub ok"))
