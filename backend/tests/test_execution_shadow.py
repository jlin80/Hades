"""The shadow harness measures a candidate without ever risking an order.

The invariants under test, in order of how much they would cost to get wrong:

1. A **live candidate is refused** — shadowing one would submit a second real
   transaction per order.
2. The **primary's fill is the only one returned**, whatever the candidate does.
3. A candidate that raises, fails or hangs **cannot fail or stall the order**.
4. Only runs where *both* sides filled count as a latency comparison.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.execution.application.shadow import (
    ShadowExecutor,
    ShadowSafetyError,
)
from hades.contexts.execution.domain.models import (
    ExecutionMode,
    FillReport,
    OrderRequest,
    OrderSide,
    OrderStatus,
)
from hades.contexts.execution.domain.ports import Executor

_MINT = "So11111111111111111111111111111111111111112"


def _request() -> OrderRequest:
    return OrderRequest(
        token=TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL"),
        side=OrderSide.BUY,
        notional=Money(amount=Decimal(50)),
        max_slippage_bps=300,
    )


class _Fake:
    """An executor with a scripted outcome, delay and identity."""

    def __init__(
        self,
        *,
        mode: str = ExecutionMode.PAPER.value,
        latency_ms: int = 100,
        delay_s: float = 0.0,
        fills: bool = True,
        raises: bool = False,
        price: Decimal = Decimal(1),
    ) -> None:
        self._mode = mode
        self.latency_ms = latency_ms  # mutable: lets a test inject one outlier run
        self._delay = delay_s
        self._fills = fills
        self._raises = raises
        self._price = price
        self.calls = 0

    @property
    def mode(self) -> str:
        return self._mode

    async def execute(self, request: OrderRequest) -> FillReport:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("candidate exploded")
        return FillReport(
            token=request.token,
            side=request.side,
            status=OrderStatus.FILLED if self._fills else OrderStatus.FAILED,
            filled_quantity=Decimal(10) if self._fills else Decimal(0),
            average_price=Money(amount=self._price),
            fees=Money(amount=Decimal("0.1")),
            mode=self._mode,
            latency_ms=self.latency_ms,
            error=None if self._fills else "candidate could not fill",
        )


# -- the safety invariant -----------------------------------------------------


def test_a_live_candidate_is_refused_outright() -> None:
    """Shadowing live would double-trade with real funds. No flag permits it."""
    with pytest.raises(ShadowSafetyError, match="second real transaction"):
        ShadowExecutor(
            primary=_Fake(),
            candidate=_Fake(mode=ExecutionMode.LIVE.value),
        )


def test_shadow_reports_the_primarys_mode() -> None:
    shadow = ShadowExecutor(primary=_Fake(), candidate=_Fake())
    assert isinstance(shadow, Executor)
    assert shadow.mode == ExecutionMode.PAPER.value


# -- the primary is never affected --------------------------------------------


async def test_only_the_primarys_fill_is_returned() -> None:
    primary = _Fake(price=Decimal(7), latency_ms=100)
    candidate = _Fake(price=Decimal(999), latency_ms=40)
    shadow = ShadowExecutor(primary=primary, candidate=candidate)

    fill = await shadow.execute(_request())

    assert fill.average_price.amount == Decimal(7)
    assert fill.latency_ms == 100
    assert candidate.calls == 1  # it did run — it just never leaves the class


async def test_a_raising_candidate_cannot_fail_the_order() -> None:
    shadow = ShadowExecutor(primary=_Fake(), candidate=_Fake(raises=True))

    fill = await shadow.execute(_request())

    assert fill.filled
    assert shadow.stats.candidate_failures == 1
    assert shadow.stats.comparable == 0


async def test_a_hanging_candidate_is_cancelled_and_cannot_stall_the_order() -> None:
    slow = _Fake(delay_s=5.0)
    shadow = ShadowExecutor(primary=_Fake(), candidate=slow, timeout_seconds=0.1)

    started = asyncio.get_running_loop().time()
    fill = await shadow.execute(_request())
    elapsed = asyncio.get_running_loop().time() - started

    assert fill.filled
    assert elapsed < 2.0, "the order waited on the shadow"
    assert shadow.stats.candidate_timeouts == 1


async def test_a_failing_candidate_is_recorded_not_counted_as_comparable() -> None:
    shadow = ShadowExecutor(primary=_Fake(), candidate=_Fake(fills=False))

    await shadow.execute(_request())

    assert shadow.stats.runs == 1
    assert shadow.stats.comparable == 0
    assert shadow.stats.candidate_failures == 1
    assert shadow.stats.median_delta_ms is None


# -- the comparison itself ----------------------------------------------------


async def test_comparable_runs_yield_a_median_delta_and_a_fill_rate() -> None:
    primary = _Fake(latency_ms=200)
    candidate = _Fake(latency_ms=50)
    shadow = ShadowExecutor(primary=primary, candidate=candidate)

    for _ in range(3):
        await shadow.execute(_request())

    assert shadow.stats.runs == 3
    assert shadow.stats.comparable == 3
    assert shadow.stats.candidate_fill_rate == 1.0
    # Negative: the candidate landed sooner than the primary.
    assert shadow.stats.median_delta_ms == -150


async def test_a_primary_failure_is_not_a_comparison_either() -> None:
    """Comparing latency against an order that never filled measures nothing."""
    shadow = ShadowExecutor(primary=_Fake(fills=False), candidate=_Fake())

    fill = await shadow.execute(_request())

    assert not fill.filled
    assert shadow.stats.comparable == 0
    assert shadow.stats.median_delta_ms is None


async def test_median_ignores_a_single_outlier() -> None:
    """A long right tail must not decide whether a paid route looks worthwhile."""
    candidate = _Fake(latency_ms=90)
    shadow = ShadowExecutor(primary=_Fake(latency_ms=100), candidate=candidate)
    await shadow.execute(_request())
    await shadow.execute(_request())
    # One pathologically slow candidate run — a retry, a near-timeout confirmation.
    candidate.latency_ms = 100_000
    await shadow.execute(_request())

    assert shadow.stats.median_delta_ms == -10
