"""DexScreener price oracle — the real price behind paper fills and marking.

Two callers need the current USD price of a token, and both were previously
starved of one: the :class:`~hades.contexts.execution.application.paper_executor.PaperExecutor`
(which fell back to a unit price, making every entry price 1.0) and the Position
Monitor (which cannot evaluate a take-profit without a mark).

This adapter satisfies the execution :class:`PriceOracle` port and adds a batch
lookup, because marking a book of positions one HTTP request at a time is what
turns a 5-second monitor interval into a rate-limit problem. Quotes are cached
for a short TTL: positions are polled far more often than prices meaningfully
move, and the upstream endpoint is unauthenticated.

Missing data is never an error. An unknown mint yields ``None``, and every
caller already degrades gracefully — a position simply is not marked this cycle
rather than being marked with an invented number.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import TokenRef
from hades.shared_kernel.logging import describe, get_logger

_logger = get_logger("market.price_oracle")

_DEFAULT_URL = "https://api.dexscreener.com/latest/dex/tokens/"


class DexScreenerPriceOracle:
    """Reads USD prices from DexScreener's batch token endpoint."""

    def __init__(
        self,
        *,
        url: str = _DEFAULT_URL,
        cache_ttl_seconds: float = 15.0,
        timeout_seconds: float = 8.0,
        batch_size: int = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/") + "/"
        self._ttl = max(0.0, cache_ttl_seconds)
        self._timeout = timeout_seconds
        self._batch = max(1, batch_size)
        self._client = client
        self._owns_client = client is None
        # mint -> (price, cached_at). Bounded by the number of tokens the
        # platform actually holds or is pricing, which the risk limits cap.
        self._cache: dict[str, tuple[Decimal, float]] = {}
        self._lock = asyncio.Lock()

    # -- the execution PriceOracle port ---------------------------------------

    async def price_usd(self, token: TokenRef) -> Decimal | None:
        prices = await self.prices_usd([token])
        return prices.get(str(token.mint))

    # -- batch lookup (what the Position Monitor uses) ------------------------

    async def prices_usd(self, tokens: Sequence[TokenRef]) -> dict[str, Decimal]:
        """Return ``mint -> price`` for every token a price could be found for."""
        mints = list(dict.fromkeys(str(t.mint) for t in tokens))
        if not mints:
            return {}

        now = time.monotonic()
        resolved: dict[str, Decimal] = {}
        stale: list[str] = []
        for mint in mints:
            cached = self._cache.get(mint)
            if cached is not None and (now - cached[1]) < self._ttl:
                resolved[mint] = cached[0]
            else:
                stale.append(mint)
        if not stale:
            return resolved

        # One in-flight refresh at a time: a burst of monitor cycles must not
        # multiply into concurrent identical requests against a rate-limited host.
        async with self._lock:
            fetched = await self._fetch_many(stale)
        cached_at = time.monotonic()
        for mint, price in fetched.items():
            self._cache[mint] = (price, cached_at)
        resolved.update(fetched)
        return resolved

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- internals ------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"accept": "application/json"})
        return self._client

    async def _fetch_many(self, mints: list[str]) -> dict[str, Decimal]:
        client = self._ensure_client()
        out: dict[str, Decimal] = {}
        for start in range(0, len(mints), self._batch):
            chunk = mints[start : start + self._batch]
            try:
                payload = await self._fetch(client, chunk)
            except Exception as exc:  # a dead feed must not break the caller
                _logger.warning("price_fetch_failed", mints=len(chunk), error=describe(exc))
                continue
            out.update(_parse_prices(payload))
        return out

    async def _fetch(self, client: httpx.AsyncClient, mints: list[str]) -> Any:
        resp = await client.get(self._url + ",".join(mints), timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()


def _parse_prices(payload: Any) -> dict[str, Decimal]:
    """Pick the deepest pair per mint — the thin ones quote unreliable prices."""
    if not isinstance(payload, dict):
        return {}
    pairs = payload.get("pairs")
    if not isinstance(pairs, list):
        return {}

    best: dict[str, tuple[Decimal, float]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        base = pair.get("baseToken")
        if not isinstance(base, dict):
            continue
        mint = base.get("address")
        price = _as_decimal(pair.get("priceUsd"))
        if not isinstance(mint, str) or price is None or price <= 0:
            continue
        liquidity = pair.get("liquidity")
        depth = 0.0
        if isinstance(liquidity, dict):
            usd = liquidity.get("usd")
            if isinstance(usd, int | float):
                depth = float(usd)
        current = best.get(mint)
        if current is None or depth > current[1]:
            best[mint] = (price, depth)
    return {mint: price for mint, (price, _) in best.items()}


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str) and value:
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None
