"""Fast-Path Executor — a second live adapter, built to be *measured*.

This does **not** replace :class:`~...live_executor.LiveExecutor`. Both satisfy
the same :class:`~...domain.ports.Executor` port and both sit behind the Risk
Manager, which remains the only component that authorises a trade. The two
differ in exactly two ways:

1. **The transport is a seam.** Where the live executor calls the signer's
   ``sign_and_send`` directly, this one goes through a
   :class:`~...domain.ports.TransactionSubmitter`, so a staked/dual-routed sender
   or a Jito bundle can be swapped in without touching the execution sequence.
2. **It times every stage.** Quote, submit and confirm are measured separately
   and reported as a :class:`LatencyBreakdown` on the fill. That breakdown is the
   entire reason this class exists: without it, "the fast path is faster" is an
   assertion, and the tip it would cost is unjustifiable.

The sequence and its fail-closed discipline are deliberately identical to the
live executor's — ``quote → slippage guard → submit → confirm → report`` — so
that a shadow comparison measures the *transport*, not two different algorithms.

**Off by default.** The factory builds this only when ``EXECUTION_FAST_PATH_ENABLED``
is set *and* the hard live gate is already open. Nothing here is enabled by this
commit; see ``docs/EXECUTION_FAST_PATH_2026-08-04.md``.
"""

from __future__ import annotations

import time
from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money
from hades.contexts.execution.application.confirmation import ConfirmationEngine
from hades.contexts.execution.application.fees import FeeEngine
from hades.contexts.execution.application.retry import RetryableError, RetryEngine
from hades.contexts.execution.application.swap_manager import SwapManager
from hades.contexts.execution.domain.models import (
    ExecutionMode,
    FillReport,
    LatencyBreakdown,
    OrderRequest,
    OrderSide,
    OrderStatus,
    SendReceipt,
)
from hades.contexts.execution.domain.ports import TransactionSubmitter
from hades.shared_kernel.errors import ValidationError
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.fastpath")


class _Stopwatch:
    """Monotonic stage timer. Milliseconds, never negative, never a wall clock."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._mark = self._started

    def lap(self) -> int:
        """Milliseconds since the previous lap (or since construction)."""
        now = time.monotonic()
        elapsed = int((now - self._mark) * 1000)
        self._mark = now
        return max(0, elapsed)

    @property
    def total_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started) * 1000))


class FastPathExecutor:
    """Executes a live swap over a pluggable transport, timing each stage.

    Satisfies ``Executor``. Reports ``mode == "live"`` because that is what it
    is — a real-funds path. It is never the default: the factory keeps the
    original live executor unless the fast-path flag is explicitly on.
    """

    def __init__(
        self,
        *,
        swap_manager: SwapManager,
        submitter: TransactionSubmitter,
        confirmation: ConfirmationEngine,
        fee_engine: FeeEngine,
        retry: RetryEngine,
        owner: str,
    ) -> None:
        self._swap = swap_manager
        self._submitter = submitter
        self._confirmation = confirmation
        self._fees = fee_engine
        self._retry = retry
        self._owner = owner

    @property
    def mode(self) -> str:
        return ExecutionMode.LIVE.value

    @property
    def route(self) -> str:
        """The transport this adapter is wired to — the label in comparisons."""
        return self._submitter.route

    async def execute(self, request: OrderRequest) -> FillReport:
        watch = _Stopwatch()
        try:
            return await self._execute(request, watch)
        except ValidationError as exc:  # slippage over budget — a clean cancel
            return self._failed(request, str(exc), watch)
        except RetryableError as exc:  # exhausted submit retries
            return self._failed(request, str(exc), watch)
        except Exception as exc:  # unknown failure → fail closed, never optimistic
            _logger.error(
                "fastpath_execute_error",
                mint=str(request.token.mint),
                route=self.route,
                error=str(exc),
            )
            return self._failed(request, f"unexpected: {exc}", watch)

    async def _execute(self, request: OrderRequest, watch: _Stopwatch) -> FillReport:
        prepared = await self._swap.prepare(request, owner=self._owner)
        quote_ms = watch.lap()

        async def _submit(attempt: int) -> SendReceipt:
            receipt = await self._submitter.submit(prepared.serialized_tx)
            if not receipt.accepted:
                raise RetryableError(
                    f"submit rejected (attempt {attempt}): {receipt.error or 'not accepted'}"
                )
            return receipt

        receipt = await self._retry.run(_submit)
        assert isinstance(receipt, SendReceipt)
        submit_ms = watch.lap()

        if receipt.signature is None:
            # Accepted without a signature is not a success we can confirm or
            # reconcile later. Treat it as a failure rather than lose the trail.
            return self._failed(
                request, "submitter accepted without a signature", watch, quote_ms=quote_ms
            )

        confirmed = await self._confirmation.confirm(receipt.signature)
        confirm_ms = watch.lap()

        latency = LatencyBreakdown(
            quote_ms=quote_ms,
            submit_ms=submit_ms,
            confirm_ms=confirm_ms,
            total_ms=watch.total_ms,
            route=receipt.route,
            landed_slot=confirmed.slot,
            complete=confirmed.confirmed,
        )

        if not confirmed.confirmed:
            return self._failed(
                request,
                confirmed.error or "not confirmed",
                watch,
                signature=receipt.signature,
                latency=latency,
            )

        notional = Decimal(str(request.notional.amount))
        fees = self._fees.estimate(notional_usd=notional)
        quantity = _received_quantity(prepared.quote, request.side)
        price = prepared.quote.price if prepared.quote.price > 0 else Decimal(1)

        # The line the whole adapter exists to produce. Emitted at INFO on every
        # fill so a landing-latency regression is visible in the log stream, not
        # only in a histogram nobody is looking at.
        _logger.info(
            "fastpath_landed",
            mint=str(request.token.mint),
            side=request.side.value,
            route=latency.route,
            signature=receipt.signature,
            quote_ms=latency.quote_ms,
            submit_ms=latency.submit_ms,
            confirm_ms=latency.confirm_ms,
            total_ms=latency.total_ms,
            landed_slot=latency.landed_slot,
            tip_lamports=receipt.tip_lamports,
            notional_usd=float(notional),
        )
        return FillReport(
            token=request.token,
            side=request.side,
            status=OrderStatus.FILLED,
            filled_quantity=quantity,
            average_price=Money(amount=price),
            fees=fees.as_money(),
            mode=self.mode,
            slippage_bps=int(prepared.quote.price_impact_pct * 100.0),
            latency_ms=latency.total_ms,
            notional=Money(amount=notional),
            fee_breakdown=fees,
            confirmation=confirmed,
            signature=receipt.signature,
            latency=latency,
        )

    def _failed(
        self,
        request: OrderRequest,
        reason: str,
        watch: _Stopwatch,
        *,
        signature: str | None = None,
        quote_ms: int = 0,
        latency: LatencyBreakdown | None = None,
    ) -> FillReport:
        # A failure still carries what was measured up to the failure point: a
        # slow path that fails is exactly the case worth diagnosing.
        measured = latency or LatencyBreakdown(
            quote_ms=quote_ms,
            total_ms=watch.total_ms,
            route=self.route,
        )
        return FillReport(
            token=request.token,
            side=request.side,
            status=OrderStatus.FAILED,
            filled_quantity=Decimal(0),
            average_price=Money(amount=Decimal(0)),
            fees=Money(amount=Decimal(0)),
            mode=self.mode,
            latency_ms=measured.total_ms,
            notional=request.notional,
            signature=signature,
            error=reason,
            latency=measured,
        )


def _received_quantity(quote: object, side: OrderSide) -> Decimal:
    """Tokens received: a BUY receives ``out_amount``; a SELL sold ``in_amount``."""
    out_amount = getattr(quote, "out_amount", Decimal(0))
    in_amount = getattr(quote, "in_amount", Decimal(0))
    return out_amount if side is OrderSide.BUY else in_amount
