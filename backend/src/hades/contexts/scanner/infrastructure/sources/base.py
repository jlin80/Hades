"""Base for HTTP-polling discovery sources.

Most DEX/aggregator "new token" feeds are exposed as a JSON HTTP endpoint. This
base owns the polling loop, the shared ``httpx`` client, timeouts and JSON
decoding; each concrete adapter implements only two things: the URL to poll and
how to turn that DEX's response into :class:`RawTokenCandidate` objects. No two
adapters share parsing logic.

A subclass that instead subscribes to a WebSocket can implement :meth:`stream`
directly — the port only requires ``name`` + ``stream``.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import (
    Money,
    TokenMint,
    TokenRef,
    WalletAddress,
)
from hades.contexts.scanner.domain.models import RawPool
from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.shared_kernel.logging import get_logger

_logger = get_logger("scanner.source")

# Canonical quote mints on Solana. In a pool of (base, quote) the *base* is the
# token we care about; the quote is the pricing asset.
QUOTE_MINTS = frozenset(
    {
        "So11111111111111111111111111111111111111112",  # wrapped SOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }
)


def pick_base_mint(mint_a: str | None, mint_b: str | None) -> str | None:
    """Return the non-quote mint of a pair, or ``None`` if the pair has none.

    A pool of two pricing assets — SOL/USDC, USDC/USDT — contains no token of
    interest, and the old fallback (``return mint_a or mint_b``) answered it with
    a quote mint anyway. That is how Wrapped SOL entered the platform as a
    discovered candidate: it was screened, scored, judged by the committee and
    ultimately bought, and the live book still holds a position on
    ``So111...112`` opened at an entry price of 75.408 — SOL's own price, not a
    meme coin's.

    The honest answer to "which of these two is the token?" is sometimes
    *neither*, and the caller already handles ``None`` by skipping the pool.
    """
    if mint_a and mint_a not in QUOTE_MINTS:
        return mint_a
    if mint_b and mint_b not in QUOTE_MINTS:
        return mint_b
    return None


def make_candidate(
    *,
    mint: str,
    source: str,
    liquidity_usd: float,
    symbol: str | None = None,
    name: str | None = None,
    signature: str | None = None,
    deployer: str | None = None,
    created_at: datetime | None = None,
    pool: RawPool | None = None,
) -> RawTokenCandidate | None:
    """Build a validated candidate, or ``None`` if the mint is not a valid pubkey.

    Shared by every adapter so mint validation lives in one place; it carries no
    DEX-specific logic.
    """
    try:
        token = TokenRef(mint=TokenMint(address=mint), symbol=symbol, name=name)
        wallet = WalletAddress(address=deployer) if deployer else None
    except ValueError:
        return None
    return RawTokenCandidate(
        token=token,
        source=source,
        liquidity=Money(amount=liquidity_usd),
        signature=signature,
        deployer=wallet,
        created_at=created_at,
        pool=pool,
    )


class HttpPollingSource(abc.ABC):
    """A :class:`TokenSource` that polls a JSON HTTP endpoint on an interval."""

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 5.0,
        timeout_seconds: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable identifier used in config and events."""

    @property
    @abc.abstractmethod
    def url(self) -> str:
        """The endpoint to poll for recent tokens/pools."""

    @abc.abstractmethod
    def parse(self, payload: Any) -> Iterable[RawTokenCandidate]:
        """Turn this DEX's JSON response into candidates. Adapter-specific."""

    async def stream(self) -> AsyncIterator[RawTokenCandidate]:
        client = self._ensure_client()
        while True:
            payload = await self._fetch(client)
            for candidate in self._safe_parse(payload):
                yield candidate
            await asyncio.sleep(self._interval)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- internals ------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"accept": "application/json"})
        return self._client

    async def _fetch(self, client: httpx.AsyncClient) -> Any:
        resp = await client.get(self.url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _safe_parse(self, payload: Any) -> list[RawTokenCandidate]:
        try:
            return list(self.parse(payload))
        except Exception as exc:  # a malformed item must not kill the stream
            _logger.warning("source_parse_failed", source=self.name, error=str(exc))
            return []
