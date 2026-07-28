"""A provider outage must not be recorded as the tokens' data being incomplete.

A live deployment accumulated 2582 ``incomplete — missing decimals`` anomalies:
one per rescan of every token, because ``decimals`` only ever comes from the RPC
mint account and that provider was failing. Two separate defects produced that
number — the validator blamed the token for a field nobody managed to fetch, and
every repeat sighting appended a new row — and these cover both.
"""

from __future__ import annotations

import asyncio

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.scanner.application.metadata_collector import MetadataCollector
from hades.contexts.scanner.application.quality_validator import QualityValidator
from hades.contexts.scanner.domain.models import AnomalyKind, TokenMetadata
from hades.contexts.scanner.infrastructure.metadata_providers import (
    DexScreenerMetadataProvider,
    RpcMetadataProvider,
)
from hades.contexts.scanner.infrastructure.repository import InMemoryDiscoveryRepository

_MINT = "B" * 44


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT))


class _Provider:
    """A provider that answers with ``meta`` (``None`` = it failed) for ``provides``."""

    def __init__(self, name: str, provides: frozenset[str], meta: TokenMetadata | None) -> None:
        self._name = name
        self._provides = provides
        self._meta = meta

    @property
    def name(self) -> str:
        return self._name

    @property
    def provides(self) -> frozenset[str]:
        return self._provides

    async def collect(self, token: TokenRef) -> TokenMetadata | None:
        return self._meta


# -- attribution --------------------------------------------------------------


def test_a_failed_provider_marks_its_fields_unavailable_not_missing() -> None:
    rpc = _Provider("rpc", frozenset({"decimals"}), None)  # the only source of decimals, down
    dex = _Provider(
        "dexscreener",
        frozenset({"name", "symbol"}),
        TokenMetadata(mint=TokenMint(address=_MINT), name="Coin", symbol="CN"),
    )
    collected = asyncio.run(MetadataCollector([rpc, dex]).collect(_token()))

    assert collected.failures == ("rpc",)
    assert "decimals" in collected.unavailable

    outcome = QualityValidator().validate_collected(collected)
    fields = {i.field for i in outcome.issues if i.kind is AnomalyKind.INCOMPLETE}
    # This is the bug: decimals was never fetched, so its absence is our outage,
    # not a fact about the token.
    assert "decimals" not in fields


def test_a_field_genuinely_absent_is_still_flagged() -> None:
    """The fix must not silence real incompleteness — only unfetched fields."""
    rpc = _Provider(
        "rpc",
        frozenset({"decimals"}),
        TokenMetadata(mint=TokenMint(address=_MINT), decimals=9),
    )
    dex = _Provider(
        "dexscreener",
        frozenset({"name", "symbol"}),
        TokenMetadata(mint=TokenMint(address=_MINT)),  # answered, but knows no name
    )
    collected = asyncio.run(MetadataCollector([rpc, dex]).collect(_token()))

    assert collected.failures == ()
    outcome = QualityValidator().validate_collected(collected)
    fields = {i.field for i in outcome.issues if i.kind is AnomalyKind.INCOMPLETE}
    assert fields == {"name", "symbol"}
    assert all(not i.fatal for i in outcome.issues)


def test_another_provider_covering_the_field_keeps_it_reportable() -> None:
    """A field is only unavailable when *no* surviving provider covers it."""
    down = _Provider("rpc", frozenset({"decimals"}), None)
    backup = _Provider(
        "backup",
        frozenset({"decimals"}),
        TokenMetadata(mint=TokenMint(address=_MINT)),  # answered; genuinely has no decimals
    )
    collected = asyncio.run(MetadataCollector([down, backup]).collect(_token()))

    assert "decimals" not in collected.unavailable
    fields = {i.field for i in QualityValidator().validate_collected(collected).issues}
    assert "decimals" in fields


def test_the_real_providers_declare_disjoint_on_chain_and_social_fields() -> None:
    """DexScreener cannot substitute for the RPC mint account, and vice versa."""
    assert "decimals" in RpcMetadataProvider.PROVIDES
    assert "decimals" not in DexScreenerMetadataProvider.PROVIDES
    assert not RpcMetadataProvider.PROVIDES & DexScreenerMetadataProvider.PROVIDES


# -- deduplication ------------------------------------------------------------


def test_repeat_sightings_are_counted_not_appended() -> None:
    repo = InMemoryDiscoveryRepository()

    async def scan_many_times() -> None:
        for _ in range(50):
            await repo.record_anomaly(
                subject=_MINT, kind="incomplete", field="decimals", detail="missing decimals"
            )

    asyncio.run(scan_many_times())

    # 50 rescans of one persistent problem is one anomaly seen 50 times — the
    # old append-only path is what turned it into 50 rows on the dashboard.
    assert len(repo.anomalies) == 1
    assert repo.anomalies[0]["occurrences"] == 50


def test_distinct_problems_stay_distinct() -> None:
    repo = InMemoryDiscoveryRepository()

    async def record() -> None:
        await repo.record_anomaly(subject=_MINT, kind="incomplete", field="name", detail="a")
        await repo.record_anomaly(subject=_MINT, kind="incomplete", field="symbol", detail="b")
        await repo.record_anomaly(subject="C" * 44, kind="incomplete", field="name", detail="c")

    asyncio.run(record())
    assert len(repo.anomalies) == 3
