"""Metadata Collector — gather absolutely everything about a token.

Runs every configured :class:`MetadataProvider` concurrently and merges their
partial results into one :class:`TokenMetadata`. Providers are complementary: an
on-chain provider fills mint/supply/authorities, an aggregator fills socials. A
provider that fails or times out is skipped (never raises) so collection is
best-effort and always returns whatever was learned.

The collector forms no opinion about the token — it only accumulates knowledge,
storing every field even if nothing downstream uses it yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.scanner.domain.models import TokenMetadata
from hades.contexts.scanner.domain.ports import MetadataProvider
from hades.shared_kernel.logging import get_logger

_logger = get_logger("scanner.metadata")

# Scalar fields merged "first non-null wins" (providers ordered by trust).
_SCALAR_FIELDS = (
    "name",
    "symbol",
    "decimals",
    "total_supply",
    "logo_uri",
    "website",
    "twitter",
    "telegram",
    "discord",
    "description",
    "program",
    "update_authority",
    "mint_authority",
    "freeze_authority",
    "is_mutable",
    "created_at",
)


@dataclass
class CollectedMetadata:
    """Merged metadata plus provenance (who answered, who failed, what we never asked)."""

    metadata: TokenMetadata
    providers: tuple[str, ...] = ()
    failures: tuple[str, ...] = field(default_factory=tuple)
    #: Fields whose every source failed this round. A ``None`` here means "not
    #: collected", not "absent from the token" — the Quality Validator must not
    #: report these as incomplete data, because we never successfully looked.
    unavailable: frozenset[str] = field(default_factory=frozenset)


class MetadataCollector:
    """Composes metadata providers into one complete record per token."""

    def __init__(self, providers: list[MetadataProvider], *, timeout_seconds: float = 12.0) -> None:
        self._providers = providers
        self._timeout = timeout_seconds

    async def collect(self, token: TokenRef) -> CollectedMetadata:
        results = await asyncio.gather(*(self._safe(p, token) for p in self._providers))
        merged = _base_metadata(token)
        answered: list[str] = []
        failed: list[str] = []
        covered: set[str] = set()
        lost: set[str] = set()
        for provider, partial in zip(self._providers, results, strict=True):
            if partial is None:
                failed.append(provider.name)
                lost |= _provides(provider)
                continue
            answered.append(provider.name)
            covered |= _provides(provider)
            merged = _merge(merged, partial, provider.name)
        return CollectedMetadata(
            metadata=merged,
            providers=tuple(answered),
            failures=tuple(failed),
            # A field is only truly unavailable when *no* surviving provider
            # covers it; if another source answered, its silence is real data.
            unavailable=frozenset(lost - covered),
        )

    async def _safe(self, provider: MetadataProvider, token: TokenRef) -> TokenMetadata | None:
        try:
            return await asyncio.wait_for(provider.collect(token), timeout=self._timeout)
        except Exception as exc:  # TimeoutError included — a provider must not break collection
            _logger.warning("metadata_provider_failed", provider=provider.name, error=str(exc))
            return None


def _provides(provider: MetadataProvider) -> set[str]:
    """Fields ``provider`` is a source for; a provider that says nothing covers all."""
    declared = getattr(provider, "provides", None)
    return set(declared) if declared else set(_SCALAR_FIELDS)


def _base_metadata(token: TokenRef) -> TokenMetadata:
    return TokenMetadata(mint=token.mint, name=token.name, symbol=token.symbol)


def _merge(base: TokenMetadata, incoming: TokenMetadata, source: str) -> TokenMetadata:
    """First non-null wins for scalars; sources + extra accumulate."""
    updates: dict[str, object] = {}
    for name in _SCALAR_FIELDS:
        if getattr(base, name) is None:
            value = getattr(incoming, name)
            if value is not None:
                updates[name] = value
    sources = tuple(dict.fromkeys((*base.sources, source, *incoming.sources)))
    extra = {**incoming.extra, **base.extra}
    return base.model_copy(update={**updates, "sources": sources, "extra": extra})
