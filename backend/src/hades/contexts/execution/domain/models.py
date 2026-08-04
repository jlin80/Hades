"""Execution value objects — the mode-agnostic vocabulary of the Execution Engine.

Every type here is identical for paper and live: an :class:`OrderRequest` looks
the same whether it will be simulated or signed on-chain, and a
:class:`FillReport` has the same shape whichever executor produced it. That is
what lets everything downstream (Portfolio, Learning, Dashboard) stay oblivious
to the mode — only the executor adapter differs. All monetary/quantity values
use :class:`Decimal`/:class:`Money` (never float) to avoid rounding error.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from hades.contexts.common.domain.value_objects import Money, TokenRef
from hades.shared_kernel.domain.base import ValueObject


class ExecutionMode(StrEnum):
    """The two *operational* execution modes. Replay/backtest reuse the paper seam."""

    PAPER = "paper"
    LIVE = "live"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    """Terminal-ish status carried on a :class:`FillReport`."""

    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FILLED = "filled"
    FAILED = "failed"


class OrderState(StrEnum):
    """Full lifecycle state tracked by the :class:`OrderManager`.

    An order flows ``PENDING → SUBMITTED → CONFIRMING → FILLED`` on the happy
    path, or lands in ``FAILED``/``CANCELLED``. Trazabilidad is never lost: every
    order the engine ever created is retained with its final state.
    """

    PENDING = "pending"
    SUBMITTED = "submitted"
    CONFIRMING = "confirming"
    FILLED = "filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.FAILED, OrderState.CANCELLED})


def is_terminal(state: OrderState) -> bool:
    return state in _TERMINAL_STATES


# -- risk-envelope tag keys ---------------------------------------------------
# The Risk Manager approves a size *and* the exit envelope around it (take-profit,
# stop-loss, trailing). Tags are the pass-through channel that carries the
# envelope from the approval, through the order, onto ``PositionOpened`` — which
# is where the Position Monitor picks it up. Both writer (ExecutionEngine) and
# reader (PositionMonitor) key off these constants so the contract is one edit.

TAG_TAKE_PROFIT_PCT = "take_profit_pct"
TAG_STOP_LOSS_PCT = "stop_loss_pct"
TAG_TRAILING_ENABLED = "trailing_enabled"
TAG_TRAILING_ACTIVATION_PCT = "trailing_activation_pct"
TAG_TRAILING_DISTANCE_PCT = "trailing_distance_pct"
#: Why a SELL was issued — surfaced on ``PositionClosed`` and the trade log.
TAG_EXIT_REASON = "exit_reason"


class OrderRequest(ValueObject):
    """An intent to trade handed to an :class:`Executor` (paper or live).

    Built by the :class:`ExecutionEngine` from a ``TradeApproved`` sizing
    envelope. The risk attribution (``tags``) travels with the order so the
    opened position can be attributed without re-deriving it; the executor never
    reads the tags — they are pass-through metadata.
    """

    token: TokenRef
    side: OrderSide
    notional: Money
    max_slippage_bps: int
    strategy: str = "default"
    correlation_id: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class SlippageContext(ValueObject):
    """Inputs the :class:`SlippageEngine` scores into a dynamic slippage estimate.

    Slippage is never a fixed constant: thin liquidity, high volatility, a wide
    spread, a large position relative to the pool, an illiquid DEX or an off-peak
    hour all widen the expected slippage. Missing inputs degrade to *more* caution.
    """

    liquidity_usd: float = 0.0
    volatility_pct: float = 0.0
    spread_bps: float = 0.0
    notional_usd: float = 0.0
    dex: str = "unknown"
    off_peak: bool = False


class SlippageEstimate(ValueObject):
    """A recommended and a hard-max slippage, with the reason and a limit verdict."""

    recommended_bps: int
    max_bps: int
    reason: str = ""
    within_limits: bool = True


class FeeBreakdown(ValueObject):
    """Every fee component of a trade, plus the priority-fee bid (live)."""

    network_fee_usd: Decimal = Decimal(0)
    priority_fee_usd: Decimal = Decimal(0)
    dex_fee_usd: Decimal = Decimal(0)
    total_usd: Decimal = Decimal(0)
    priority_fee_microlamports: int = 0

    def as_money(self) -> Money:
        return Money(amount=self.total_usd)


class Quote(ValueObject):
    """A swap quote from an aggregator/DEX (live only).

    ``price`` is output-per-input; ``price_impact_pct`` is the quote's own
    estimate of how much the size moves the pool. The :class:`LiveExecutor`
    checks the impact against the order's slippage budget before signing.
    """

    input_mint: str
    output_mint: str
    in_amount: Decimal
    out_amount: Decimal
    price: Decimal
    price_impact_pct: float = 0.0
    route: str = ""


class SendReceipt(ValueObject):
    """What a :class:`~...ports.TransactionSubmitter` gives back after submitting.

    ``accepted`` says only that the transport took the transaction — never that it
    landed. Landing is what the :class:`ConfirmationResult` answers. Keeping the
    two apart is the whole point: a route can accept instantly and still never
    include the transaction, and a fast path that conflated them would report
    excellent latency for transactions that never happened.
    """

    signature: str | None = None
    accepted: bool = False
    #: Which transport carried it ("signer", "sender", "jito"…) — for comparison.
    route: str = "unknown"
    #: Tip paid to enter a priority auction, in lamports. Zero on the plain path.
    tip_lamports: int = 0
    error: str | None = None


class LatencyBreakdown(ValueObject):
    """End-to-end landing latency, split by stage. The unit of comparison.

    ``total_ms`` is wall time from the moment the executor starts to the moment
    the transaction is confirmed on-chain — the number that decides whether a
    fast path is worth its tip. The per-stage fields say *where* the time went,
    which is what turns "the new adapter is 200 ms faster" into an actionable
    claim. Every field is best-effort: a stage that did not run stays at zero.
    """

    quote_ms: int = 0
    submit_ms: int = 0
    confirm_ms: int = 0
    total_ms: int = 0
    route: str = "unknown"
    #: Slot the transaction landed in, when the confirmation reported one.
    landed_slot: int | None = None
    #: True only when every stage ran to a confirmed landing. Set explicitly by
    #: the executor rather than inferred from the durations: a stage legitimately
    #: takes 0 ms (a confirmation served from the first poll), so a zero is not
    #: evidence that the stage did not run. Only a ``complete`` breakdown belongs
    #: in a latency comparison — a partial one would flatter whichever route
    #: failed earliest.
    complete: bool = False


class ConfirmationResult(ValueObject):
    """The outcome of waiting for on-chain confirmation (live only).

    Paper fills are confirmed instantly and synthesise this with ``confirmed=True``
    and a simulated signature so the shape stays identical across modes.
    """

    confirmed: bool
    signature: str | None = None
    slot: int | None = None
    block_time: int | None = None
    attempts: int = 0
    elapsed_ms: int = 0
    rpc_url: str | None = None
    error: str | None = None


class FillReport(ValueObject):
    """The outcome of an executed order — identical shape for paper and live.

    The Portfolio Manager reads only this; it cannot tell which executor produced
    it. ``mode`` and ``confirmation`` are recorded for audit/telemetry, never for
    branching upstream.
    """

    token: TokenRef
    side: OrderSide
    status: OrderStatus
    filled_quantity: Decimal
    average_price: Money
    fees: Money
    mode: str = ExecutionMode.PAPER.value
    slippage_bps: int = 0
    latency_ms: int = 0
    notional: Money | None = None
    fee_breakdown: FeeBreakdown | None = None
    confirmation: ConfirmationResult | None = None
    signature: str | None = None  # on-chain tx signature (live) or sim id (paper)
    error: str | None = None
    #: Per-stage landing latency, when the executor that produced this fill
    #: measures it. Optional so the shape stays identical for executors that do
    #: not (``latency_ms`` remains the coarse, always-present figure).
    latency: LatencyBreakdown | None = None

    @property
    def filled(self) -> bool:
        return self.status is OrderStatus.FILLED
