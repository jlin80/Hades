"""The assembler waits on independent lookups concurrently, not in a queue.

Why this is a throughput test and not a style test: a consumer lane awaits each
handler in turn (``_dispatch`` loops over handlers with ``await``), so a lane's
event rate is exactly ``1 / per-event latency``. Every serialised round-trip in
the Security handler is therefore a direct divisor of the platform's throughput.

The analyzers are pure synchronous functions over an already-assembled bundle and
cost microseconds. The seconds were always in the waiting, and four of those
waits had no reason to be ordered.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.features.domain.events import FeaturesComputed
from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.security.domain.models import (
    ClusterReport,
    DeveloperReputation,
    HolderBalance,
    HoneypotProbe,
    MintAccount,
)
from hades.contexts.security.domain.ports import StoredTokenFacts
from hades.contexts.security.infrastructure.assembler import SecurityContextAssembler
from hades.shared_kernel.domain.identifiers import new_id

_MINT = "So11111111111111111111111111111111111111112"
_DEPLOYER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
#: Long enough that a serialised run is unmistakable, short enough to stay fast.
_STEP = 0.05


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


class _SlowReader:
    """Every read costs ``_STEP``, like a real RPC round-trip."""

    async def get_mint_account(self, mint: str) -> MintAccount | None:
        await asyncio.sleep(_STEP)
        return None

    async def get_largest_holders(self, mint: str, *, limit: int = 20) -> list[HolderBalance]:
        await asyncio.sleep(_STEP)
        return []

    async def get_holder_count(self, mint: str) -> int | None:
        await asyncio.sleep(_STEP)
        return 42

    async def get_lp_secure_pct(self, lp_mint: str) -> tuple[float | None, float | None]:
        await asyncio.sleep(_STEP)
        return (100.0, None)

    async def get_funders(self, wallet: str, *, limit: int = 25) -> list[str]:
        await asyncio.sleep(_STEP)
        return []


class _SlowFacts:
    async def load(self, token: TokenRef) -> StoredTokenFacts | None:
        await asyncio.sleep(_STEP)
        return StoredTokenFacts(
            first_seen_at=datetime.now(UTC),
            deployer=_DEPLOYER,
            pool_address="pool1111111111111111111111111111111111111111",
            pool_dex="raydium",
            pool_liquidity_usd=Decimal("25000"),
            lp_mint="lp11111111111111111111111111111111111111111",
        )


class _SlowSimulator:
    async def probe(self, token: TokenRef, *, amount_usd: float) -> HoneypotProbe | None:
        await asyncio.sleep(_STEP)
        return None


class _SlowDeveloperStore:
    async def get(self, deployer: object) -> DeveloperReputation | None:
        await asyncio.sleep(_STEP)
        return None

    async def observe_token(self, deployer: object, *, mint: str) -> None:
        return None


class _SlowClusterDetector:
    async def detect(
        self, token: TokenRef, holders: list[HolderBalance]
    ) -> ClusterReport | None:
        await asyncio.sleep(_STEP)
        return None


def _assembler() -> SecurityContextAssembler:
    return SecurityContextAssembler(
        reader=_SlowReader(),
        facts_source=_SlowFacts(),
        swap_simulator=_SlowSimulator(),
        developer_store=_SlowDeveloperStore(),
        cluster_detector=_SlowClusterDetector(),
        fetch_holder_count=True,
    )


def _event() -> FeaturesComputed:
    features = FeatureSet(
        token=_token(),
        computed_at=datetime.now(UTC),
        values={"liquidity_usd": 25000.0},
    )
    return FeaturesComputed(
        aggregate_id=new_id(),
        token=_token(),
        features=features,
        feature_count=len(features.values),
    )


async def test_independent_lookups_do_not_run_in_a_queue() -> None:
    """Two waves of I/O, not one wave and then four chained awaits.

    Wave 1 (mint account, holders, stored facts, honeypot) was already
    concurrent. Wave 2 — developer reputation, wallet clustering, the LP
    burn/lock read and the holder count — depends only on wave 1 and never on
    itself, but ran as four sequential awaits.

    Serialised, this assembler costs at least 8 steps (4 concurrent + 4 chained,
    where the pool read is itself two hops: stored facts then the LP mint).
    Concurrent, wave 2 collapses to its slowest member.
    """
    started = asyncio.get_running_loop().time()
    inputs = await _assembler().build(_event())
    elapsed = asyncio.get_running_loop().time() - started

    # Wave 1 (1 step) + wave 2, whose slowest member is the pool read at 1 step.
    # Generous ceiling so a loaded CI box cannot make this flaky, while still
    # sitting far below the ~5 steps a serialised wave 2 would cost.
    assert elapsed < _STEP * 4, f"assembly took {elapsed:.3f}s — wave 2 is serialised"

    # And it still assembles everything: concurrency must not cost correctness.
    assert inputs.token == _token()
    assert inputs.holder_count == 42
    assert inputs.pool is not None
    assert inputs.pool.lp_burned_pct == 100.0
    assert inputs.deployer is not None
    assert str(inputs.deployer) == _DEPLOYER


async def test_an_unknown_deployer_costs_nothing() -> None:
    """No deployer must skip the lookup, not serialise the wave behind a branch."""

    class _NoDeployer(_SlowFacts):
        async def load(self, token: TokenRef) -> StoredTokenFacts | None:
            await asyncio.sleep(_STEP)
            return StoredTokenFacts(first_seen_at=datetime.now(UTC))

    assembler = SecurityContextAssembler(
        reader=_SlowReader(),
        facts_source=_NoDeployer(),
        swap_simulator=_SlowSimulator(),
        developer_store=_SlowDeveloperStore(),
        cluster_detector=_SlowClusterDetector(),
    )
    inputs = await assembler.build(_event())
    assert inputs.developer is None
    assert inputs.deployer is None
    assert inputs.pool is None  # no pool address stored


async def test_a_failing_lookup_degrades_to_doubt_without_sinking_the_rest() -> None:
    """The gather must not let one raising source cancel its siblings."""

    class _BrokenClusters(_SlowClusterDetector):
        async def detect(
            self, token: TokenRef, holders: list[HolderBalance]
        ) -> ClusterReport | None:
            raise RuntimeError("rpc budget exhausted")

    assembler = SecurityContextAssembler(
        reader=_SlowReader(),
        facts_source=_SlowFacts(),
        swap_simulator=_SlowSimulator(),
        developer_store=_SlowDeveloperStore(),
        cluster_detector=_BrokenClusters(),
        fetch_holder_count=True,
    )
    inputs = await assembler.build(_event())

    assert inputs.cluster is None  # the failure became doubt
    assert inputs.holder_count == 42  # its siblings still arrived
    assert inputs.pool is not None
