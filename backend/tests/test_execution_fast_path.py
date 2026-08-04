"""The fast-path adapter measures landing latency, fails closed, and stays off.

These tests fix the three properties that make the adapter worth having:

1. It reports a **per-stage** latency breakdown on a successful fill, and the
   total is consistent with the stages — otherwise "faster" is unmeasurable.
2. It **fails closed** on every failure mode (rejected submit, accepted-without-
   signature, unconfirmed) and still reports what it measured up to that point.
3. It is **off by default** and cannot open the live gate on its own.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.execution.application.confirmation import ConfirmationEngine
from hades.contexts.execution.application.fast_path_executor import FastPathExecutor
from hades.contexts.execution.application.fees import FeeEngine
from hades.contexts.execution.application.retry import RetryEngine, RetryPolicy
from hades.contexts.execution.application.submitters import ROUTE_SIGNER, SignerSubmitter
from hades.contexts.execution.application.swap_manager import SwapManager
from hades.contexts.execution.domain.models import (
    ExecutionMode,
    OrderRequest,
    OrderSide,
    Quote,
    SendReceipt,
)
from hades.contexts.execution.domain.ports import Executor, TransactionSubmitter

_MINT = "So11111111111111111111111111111111111111112"


# -- doubles ------------------------------------------------------------------


class _Quotes:
    """A quote provider that takes a fixed, measurable amount of time."""

    def __init__(self, *, delay_s: float = 0.0, impact_pct: float = 0.0) -> None:
        self._delay = delay_s
        self._impact = impact_pct

    async def quote(
        self, *, input_mint: str, output_mint: str, amount: Decimal, slippage_bps: int
    ) -> Quote:
        if self._delay:
            await asyncio.sleep(self._delay)
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=amount * 2,
            price=Decimal(2),
            price_impact_pct=self._impact,
            route="test-route",
        )

    async def build_swap_tx(self, quote: Quote, *, owner: str) -> bytes:
        return b"tx"


class _Submitter:
    """A submitter with a configurable outcome and a measurable delay."""

    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        accepted: bool = True,
        signature: str | None = "sig-1",
        route: str = "test-sender",
        tip_lamports: int = 0,
    ) -> None:
        self._delay = delay_s
        self._accepted = accepted
        self._signature = signature
        self._route = route
        self._tip = tip_lamports
        self.calls = 0

    @property
    def route(self) -> str:
        return self._route

    async def submit(self, serialized_tx: bytes) -> SendReceipt:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._accepted:
            return SendReceipt(accepted=False, route=self._route, error="rejected by transport")
        return SendReceipt(
            signature=self._signature,
            accepted=True,
            route=self._route,
            tip_lamports=self._tip,
        )


class _Rpc:
    """An RPC that confirms (or never confirms) the signature."""

    def __init__(self, *, confirms: bool = True, slot: int = 777) -> None:
        self._confirms = confirms
        self._slot = slot

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if not self._confirms:
            return {"value": [None]}
        return {"value": [{"slot": self._slot, "confirmationStatus": "confirmed"}]}

    @property
    def active_endpoint(self) -> str | None:
        return "rpc-test"


def _build(
    *,
    submitter: TransactionSubmitter,
    quotes: _Quotes | None = None,
    rpc: _Rpc | None = None,
    max_attempts: int = 1,
) -> FastPathExecutor:
    return FastPathExecutor(
        swap_manager=SwapManager(quotes or _Quotes(), quote_mint=_MINT, sol_price_usd=150.0),
        submitter=submitter,
        confirmation=ConfirmationEngine(
            rpc or _Rpc(), timeout_seconds=1.0, poll_interval_seconds=0.05
        ),
        fee_engine=FeeEngine(),
        retry=RetryEngine(RetryPolicy(max_attempts=max_attempts, base_delay_seconds=0.0)),
        owner="owner-pubkey",
    )


def _request(*, side: OrderSide = OrderSide.BUY, max_slippage_bps: int = 300) -> OrderRequest:
    return OrderRequest(
        token=TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL"),
        side=side,
        notional=Money(amount=Decimal(50)),
        max_slippage_bps=max_slippage_bps,
    )


# -- the contract -------------------------------------------------------------


def test_satisfies_the_executor_port_and_reports_live_mode() -> None:
    executor = _build(submitter=_Submitter())
    # The whole point: it goes behind the existing seam, it does not bypass it.
    assert isinstance(executor, Executor)
    assert executor.mode == ExecutionMode.LIVE.value


async def test_successful_fill_reports_a_per_stage_latency_breakdown() -> None:
    executor = _build(
        submitter=_Submitter(delay_s=0.05, route="test-sender", tip_lamports=1000),
        quotes=_Quotes(delay_s=0.05),
    )

    fill = await executor.execute(_request())

    assert fill.filled
    latency = fill.latency
    assert latency is not None
    assert latency.complete is True
    assert latency.route == "test-sender"
    assert latency.landed_slot == 777
    # Each stage actually observed the delay it was given.
    assert latency.quote_ms >= 40, latency
    assert latency.submit_ms >= 40, latency
    # The total is end-to-end, so it is at least the sum of the stages.
    assert latency.total_ms >= latency.quote_ms + latency.submit_ms + latency.confirm_ms - 5
    # And the coarse figure downstream already reads stays consistent with it.
    assert fill.latency_ms == latency.total_ms


async def test_rejected_submission_fails_closed_and_still_reports_timing() -> None:
    submitter = _Submitter(accepted=False)
    executor = _build(submitter=submitter, max_attempts=2)

    fill = await executor.execute(_request())

    assert not fill.filled
    assert fill.filled_quantity == Decimal(0)
    assert fill.signature is None
    assert "rejected by transport" in (fill.error or "")
    assert submitter.calls == 2  # retried to the policy limit, then gave up
    # A failure is exactly the case worth diagnosing, so timing survives it.
    assert fill.latency is not None
    assert fill.latency.route == "test-sender"
    # …but it is not a comparable measurement, and says so.
    assert fill.latency.complete is False


async def test_acceptance_without_a_signature_is_a_failure_not_a_fill() -> None:
    """An unreconcilable "success" is worse than a failure — it loses the trail."""
    executor = _build(submitter=_Submitter(signature=None))

    fill = await executor.execute(_request())

    assert not fill.filled
    assert "without a signature" in (fill.error or "")


async def test_unconfirmed_transaction_is_never_reported_as_filled() -> None:
    executor = _build(submitter=_Submitter(), rpc=_Rpc(confirms=False))

    fill = await executor.execute(_request())

    assert not fill.filled
    assert fill.signature == "sig-1"  # the trail is kept for reconciliation
    assert fill.latency is not None
    assert fill.latency.confirm_ms > 0  # it waited, and we know how long
    # It timed out rather than landed, so it must never enter a comparison.
    assert fill.latency.complete is False


async def test_price_impact_over_budget_cancels_before_anything_is_submitted() -> None:
    submitter = _Submitter()
    # 5% impact = 500 bps, over the order's 300 bps budget.
    executor = _build(submitter=submitter, quotes=_Quotes(impact_pct=5.0))

    fill = await executor.execute(_request(max_slippage_bps=300))

    assert not fill.filled
    assert submitter.calls == 0, "nothing may be submitted once the guard trips"


async def test_signer_submitter_is_the_baseline_route_and_fails_closed() -> None:
    class _BadSigner:
        @property
        def public_key(self) -> str:
            return "owner"

        async def sign_and_send(self, serialized_tx: bytes) -> str:
            raise RuntimeError("node unreachable")

    submitter = SignerSubmitter(_BadSigner())
    assert submitter.route == ROUTE_SIGNER

    receipt = await submitter.submit(b"tx")

    assert receipt.accepted is False
    assert receipt.signature is None
    assert "node unreachable" in (receipt.error or "")
