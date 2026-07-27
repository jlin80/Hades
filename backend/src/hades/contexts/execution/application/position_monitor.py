"""Position Monitor — marks open positions to market and closes them.

This is the missing half of the trade lifecycle. The pipeline could open a
position and had no way to ever close one: nothing marked a position to market
(so unrealised PnL stayed 0 and equity never moved) and nothing ever issued a
SELL (so no position could reach ``PositionClosed``). Both live here.

    PositionOpened ─► register ─┐
                                │  every ``interval`` seconds:
                                ├─► price the book (batched)
                                ├─► publish PositionUpdated  (mark-to-market)
                                └─► envelope breached? ─► SELL ─► PositionClosed

**It decides nothing.** The exit envelope — take-profit, stop-loss, trailing —
was approved by the Risk Manager at entry and travelled here on the position's
tags. The monitor only detects that a level the guardian already set has been
crossed and executes the exit it already authorised. That keeps the invariant
intact: the Risk Manager remains the only component that authorises *opening*
risk, and closing risk needs no authorisation because it only ever reduces it.

An exit is therefore never blocked by the defence layer. A kill switch or an
open circuit breaker must not trap the platform in a losing position — those
gates withhold *entries*, and stopping an exit would be the one way a brake
could destroy capital instead of preserving it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenRef
from hades.contexts.execution.application.engine import ExecutionEngine
from hades.contexts.execution.domain.models import (
    TAG_EXIT_REASON,
    TAG_STOP_LOSS_PCT,
    TAG_TAKE_PROFIT_PCT,
    TAG_TRAILING_ACTIVATION_PCT,
    TAG_TRAILING_DISTANCE_PCT,
    TAG_TRAILING_ENABLED,
    OrderRequest,
    OrderSide,
)
from hades.contexts.execution.domain.ports import PriceOracle
from hades.contexts.portfolio.domain.events import (
    PositionClosed,
    PositionOpened,
    PositionUpdated,
    TrailingStopAdjusted,
)
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.position_monitor")

#: Exit reasons, recorded on ``PositionClosed`` and the trade log.
EXIT_TAKE_PROFIT = "take_profit"
EXIT_STOP_LOSS = "stop_loss"
EXIT_TRAILING_STOP = "trailing_stop"


@dataclass
class _Monitored:
    """One open position and the exit envelope approved with it."""

    position_id: str
    token: TokenRef
    entry_price: Decimal
    quantity: Decimal
    take_profit_pct: float
    stop_loss_pct: float
    trailing_enabled: bool
    trailing_activation_pct: float
    trailing_distance_pct: float
    peak_price: Decimal
    trailing_armed: bool = False
    #: Set once an exit order is in flight so a slow fill cannot be double-sold.
    exiting: bool = False
    opened_at: float = field(default_factory=time.monotonic)

    def take_profit_price(self) -> Decimal:
        return self.entry_price * (Decimal(1) + _pct(self.take_profit_pct))

    def stop_loss_price(self) -> Decimal:
        return self.entry_price * (Decimal(1) - _pct(self.stop_loss_pct))

    def trailing_arm_price(self) -> Decimal:
        return self.entry_price * (Decimal(1) + _pct(self.trailing_activation_pct))

    def trailing_stop_price(self) -> Decimal:
        return self.peak_price * (Decimal(1) - _pct(self.trailing_distance_pct))


class PositionMonitor:
    """Marks positions to market and exits them when their envelope is breached."""

    def __init__(
        self,
        *,
        engine: ExecutionEngine,
        price_oracle: PriceOracle,
        event_bus: EventBus,
        interval_seconds: float = 5.0,
        max_slippage_bps: int = 300,
    ) -> None:
        self._engine = engine
        self._oracle = price_oracle
        self._bus = event_bus
        self._interval = max(0.5, interval_seconds)
        self._max_slippage_bps = max_slippage_bps
        self._positions: dict[str, _Monitored] = {}
        self._stop = asyncio.Event()

    @property
    def tracked(self) -> int:
        return len(self._positions)

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(PositionOpened.__name__, self._on_opened)
        event_bus.subscribe(PositionClosed.__name__, self._on_closed)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        return [asyncio.create_task(self._run(), name="position-monitor")]

    async def stop(self) -> None:
        self._stop.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # the monitor must outlive any one cycle
                _logger.warning("position_monitor_cycle_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    # -- subscriptions --------------------------------------------------------

    async def _on_opened(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionOpened):
            return
        entry = Decimal(str(event.entry_price.amount))
        if entry <= 0 or event.quantity <= 0:
            _logger.warning(
                "position_not_monitored",
                position_id=str(event.aggregate_id),
                reason="non-positive entry price or quantity",
            )
            return
        tags = event.tags
        self._positions[str(event.aggregate_id)] = _Monitored(
            position_id=str(event.aggregate_id),
            token=event.token,
            entry_price=entry,
            quantity=Decimal(str(event.quantity)),
            take_profit_pct=_as_float(tags.get(TAG_TAKE_PROFIT_PCT), 0.0),
            stop_loss_pct=_as_float(tags.get(TAG_STOP_LOSS_PCT), 0.0),
            trailing_enabled=tags.get(TAG_TRAILING_ENABLED, "false") == "true",
            trailing_activation_pct=_as_float(tags.get(TAG_TRAILING_ACTIVATION_PCT), 0.0),
            trailing_distance_pct=_as_float(tags.get(TAG_TRAILING_DISTANCE_PCT), 0.0),
            peak_price=entry,
        )

    async def _on_closed(self, event: DomainEvent) -> None:
        if not isinstance(event, PositionClosed):
            return
        self._positions.pop(str(event.aggregate_id), None)

    # -- the cycle ------------------------------------------------------------

    async def tick(self) -> None:
        """Price the book once: mark every position, then exit those breached."""
        open_positions = [p for p in self._positions.values() if not p.exiting]
        if not open_positions:
            return

        prices = await self._prices({p.token for p in open_positions})
        if not prices:
            return

        for position in open_positions:
            price = prices.get(str(position.token.mint))
            if price is None or price <= 0:
                continue
            try:
                await self._mark(position, price)
                reason = self._breached(position, price)
                if reason is not None:
                    await self._exit(position, price, reason)
            except Exception as exc:  # one token must never stall the others
                _logger.warning(
                    "position_mark_failed",
                    position_id=position.position_id,
                    mint=str(position.token.mint),
                    error=str(exc),
                )

    async def _prices(self, tokens: set[TokenRef]) -> dict[str, Decimal]:
        batch = getattr(self._oracle, "prices_usd", None)
        if callable(batch):
            result: dict[str, Decimal] = await batch(list(tokens))
            return result
        # A single-price oracle still works, just one request per position.
        out: dict[str, Decimal] = {}
        for token in tokens:
            price = await self._oracle.price_usd(token)
            if price is not None:
                out[str(token.mint)] = price
        return out

    async def _mark(self, position: _Monitored, price: Decimal) -> None:
        if price > position.peak_price:
            position.peak_price = price
            if (
                position.trailing_enabled
                and not position.trailing_armed
                and price >= position.trailing_arm_price()
            ):
                position.trailing_armed = True
            if position.trailing_armed:
                await self._bus.publish(
                    TrailingStopAdjusted(
                        aggregate_id=position.position_id,
                        new_stop_price=Money(amount=position.trailing_stop_price()),
                    )
                )
        unrealized = (price - position.entry_price) * position.quantity
        await self._bus.publish(
            PositionUpdated(
                aggregate_id=position.position_id,
                mark_price=Money(amount=price),
                unrealized_pnl=Money(amount=unrealized),
            )
        )

    @staticmethod
    def _breached(position: _Monitored, price: Decimal) -> str | None:
        # Stop-loss is evaluated before take-profit: when a single volatile tick
        # spans both levels, assuming the worse outcome is the honest simulation.
        if position.stop_loss_pct > 0 and price <= position.stop_loss_price():
            return EXIT_STOP_LOSS
        if (
            position.trailing_armed
            and position.trailing_distance_pct > 0
            and price <= position.trailing_stop_price()
        ):
            return EXIT_TRAILING_STOP
        if position.take_profit_pct > 0 and price >= position.take_profit_price():
            return EXIT_TAKE_PROFIT
        return None

    async def _exit(self, position: _Monitored, price: Decimal, reason: str) -> None:
        position.exiting = True
        notional = position.quantity * price
        _logger.info(
            "position_exit_triggered",
            position_id=position.position_id,
            mint=str(position.token.mint),
            symbol=position.token.symbol or str(position.token.mint)[:8],
            reason=reason,
            entry_price=float(position.entry_price),
            mark_price=float(price),
            notional_usd=float(notional),
        )
        request = OrderRequest(
            token=position.token,
            side=OrderSide.SELL,
            notional=Money(amount=notional),
            max_slippage_bps=self._max_slippage_bps,
            tags={TAG_EXIT_REASON: reason},
        )
        try:
            await self._engine.execute(request)
        except Exception as exc:
            # The exit failed to execute; un-latch so the next cycle retries
            # rather than stranding the position with nothing watching it.
            position.exiting = False
            _logger.error(
                "position_exit_failed",
                position_id=position.position_id,
                mint=str(position.token.mint),
                reason=reason,
                error=str(exc),
            )


def _pct(value: float) -> Decimal:
    return Decimal(str(value)) / Decimal(100)


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
