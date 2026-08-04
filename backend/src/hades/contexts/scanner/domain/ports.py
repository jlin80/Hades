"""Ports (contracts) the Scanner defines.

The Scanner depends on abstractions only. Each real source (pump.fun, Raydium,
DexScreener, Orca, Meteora, Jupiter, a WebSocket log subscription) is a
:class:`TokenSource` adapter; metadata providers implement
:class:`MetadataProvider`; persistence implements :class:`DiscoveryRepository`.
Adding a source or provider therefore never touches the Scanner's core logic —
Open/Closed in practice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from hades.contexts.common.domain.value_objects import Money, TokenRef, WalletAddress
from hades.contexts.scanner.domain.models import RawPool, TokenMetadata
from hades.shared_kernel.domain.base import ValueObject


class RawTokenCandidate(ValueObject):
    """A candidate as seen by a source, before gating/dedup by the Scanner."""

    token: TokenRef
    source: str
    liquidity: Money
    signature: str | None = None
    # Optional enrichments a source may already know — used by coarse gating.
    deployer: WalletAddress | None = None
    created_at: datetime | None = None
    pool: RawPool | None = None


@runtime_checkable
class TokenSource(Protocol):
    """A pollable / streamable source of new token candidates."""

    @property
    def name(self) -> str:
        """Stable identifier used in config (``SCANNER_SOURCES``) and events."""
        ...

    def stream(self) -> AsyncIterator[RawTokenCandidate]:
        """Yield candidates as they appear. Implementations may poll or subscribe."""
        ...


@runtime_checkable
class SeenTokenRegistry(Protocol):
    """Dedup store — has this mint already entered the pipeline recently?"""

    async def is_new(self, token: TokenRef) -> bool: ...

    async def mark_seen(self, token: TokenRef) -> None: ...


@runtime_checkable
class MetadataProvider(Protocol):
    """Contributes part of a token's metadata (composed by the collector).

    Each provider is single-purpose (on-chain mint account, a metadata program,
    an aggregator's socials). It returns whatever it could learn as a partial
    :class:`TokenMetadata`; the collector merges them. It must never raise —
    return ``None`` on failure so one bad provider cannot block collection.

    ``provides`` names the fields this provider is a source for. It exists so the
    collector can tell "the token has no decimals" apart from "the only provider
    that could have told us about decimals was down" — the two look identical in
    the merged result but mean completely different things to the Quality
    Validator. A provider that omits it is treated as a source for every field.
    """

    @property
    def name(self) -> str: ...

    @property
    def provides(self) -> frozenset[str]: ...

    async def collect(self, token: TokenRef) -> TokenMetadata | None: ...


@runtime_checkable
class DiscoveryRepository(Protocol):
    """Persists what the Scanner learns (tokens, metadata, pools, anomalies).

    The Scanner stores *facts*, never judgements. Implementations write to the
    operational database; an in-memory adapter backs the tests.
    """

    async def upsert_token(self, candidate: RawTokenCandidate) -> None: ...

    async def save_metadata(self, metadata: TokenMetadata) -> None: ...

    async def save_pool(self, pool: RawPool) -> None: ...

    async def record_anomaly(self, *, subject: str, kind: str, field: str, detail: str) -> None: ...
