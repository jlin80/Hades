"""Jupiter discovery adapter.

Jupiter is Solana's main aggregator; its "new" token feed is an early listing
signal. Jupiter does not report pool liquidity directly, so candidates surface
with the liquidity Jupiter provides (often 0); such tokens are gated out until
the Market Engine measures real liquidity, unless whitelisted. The adapter still
belongs here as an extensible discovery source.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx

from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.contexts.scanner.infrastructure.sources.base import (
    HttpPollingSource,
    make_candidate,
)

# NOTE (verified 2026-07-26 from a live host): this endpoint answers 404 — the
# provider moved or retired it. It is left as-is rather than guessed at, because a
# wrong URL is worse than a known-bad one: it would fail in a way that looks like
# a transient outage. Point it somewhere real with
# ``SCANNER_SOURCE_URLS={"jupiter": "…"}`` (the parser below still expects this
# provider's payload shape), or leave this source out of ``SCANNER_SOURCES``.
_DEFAULT_URL = "https://api.jup.ag/tokens/v1/new"


class JupiterSource(HttpPollingSource):
    """Polls Jupiter's newly-listed tokens feed."""

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
        return "jupiter"

    @property
    def url(self) -> str:
        return self._url

    def parse(self, payload: Any) -> Iterable[RawTokenCandidate]:
        tokens = payload if isinstance(payload, list) else payload.get("tokens") or []
        for token in tokens:
            if not isinstance(token, dict):
                continue
            mint = token.get("mint") or token.get("address")
            if not mint:
                continue
            liquidity = float(token.get("liquidity") or token.get("liquidity_usd") or 0.0)
            candidate = make_candidate(
                mint=str(mint),
                source=self.name,
                liquidity_usd=liquidity,
                symbol=token.get("symbol"),
                name=token.get("name"),
                created_at=_iso(token.get("created_at")),
            )
            if candidate is not None:
                yield candidate


def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
