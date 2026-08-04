"""The ingest clocks measure the legs that were previously invisible.

Two figures, and the rules that keep them honest:

* **Discovery lag** (``created_at`` → discovered) is the part of the budget no
  downstream work can recover. A source that reports no ``created_at`` must be
  skipped, not recorded as zero — a missing measurement is not an instant one.
* **discovered → features-ready** spans two contexts, so it closes in the
  features subscriber against the source event's ``occurred_at``. A token that
  failed to compute must not be recorded, or the pipeline would look faster the
  more often it broke.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.features.application.subscriber import FeatureComputationHandler
from hades.contexts.scanner.application.discovery_engine import DiscoveryEngine
from hades.contexts.scanner.application.metrics import ScannerMetrics
from hades.contexts.scanner.domain.events import TokenDiscovered
from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.shared_kernel.config.settings import ScannerSettings
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry

_MINT = "So11111111111111111111111111111111111111112"


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


def _candidate(*, created_at: datetime | None) -> RawTokenCandidate:
    return RawTokenCandidate(
        token=_token(),
        source="pumpfun",
        liquidity=Money(amount=50_000),
        created_at=created_at,
    )


class _Registry:
    """Every token is new; nothing is ever a duplicate."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def is_new(self, token: TokenRef) -> bool:
        return True

    async def mark_seen(self, token: TokenRef) -> None:
        self.seen.append(str(token.mint))


def _engine(metrics: ScannerMetrics) -> tuple[DiscoveryEngine, list[RawTokenCandidate]]:
    sunk: list[RawTokenCandidate] = []

    async def sink(candidate: RawTokenCandidate) -> None:
        sunk.append(candidate)

    engine = DiscoveryEngine(
        [],
        _Registry(),
        sink,
        settings=ScannerSettings(),
        event_bus=InMemoryEventBus(),
        metrics=metrics,
    )
    return engine, sunk


def _lag_sample_count(registry: MetricsRegistry, source: str) -> float:
    """Read the histogram's observation count straight off the client."""
    metric = registry.registry.get_sample_value(
        "hades_scanner_discovery_lag_seconds_count", {"source": source}
    )
    return metric or 0.0


def _lag_sum(registry: MetricsRegistry, source: str) -> float:
    metric = registry.registry.get_sample_value(
        "hades_scanner_discovery_lag_seconds_sum", {"source": source}
    )
    return metric or 0.0


# -- discovery lag ------------------------------------------------------------


async def test_discovery_lag_is_recorded_for_a_dated_candidate() -> None:
    registry = MetricsRegistry()
    metrics = ScannerMetrics(registry)
    engine, sunk = _engine(metrics)

    age = timedelta(seconds=30)
    await engine._handle(_candidate(created_at=datetime.now(UTC) - age), "pumpfun")

    assert len(sunk) == 1
    assert _lag_sample_count(registry, "pumpfun") == 1.0
    # ~30s, allowing for the time the test itself takes.
    assert 29.0 <= _lag_sum(registry, "pumpfun") <= 45.0


async def test_a_candidate_without_created_at_is_skipped_not_recorded_as_zero() -> None:
    registry = MetricsRegistry()
    metrics = ScannerMetrics(registry)
    engine, sunk = _engine(metrics)

    await engine._handle(_candidate(created_at=None), "pumpfun")

    assert len(sunk) == 1, "the candidate still flows — only the measurement is skipped"
    assert _lag_sample_count(registry, "pumpfun") == 0.0


async def test_a_naive_timestamp_is_treated_as_utc_not_rejected() -> None:
    registry = MetricsRegistry()
    metrics = ScannerMetrics(registry)
    engine, _ = _engine(metrics)

    naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
    await engine._handle(_candidate(created_at=naive), "raydium")

    assert _lag_sample_count(registry, "raydium") == 1.0


async def test_a_source_clock_ahead_of_ours_is_discarded_not_clamped() -> None:
    """Clamping a negative lag to zero would quietly pull the median down."""
    registry = MetricsRegistry()
    metrics = ScannerMetrics(registry)
    engine, sunk = _engine(metrics)

    future = datetime.now(UTC) + timedelta(minutes=5)
    await engine._handle(_candidate(created_at=future), "dexscreener")

    assert len(sunk) == 1
    assert _lag_sample_count(registry, "dexscreener") == 0.0


# -- discovered → features ready ----------------------------------------------


class _Assembler:
    def __init__(self, *, fails: bool = False) -> None:
        self._fails = fails

    async def build(self, token: TokenRef, *, hint_liquidity: float = 0.0) -> object:
        if self._fails:
            raise RuntimeError("assembler unavailable")
        return object()


class _Engine:
    def __init__(self) -> None:
        self.calls = 0

    async def compute(self, inputs: object) -> object:
        self.calls += 1
        await asyncio.sleep(0.05)
        return object()


def _discovered(*, seconds_ago: float = 0.0) -> TokenDiscovered:
    event = TokenDiscovered(
        aggregate_id=new_id(),
        token=_token(),
        source="pumpfun",
        initial_liquidity=Money(amount=50_000),
    )
    if seconds_ago:
        return event.model_copy(
            update={"occurred_at": datetime.now(UTC) - timedelta(seconds=seconds_ago)}
        )
    return event


async def test_end_to_end_latency_is_recorded_once_features_are_ready() -> None:
    observed: list[float] = []
    handler = FeatureComputationHandler(
        _Engine(),  # type: ignore[arg-type]
        _Assembler(),  # type: ignore[arg-type]
        on_latency=observed.append,
    )

    await handler.handle(_discovered(seconds_ago=2.0))

    assert len(observed) == 1
    # It spans the discovery event, not just the compute call.
    assert observed[0] >= 2.0


async def test_a_failed_computation_records_no_latency() -> None:
    """Otherwise the pipeline looks faster the more often it breaks."""
    observed: list[float] = []
    handler = FeatureComputationHandler(
        _Engine(),  # type: ignore[arg-type]
        _Assembler(fails=True),  # type: ignore[arg-type]
        on_latency=observed.append,
    )

    await handler.handle(_discovered(seconds_ago=2.0))

    assert observed == []


async def test_a_raising_latency_sink_never_breaks_the_pipeline() -> None:
    engine = _Engine()

    def _explode(seconds: float) -> None:
        raise RuntimeError("metrics backend down")

    handler = FeatureComputationHandler(
        engine,  # type: ignore[arg-type]
        _Assembler(),  # type: ignore[arg-type]
        on_latency=_explode,
    )

    await handler.handle(_discovered())

    assert engine.calls == 1  # the work still happened


async def test_the_handler_works_without_a_latency_sink() -> None:
    engine = _Engine()
    handler = FeatureComputationHandler(
        engine,  # type: ignore[arg-type]
        _Assembler(),  # type: ignore[arg-type]
    )

    await handler.handle(_discovered())

    assert engine.calls == 1
