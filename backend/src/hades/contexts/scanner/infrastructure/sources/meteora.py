"""Meteora discovery adapter.

Meteora's DLMM pairs are a common venue for freshly launched liquidity. We read
its pair list and surface the non-quote mint with the pair's reported liquidity.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import Money, TokenMint
from hades.contexts.scanner.domain.models import RawPool
from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.contexts.scanner.infrastructure.sources.base import (
    HttpPollingSource,
    make_candidate,
    pick_base_mint,
)

# NOTE (verified 2026-07-26 from a live host): this endpoint answers 404 — the
# provider moved or retired it. It is left as-is rather than guessed at, because a
# wrong URL is worse than a known-bad one: it would fail in a way that looks like
# a transient outage. Point it somewhere real with
# ``SCANNER_SOURCE_URLS={"meteora": "…"}`` (the parser below still expects this
# provider's payload shape), or leave this source out of ``SCANNER_SOURCES``.
_DEFAULT_URL = "https://dlmm-api.meteora.ag/pair/all"


class MeteoraSource(HttpPollingSource):
    """Polls Meteora's DLMM pair list."""

    def __init__(
        self,
        *,
        url: str = _DEFAULT_URL,
        poll_interval_seconds: float = 5.0,
        timeout_seconds: float = 12.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            client=client,
        )
        self._url = url

    @property
    def name(self) -> str:
        return "meteora"

    @property
    def url(self) -> str:
        return self._url

    def parse(self, payload: Any) -> Iterable[RawTokenCandidate]:
        pairs = payload if isinstance(payload, list) else payload.get("pairs") or []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            mint_x = pair.get("mint_x")
            mint_y = pair.get("mint_y")
            base = pick_base_mint(mint_x, mint_y)
            if not base:
                continue
            liquidity = float(pair.get("liquidity") or 0.0)
            quote = mint_y if base == mint_x else mint_x
            raw_pool = _pool(str(pair.get("address")), base, quote, liquidity)
            candidate = make_candidate(
                mint=str(base),
                source=self.name,
                liquidity_usd=liquidity,
                name=pair.get("name"),
                pool=raw_pool,
            )
            if candidate is not None:
                yield candidate


def _pool(address: str, base: str, quote: str | None, liquidity: float) -> RawPool | None:
    if not address:
        return None
    try:
        return RawPool(
            address=address,
            dex="meteora",
            base_mint=TokenMint(address=base),
            quote_mint=TokenMint(address=quote) if quote else None,
            liquidity=Money(amount=liquidity),
        )
    except ValueError:
        return None
