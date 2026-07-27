"""pump.fun discovery adapter.

pump.fun is where most Solana meme coins are born, so it is the earliest
discovery signal. We read its recently-created coins feed; liquidity is
approximated from the bonding-curve USD market cap the API reports (the Market
Engine refines real liquidity later — the Scanner only needs a coarse figure to
gate obvious noise).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.contexts.scanner.infrastructure.sources.base import (
    HttpPollingSource,
    make_candidate,
)

# ``frontend-api.pump.fun`` was retired — it answers Cloudflare 530 ("origin
# unreachable"), so this source was permanently down. pump.fun moved the feed to
# ``frontend-api-v3``; the payload shape is unchanged, so only the host moves.
_DEFAULT_URL = (
    "https://frontend-api-v3.pump.fun/coins"
    "?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false"
)


class PumpFunSource(HttpPollingSource):
    """Polls pump.fun's recently-created coins feed."""

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
        return "pumpfun"

    @property
    def url(self) -> str:
        return self._url

    def parse(self, payload: Any) -> Iterable[RawTokenCandidate]:
        coins = payload if isinstance(payload, list) else payload.get("coins") or []
        for coin in coins:
            if not isinstance(coin, dict):
                continue
            mint = coin.get("mint")
            if not mint:
                continue
            liquidity = coin.get("usd_market_cap") or coin.get("market_cap") or 0.0
            candidate = make_candidate(
                mint=str(mint),
                source=self.name,
                liquidity_usd=float(liquidity),
                symbol=coin.get("symbol"),
                name=coin.get("name"),
                deployer=coin.get("creator"),
                created_at=_ts(coin.get("created_timestamp")),
            )
            if candidate is not None:
                yield candidate


def _ts(value: Any) -> datetime | None:
    """pump.fun reports creation as epoch milliseconds."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None
