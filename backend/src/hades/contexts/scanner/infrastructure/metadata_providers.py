"""Metadata provider adapters — on-chain (RPC) and off-chain (aggregator).

Providers are complementary and single-purpose. The RPC provider reads the mint
account for authoritative on-chain facts (decimals, supply, mint/freeze
authorities, owning program). The DexScreener provider fills human/social
metadata (name, symbol, logo, website, socials). Neither raises — a failure
returns ``None`` so the collector merges whatever succeeded.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.scanner.domain.models import TokenMetadata
from hades.shared_kernel.logging import describe, get_logger
from hades.shared_kernel.solana import RpcManager

_logger = get_logger("scanner.metadata.provider")


class RpcMetadataProvider:
    """Reads the mint account via the RPC Manager for on-chain facts."""

    def __init__(self, rpc: RpcManager) -> None:
        self._rpc = rpc

    #: The mint account is the *only* source for these; nothing off-chain has them.
    PROVIDES = frozenset(
        {
            "decimals",
            "total_supply",
            "mint_authority",
            "freeze_authority",
            "program",
        }
    )

    @property
    def name(self) -> str:
        return "rpc"

    @property
    def provides(self) -> frozenset[str]:
        return self.PROVIDES

    async def collect(self, token: TokenRef) -> TokenMetadata | None:
        try:
            result = await self._rpc.call(
                "getAccountInfo",
                [str(token.mint), {"encoding": "jsonParsed"}],
            )
        except Exception as exc:  # provider must never raise
            _logger.warning("rpc_metadata_failed", mint=str(token.mint), error=describe(exc))
            return None
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict):
            return None
        info = (((value.get("data") or {}).get("parsed") or {}).get("info")) or {}
        if not isinstance(info, dict):
            info = {}
        decimals = info.get("decimals")
        return TokenMetadata(
            mint=token.mint,
            decimals=decimals if isinstance(decimals, int) else None,
            total_supply=_supply(info.get("supply"), decimals),
            mint_authority=info.get("mintAuthority"),
            freeze_authority=info.get("freezeAuthority"),
            program=value.get("owner"),
            is_mutable=None,
            sources=("rpc",),
        )


class DexScreenerMetadataProvider:
    """Reads DexScreener's token endpoint for name/symbol/socials/logo."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.dexscreener.com/latest/dex/tokens",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    #: Human/social metadata only — DexScreener's pair payload carries no
    #: on-chain facts, so it can never fill in decimals or an authority.
    PROVIDES = frozenset(
        {
            "name",
            "symbol",
            "logo_uri",
            "website",
            "twitter",
            "telegram",
            "discord",
        }
    )

    @property
    def name(self) -> str:
        return "dexscreener"

    @property
    def provides(self) -> frozenset[str]:
        return self.PROVIDES

    async def collect(self, token: TokenRef) -> TokenMetadata | None:
        client = self._ensure_client()
        try:
            resp = await client.get(f"{self._base}/{token.mint}", timeout=self._timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # provider must never raise
            _logger.warning(
                "dexscreener_metadata_failed", mint=str(token.mint), error=describe(exc)
            )
            return None
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not pairs:
            return None
        pair = pairs[0]
        base = pair.get("baseToken") or {}
        info = pair.get("info") or {}
        socials = {s.get("type"): s.get("url") for s in info.get("socials") or []}
        websites = info.get("websites") or []
        website = websites[0].get("url") if websites else None
        return TokenMetadata(
            mint=token.mint,
            name=base.get("name"),
            symbol=base.get("symbol"),
            logo_uri=info.get("imageUrl"),
            website=website,
            twitter=socials.get("twitter"),
            telegram=socials.get("telegram"),
            discord=socials.get("discord"),
            sources=("dexscreener",),
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"accept": "application/json"})
        return self._client


def _supply(raw: Any, decimals: Any) -> Decimal | None:
    """Convert a raw integer supply string to a human-readable Decimal."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
        if isinstance(decimals, int) and decimals > 0:
            value /= Decimal(10) ** decimals
        return value
    except (InvalidOperation, ValueError):
        return None
