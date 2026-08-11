"""Execution Engine — orchestrates the whole execution flow, mode-agnostic.

It is the single component in the platform that knows whether an order is paper
or live, and it confines that knowledge to one line: :meth:`_executor_for`
resolves the active mode (via the injected mode provider) and picks the matching
:class:`Executor`. Everything else — building the order, tracking its lifecycle,
recording the transaction, emitting the result and opening/closing the position —
is identical across modes.

Flow (never broken):

    TradeApproved → build order → OrderManager.create → execute (paper|live)
        → TransactionManager.record → OrderFilled/OrderFailed
        → PositionOpened / PositionClosed (Portfolio reacts) → Discord

A ``TradeApproved`` is a *permission* to trade; this engine is what turns it into
a real (or simulated) order. It never decides *whether* to trade — the Risk
Manager already did.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenRef
from hades.contexts.execution.application.metrics import ExecutionMetrics
from hades.contexts.execution.application.order_manager import OrderManager
from hades.contexts.execution.application.transaction_manager import TransactionManager
from hades.contexts.execution.domain.events import (
    OrderFailed,
    OrderFilled,
    OrderSubmitted,
)
from hades.contexts.execution.domain.models import (
    TAG_EXIT_REASON,
    TAG_STOP_LOSS_PCT,
    TAG_TAKE_PROFIT_PCT,
    TAG_TRAILING_ACTIVATION_PCT,
    TAG_TRAILING_DISTANCE_PCT,
    TAG_TRAILING_ENABLED,
    ExecutionMode,
    FillReport,
    OrderRequest,
    OrderSide,
    OrderState,
)
from hades.contexts.execution.domain.ports import Executor
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.portfolio.domain.events import PositionClosed, PositionOpened
from hades.contexts.risk.domain.events import TradeApproved
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.engine")

#: Async callable returning the effective mode ("paper" | "live").
ModeProvider = Callable[[], Awaitable[str]]


@dataclass
class _OpenPosition:
    position_id: str
    entry_notional_usd: float
    # Buy-side fee paid to open. Realized PnL on close must net BOTH round-trip
    # frictions (entry + exit fee), not just the sell-side one.
    entry_fees_usd: float


class ExecutionEngine:
    """Turns approved trades into executed orders and position events."""

    def __init__(
        self,
        *,
        executors: dict[str, Executor],
        mode_provider: ModeProvider,
        order_manager: OrderManager,
        transaction_manager: TransactionManager,
        event_bus: EventBus,
        notifier: NotificationPublisher,
        metrics: ExecutionMetrics | None = None,
        default_max_slippage_bps: int = 300,
    ) -> None:
        if ExecutionMode.PAPER.value not in executors:
            raise ValueError("a paper executor is mandatory (the safe default)")
        self._executors = executors
        self._mode_provider = mode_provider
        self._orders = order_manager
        self._txns = transaction_manager
        self._bus = event_bus
        self._notifier = notifier
        self._metrics = metrics
        self._default_max_slippage_bps = default_max_slippage_bps
        # token mint → the open position we opened, so a SELL closes the same id.
        self._open: dict[str, _OpenPosition] = {}

    @property
    def orders(self) -> OrderManager:
        return self._orders

    @property
    def transactions(self) -> TransactionManager:
        return self._txns

    async def on_trade_approved(self, event: TradeApproved) -> None:
        """React to a Risk-Manager approval by executing the sized order."""
        request = self._build_request(event)
        await self.execute(request)

    async def execute(self, request: OrderRequest) -> FillReport:
        mode = await self._resolve_mode()
        executor = self._executor_for(mode)
        record = self._orders.create(request, mode=mode)
        self._orders.transition(record, OrderState.SUBMITTED)
        if self._metrics:
            self._metrics.submitted.labels(mode=mode).inc()
        await self._bus.publish(OrderSubmitted(aggregate_id=new_id(), request=request, mode=mode))

        started = time.monotonic()
        fill = await executor.execute(request)
        if self._metrics:
            self._metrics.execute_seconds.observe(time.monotonic() - started)

        await self._orders.complete(record, fill)
        await self._txns.record(order_id=record.order_id, fill=fill)

        if fill.filled:
            await self._on_filled(request, fill, mode)
        else:
            await self._on_failed(request, fill, mode)
        return fill

    # -- outcome handling -----------------------------------------------------

    async def _on_filled(self, request: OrderRequest, fill: FillReport, mode: str) -> None:
        if self._metrics:
            self._metrics.filled.labels(mode=mode).inc()
            self._metrics.slippage_bps.observe(fill.slippage_bps)
            self._metrics.fees_usd.observe(float(fill.fees.amount))
            if fill.confirmation is not None:
                self._metrics.confirmation_seconds.observe(fill.confirmation.elapsed_ms / 1000.0)
        await self._bus.publish(OrderFilled(aggregate_id=new_id(), fill=fill))
        # A structured line per fill: this is what makes the dashboard terminal
        # answer "which token did it trade, for how much, and at what price".
        # Metrics aggregate and events are internal — an operator watching the
        # stream needs the individual trade, legibly.
        _logger.info(
            "trade_filled",
            side=request.side.value,
            mint=str(request.token.mint),
            symbol=_symbol(request.token),
            notional_usd=float((fill.notional or request.notional).amount),
            price=float(fill.average_price.amount),
            quantity=float(fill.filled_quantity),
            slippage_bps=fill.slippage_bps,
            fees_usd=float(fill.fees.amount),
            mode=mode,
        )
        if request.side is OrderSide.BUY:
            await self._open_position(request, fill)
        else:
            await self._close_position(request, fill)
        await self._notify_fill(request, fill, mode)

    async def _on_failed(self, request: OrderRequest, fill: FillReport, mode: str) -> None:
        reason = fill.error or "unknown"
        if self._metrics:
            self._metrics.failed.labels(mode=mode, reason=_reason_label(reason)).inc()
        await self._bus.publish(OrderFailed(aggregate_id=new_id(), request=request, reason=reason))
        await self._notifier.notify(
            title=f"Order failed · {_symbol(request.token)}",
            body=f"{request.side.value.upper()} {request.notional.amount} USD failed: {reason}",
            severity=Severity.WARNING,
            tags={"topic": "trades", "mode": mode, "mint": str(request.token.mint)},
        )

    async def _open_position(self, request: OrderRequest, fill: FillReport) -> None:
        position_id = str(new_id())
        notional = fill.notional or request.notional
        self._open[str(request.token.mint)] = _OpenPosition(
            position_id=position_id,
            entry_notional_usd=float(notional.amount),
            entry_fees_usd=float(fill.fees.amount),
        )
        await self._bus.publish(
            PositionOpened(
                aggregate_id=position_id,
                token=request.token,
                entry_price=fill.average_price,
                quantity=fill.filled_quantity,
                notional=notional,
                tags=request.tags,
            )
        )

    async def _close_position(self, request: OrderRequest, fill: FillReport) -> None:
        # NOTE: single-position-per-mint model — a SELL fully closes the position
        # the matching BUY opened. Partial exits are not supported; if they are
        # ever added, entry accounting must track remaining quantity/cost basis.
        mint = str(request.token.mint)
        open_pos = self._open.pop(mint, None)
        exit_notional = float((fill.notional or request.notional).amount)
        exit_fees = float(fill.fees.amount)

        if open_pos is None:
            # ``self._open`` is process state: a restart empties it while the
            # Portfolio Manager restores its positions from the snapshot, so from
            # then on the engine cannot name the position it is closing. The old
            # code minted a fresh id here and treated the exit notional as the
            # entry. Both are fabrications, and together they were the whole
            # defect: the book looked the id up, missed, and applied a fictitious
            # PnL to a position it never removed. Measured in production, 2,420
            # closes took this path and *none* of them were attributable.
            #
            # So say so, and hand the book the two facts that are real — which
            # token, and what the exit was actually worth. The Portfolio Manager
            # holds the entry and is the book of record; it can finish the sum.
            _logger.error(
                "position_close_unattributed",
                mint=mint,
                symbol=_symbol(request.token),
                exit_usd=round(exit_notional, 4),
                exit_fees_usd=round(exit_fees, 4),
                reason=request.tags.get(TAG_EXIT_REASON, "manual"),
                note="entry unknown to this process; the book reconciles by token",
            )
            await self._bus.publish(
                PositionClosed(
                    aggregate_id=str(new_id()),
                    token=request.token,
                    exit_price=fill.average_price,
                    realized_pnl=Money(amount=Decimal(str(-exit_fees))),
                    exit_notional=Money(amount=Decimal(str(exit_notional))),
                    reason=request.tags.get(TAG_EXIT_REASON, "manual"),
                )
            )
            return

        position_id = open_pos.position_id
        entry_notional = open_pos.entry_notional_usd
        entry_fees = open_pos.entry_fees_usd
        # Realized PnL is net of BOTH round-trip frictions (buy fee + sell fee).
        realized = exit_notional - entry_notional - entry_fees - exit_fees
        # The closing line carries the number the operator actually cares about:
        # realized PnL, net of both round-trip frictions, plus the ROI it implies.
        roi_pct = (realized / entry_notional * 100.0) if entry_notional else 0.0
        _logger.info(
            "position_closed",
            mint=mint,
            symbol=_symbol(request.token),
            entry_usd=round(entry_notional, 4),
            exit_usd=round(exit_notional, 4),
            fees_usd=round(entry_fees + exit_fees, 4),
            realized_pnl_usd=round(realized, 4),
            roi_pct=round(roi_pct, 2),
            reason=request.tags.get(TAG_EXIT_REASON, "manual"),
        )
        await self._bus.publish(
            PositionClosed(
                aggregate_id=position_id,
                token=request.token,
                exit_price=fill.average_price,
                realized_pnl=Money(amount=Decimal(str(realized))),
                reason=request.tags.get(TAG_EXIT_REASON, "manual"),
            )
        )

    async def _notify_fill(self, request: OrderRequest, fill: FillReport, mode: str) -> None:
        is_live_sig = mode == ExecutionMode.LIVE.value and fill.signature
        sig = f" · sig {fill.signature}" if is_live_sig else ""
        await self._notifier.notify(
            title=f"{request.side.value.upper()} filled · {_symbol(request.token)}",
            body=(
                f"{fill.notional.amount if fill.notional else request.notional.amount} USD "
                f"@ {fill.average_price.amount} · slip {fill.slippage_bps}bps · "
                f"fee ${fill.fees.amount}{sig}"
            ),
            severity=Severity.INFO,
            tags={
                "topic": "trades",
                "mode": mode,
                "mint": str(request.token.mint),
                "strategy": request.strategy,
            },
        )

    # -- helpers --------------------------------------------------------------

    async def _resolve_mode(self) -> str:
        try:
            mode = await self._mode_provider()
        except Exception as exc:  # never let a mode-read failure route to live
            _logger.warning("mode_resolve_failed", error=str(exc))
            return ExecutionMode.PAPER.value
        if mode not in self._executors:
            _logger.warning("mode_no_executor", mode=mode)
            return ExecutionMode.PAPER.value
        return mode

    def _executor_for(self, mode: str) -> Executor:
        return self._executors.get(mode, self._executors[ExecutionMode.PAPER.value])

    def _build_request(self, event: TradeApproved) -> OrderRequest:
        sizing = event.sizing
        tags = {
            "strategy": event.strategy,
            "regime": event.regime,
            # The approved exit envelope rides along to the position, which is
            # what lets the Position Monitor close this trade later without
            # re-deriving (or re-deciding) anything the Risk Manager settled.
            TAG_TAKE_PROFIT_PCT: str(sizing.take_profit.value),
            TAG_STOP_LOSS_PCT: str(sizing.stop_loss.value),
            TAG_TRAILING_ENABLED: "true" if sizing.trailing_enabled else "false",
            TAG_TRAILING_ACTIVATION_PCT: str(sizing.trailing_activation.value),
            TAG_TRAILING_DISTANCE_PCT: str(sizing.trailing_distance.value),
        }
        if event.developer:
            tags["developer"] = event.developer
        if event.cluster:
            tags["cluster"] = event.cluster
        if event.narrative:
            tags["narrative"] = event.narrative
        return OrderRequest(
            token=event.token,
            side=OrderSide.BUY,
            notional=event.sizing.notional,
            max_slippage_bps=self._default_max_slippage_bps,
            strategy=event.strategy,
            correlation_id=event.correlation_id_ref,
            tags=tags,
        )


def _symbol(token: TokenRef) -> str:
    return token.symbol or str(token.mint)[:8]


def _reason_label(reason: str) -> str:
    lowered = reason.lower()
    if "slippage" in lowered or "impact" in lowered:
        return "slippage"
    if "confirm" in lowered or "timeout" in lowered:
        return "confirmation"
    if "send" in lowered:
        return "send"
    return "other"
