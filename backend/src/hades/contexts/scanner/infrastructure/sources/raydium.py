"""Raydium discovery adapter.

Raydium is a primary Solana AMM; new pools there signal graduated / launched
tokens with real liquidity. We read its pool list and surface the non-quote mint
of each pool with the pool's TVL as coarse liquidity.
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

# ``poolSortField=created`` is not a value Raydium v3 accepts (it takes
# ``default``, ``liquidity``, ``volume24h``, ``fee24h``, ``apr24h``, …). The API
# answers 500 rather than 400, so this source failed on every single poll.
#
# The honest cost of the fix: v3 exposes no "newest pools" ordering, so this
# source no longer surfaces brand-new pools preferentially — it returns Raydium's
# default ranking, and new listings are found only once they rank into the first
# page. Discovery of fresh launches leans on the other sources; the dedup registry
# means re-seeing the same top pools is cheap, not harmful.
_DEFAULT_URL = "https://api-v3.raydium.io/pools/info/list?poolType=all&poolSortField=default&sortType=desc&pageSize=100&page=1"


class RaydiumSource(HttpPollingSource):
    """Polls Raydium's pool list for recently created pools."""

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
        return "raydium"

    @property
    def url(self) -> str:
        return self._url

    def parse(self, payload: Any) -> Iterable[RawTokenCandidate]:
        data = payload.get("data") if isinstance(payload, dict) else None
        pools = (data or {}).get("data") if isinstance(data, dict) else data
        for pool in pools or []:
            if not isinstance(pool, dict):
                continue
            mint_a = (pool.get("mintA") or {}).get("address")
            mint_b = (pool.get("mintB") or {}).get("address")
            base = pick_base_mint(mint_a, mint_b)
            if not base:
                continue
            tvl = float(pool.get("tvl") or 0.0)
            raw_pool = _pool(pool, base, mint_a, mint_b, tvl)
            candidate = make_candidate(
                mint=str(base),
                source=self.name,
                liquidity_usd=tvl,
                pool=raw_pool,
            )
            if candidate is not None:
                yield candidate


def _pool(
    pool: dict[str, Any], base: str, mint_a: str | None, mint_b: str | None, tvl: float
) -> RawPool | None:
    address = pool.get("id")
    if not address:
        return None
    quote = mint_b if base == mint_a else mint_a
    try:
        return RawPool(
            address=str(address),
            dex="raydium",
            base_mint=TokenMint(address=str(base)),
            quote_mint=TokenMint(address=str(quote)) if quote else None,
            liquidity=Money(amount=tvl),
        )
    except ValueError:
        return None
