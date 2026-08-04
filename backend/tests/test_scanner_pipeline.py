"""Quality Validator, Metadata Collector and the Acquisition Pipeline."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.scanner.application.metadata_collector import MetadataCollector
from hades.contexts.scanner.application.metrics import ScannerMetrics
from hades.contexts.scanner.application.pipeline import AcquisitionPipeline
from hades.contexts.scanner.application.quality_validator import QualityValidator
from hades.contexts.scanner.domain.events import TokenDiscovered, TokenMetadataCollected
from hades.contexts.scanner.domain.models import TokenMetadata
from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.contexts.scanner.infrastructure.repository import InMemoryDiscoveryRepository
from hades.shared_kernel.config.settings import PipelineSettings
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry

_MINT = "A" * 44


def _candidate(liquidity: float = 9000.0) -> RawTokenCandidate:
    return RawTokenCandidate(
        token=TokenRef(mint=TokenMint(address=_MINT)),
        source="test",
        liquidity=Money(amount=liquidity),
    )


class FakeProvider:
    def __init__(self, name: str, meta: TokenMetadata | None) -> None:
        self._name = name
        self._meta = meta

    @property
    def name(self) -> str:
        return self._name

    async def collect(self, token: TokenRef) -> TokenMetadata | None:
        return self._meta


# -- Quality Validator --------------------------------------------------------


def test_validator_rejects_negative_liquidity() -> None:
    outcome = QualityValidator().validate_candidate(_candidate(liquidity=-5.0))
    assert not outcome.accepted
    assert outcome.fatal_issues


def test_validator_flags_duplicate() -> None:
    outcome = QualityValidator().validate_candidate(_candidate(), is_duplicate=True)
    assert not outcome.accepted


def test_validator_rejects_future_timestamp() -> None:
    meta = TokenMetadata(
        mint=TokenMint(address=_MINT), created_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert not QualityValidator().validate_metadata(meta).accepted


def test_validator_rejects_bad_decimals() -> None:
    meta = TokenMetadata(mint=TokenMint(address=_MINT), decimals=99)
    assert not QualityValidator().validate_metadata(meta).accepted


def test_validator_flags_incomplete_but_accepts() -> None:
    meta = TokenMetadata(mint=TokenMint(address=_MINT), decimals=6)
    outcome = QualityValidator().validate_metadata(meta)
    assert outcome.accepted  # missing name/symbol is non-fatal
    assert any(i.field == "name" for i in outcome.issues)


def test_validator_rejects_non_finite_feature() -> None:
    outcome = QualityValidator().validate_features({"x": float("nan"), "y": 1.0})
    assert not outcome.accepted


# -- Metadata Collector -------------------------------------------------------


async def test_collector_merges_providers() -> None:
    mint = TokenMint(address=_MINT)
    on_chain = TokenMetadata(mint=mint, decimals=6, mint_authority=None, sources=("rpc",))
    social = TokenMetadata(mint=mint, name="Doge", symbol="DOGE", sources=("dexscreener",))
    collector = MetadataCollector(
        [FakeProvider("rpc", on_chain), FakeProvider("dex", social), FakeProvider("dead", None)]
    )
    result = await collector.collect(TokenRef(mint=mint))
    assert result.metadata.decimals == 6
    assert result.metadata.name == "Doge"
    assert result.metadata.symbol == "DOGE"
    assert "dead" in result.failures
    assert set(result.providers) == {"rpc", "dex"}


# -- Acquisition Pipeline -----------------------------------------------------


def _pipeline(
    repo: InMemoryDiscoveryRepository,
    bus: InMemoryEventBus,
    *,
    workers: int = 2,
    queue_max: int = 10,
) -> AcquisitionPipeline:
    meta = TokenMetadata(mint=TokenMint(address=_MINT), decimals=6, name="X", symbol="X")
    collector = MetadataCollector([FakeProvider("rpc", meta)])
    settings = PipelineSettings(workers=workers, queue_max_size=queue_max)
    return AcquisitionPipeline(
        collector,
        QualityValidator(),
        repo,
        settings=settings,
        event_bus=bus,
        metrics=ScannerMetrics(MetricsRegistry()),
    )


async def test_pipeline_processes_and_emits() -> None:
    repo = InMemoryDiscoveryRepository()
    bus = InMemoryEventBus()
    discovered: list[TokenDiscovered] = []
    metadata_events: list[TokenMetadataCollected] = []

    async def on_discovered(event: object) -> None:
        assert isinstance(event, TokenDiscovered)
        discovered.append(event)

    async def on_metadata(event: object) -> None:
        assert isinstance(event, TokenMetadataCollected)
        metadata_events.append(event)

    bus.subscribe(TokenDiscovered.__name__, on_discovered)
    bus.subscribe(TokenMetadataCollected.__name__, on_metadata)

    pipeline = _pipeline(repo, bus)
    await pipeline.start()
    await pipeline.enqueue(_candidate())
    # Wait for the *last* thing the unit of work does, not the first. The worker
    # writes the repository and only then publishes TokenDiscovered followed by
    # TokenMetadataCollected, so polling on `repo.metadata` and stopping the
    # pipeline the moment it appeared cut the worker off mid-publish: on a
    # contended machine this failed with `discovered == 1` and
    # `metadata_events == 0`, which reads like a broken pipeline and was a broken
    # test.
    for _ in range(500):
        if metadata_events:
            break
        await asyncio.sleep(0.01)
    await pipeline.stop()

    assert _MINT in repo.tokens
    assert _MINT in repo.metadata
    assert repo.metadata[_MINT].decimals == 6
    assert len(discovered) == 1
    assert len(metadata_events) == 1
    # The discovered event correlates the metadata event (same correlation id).
    assert discovered[0].correlation_id == metadata_events[0].correlation_id


async def test_pipeline_backpressure_drops_oldest() -> None:
    repo = InMemoryDiscoveryRepository()
    bus = InMemoryEventBus()
    pipeline = _pipeline(repo, bus, queue_max=2)  # not started: no workers drain it
    await pipeline.enqueue(_candidate())
    await pipeline.enqueue(_candidate())
    await pipeline.enqueue(_candidate())
    assert pipeline.queue_depth() == 2  # bounded — oldest was dropped
