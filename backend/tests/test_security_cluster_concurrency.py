"""Funder lookups overlap, within a bound, and the answer does not change.

This loop was the platform's throughput. Measured live via the metrics added the
same session:

    hades_security_assemble_seconds   1563.34 / 73 = 21.42 s per token
    hades_security_analyzers_seconds     0.638 / 64 =  0.010 s per token

Twelve funder lookups, awaited one after another, against ten milliseconds for
every analyzer combined — a ratio of roughly 2,140 to 1. The brief that opened the
session blamed the analyzers.
"""

from __future__ import annotations

import asyncio

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.security.domain.models import HolderBalance
from hades.contexts.security.infrastructure.cluster_detector import (
    FundingGraphClusterDetector,
)

_MINT = "So11111111111111111111111111111111111111112"
_STEP = 0.05


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


def _holders(n: int) -> list[HolderBalance]:
    return [
        HolderBalance(owner=f"holder{i:02d}", pct=float(n - i), amount=float(n - i))
        for i in range(n)
    ]


class _SlowReader:
    """Each funder lookup costs a round-trip; records peak in-flight calls."""

    def __init__(self, funder_of: dict[str, list[str]] | None = None) -> None:
        self._funders = funder_of or {}
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    async def get_funders(self, wallet: str, *, limit: int = 25) -> list[str]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls += 1
        try:
            await asyncio.sleep(_STEP)
            return self._funders.get(wallet, [])
        finally:
            self.in_flight -= 1


async def test_lookups_overlap_instead_of_queueing() -> None:
    """Twelve lookups at the default concurrency cost three waves, not twelve.

    Deliberately constructed *without* passing ``concurrency``: against the
    sequential version this must fail on elapsed time, and a test that instead
    failed with ``TypeError`` on an unknown keyword would prove only that a
    signature changed.
    """
    reader = _SlowReader()
    detector = FundingGraphClusterDetector(reader, max_holders=12)  # type: ignore[arg-type]

    started = asyncio.get_running_loop().time()
    await detector.detect(_token(), _holders(12))
    elapsed = asyncio.get_running_loop().time() - started

    assert reader.calls == 12
    # Sequential would be 12 steps (0.6 s). Three waves is 3 (0.15 s). The ceiling
    # is generous so a loaded box cannot make this flaky while still sitting far
    # below the sequential cost.
    assert elapsed < _STEP * 7, f"took {elapsed:.3f}s — lookups are still queueing"


async def test_concurrency_is_bounded_not_unbounded() -> None:
    """The bound is the point: a burst of twelve would be rate-limited into doubt."""
    reader = _SlowReader()
    detector = FundingGraphClusterDetector(reader, max_holders=12, concurrency=4)  # type: ignore[arg-type]

    await detector.detect(_token(), _holders(12))

    assert reader.peak <= 4, f"{reader.peak} lookups were in flight at once"


async def test_clustering_is_unchanged_by_concurrency() -> None:
    """Same input, same clusters — and membership in the sample's order.

    Consuming results in completion order would let two workers describe the same
    token differently depending on which RPC answered first.
    """
    shared = {f"holder{i:02d}": ["whale-funder"] for i in range(4)}
    reader = _SlowReader(shared)
    detector = FundingGraphClusterDetector(
        reader,  # type: ignore[arg-type]
        max_holders=12,
        min_members=3,
        concurrency=4,
    )

    report = await detector.detect(_token(), _holders(12))

    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert cluster.funder == "whale-funder"
    assert cluster.members == ("holder00", "holder01", "holder02", "holder03")
    assert report.analysed_wallets == 12
    assert report.data_complete is True


async def test_a_failing_lookup_lowers_coverage_without_sinking_the_rest() -> None:
    class _PartlyBroken(_SlowReader):
        async def get_funders(self, wallet: str, *, limit: int = 25) -> list[str]:
            if wallet == "holder05":
                raise RuntimeError("rpc budget exhausted")
            return await super().get_funders(wallet, limit=limit)

    reader = _PartlyBroken({f"holder{i:02d}": ["whale-funder"] for i in range(4)})
    detector = FundingGraphClusterDetector(reader, max_holders=12, concurrency=4)  # type: ignore[arg-type]

    report = await detector.detect(_token(), _holders(12))

    assert report.data_complete is False  # the gap becomes doubt
    assert report.analysed_wallets == 11  # its siblings still answered
    assert len(report.clusters) == 1
