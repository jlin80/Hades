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
from hades.contexts.strategy.domain.events import EnsembleSignalGenerated
from hades.contexts.strategy.domain.models import SignalType
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
        honour_strategy_exits: bool = False,
    ) -> None:
        self._engine = engine
        self._oracle = price_oracle
        self._bus = event_bus
        self._interval = max(0.5, interval_seconds)
        self._max_slippage_bps = max_slippage_bps
        # When the Strategy Engine gates risk, its SELL/EXIT verdicts are also
        # honoured here. Off by default: the same flag turns both directions on,
        # so an operator enabling the Strategy Engine gets one switch, not two.
        self._honour_strategy_exits = honour_strategy_exits
        self._positions: dict[str, _Monitored] = {}
        self._requested_exits: dict[str, str] = {}
        self._stop = asyncio.Event()

    @property
    def tracked(self) -> int:
        return len(self._positions)

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(PositionOpened.__name__, self._on_opened)
        event_bus.subscribe(PositionClosed.__name__, self._on_closed)
        if self._honour_strategy_exits:
            event_bus.subscribe(EnsembleSignalGenerated.__name__, self._on_ensemble)

    async def _on_ensemble(self, event: DomainEvent) -> None:
        """A strategy SELL/EXIT on a token we hold becomes an exit request.

        These verdicts used to die in the ensemble: the Strategy Engine could
        emit them and no subscriber existed, so the only way out of a position
        was the TP/SL envelope approved at entry.

        The request is *recorded*, not acted on here. It is honoured on the next
        tick, at a price the oracle actually returned, through the same
        ``_exit`` path as every other exit — so a strategy cannot cause a sale at
        a price nobody quoted, and the latch that prevents double-selling still
        applies. And it can only ever close a position: there is no path from
        this handler to opening one.
        """
        if not isinstance(event, EnsembleSignalGenerated):
            return
        decision = event.ensemble.decision
        if decision not in (SignalType.SELL, SignalType.EXIT):
            return
        mint = str(event.token.mint)
        for position in self._positions.values():
            if str(position.token.mint) == mint and not position.exiting:
                self._requested_exits[position.position_id] = f"strategy_{decision.value}"
                _logger.info(
                    "strategy_exit_requested",
                    position_id=position.position_id,
                    mint=mint,
                    decision=decision.value,
                    score=round(float(event.ensemble.score), 4),
                )

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

    def resume(
        self,
        *,
        position_id: str,
        token: TokenRef,
        entry_price: Decimal,
        quantity: Decimal,
        tags: dict[str, str],
        peak_price: Decimal | None = None,
    ) -> bool:
        """Take a position opened before this process started back under watch.

        The monitor learns about positions only from live ``PositionOpened``
        events, but the portfolio book is persisted and rehydrates on startup.
        Without this, a position that survived a restart was owned by the book
        and watched by nobody: never marked to market, and unreachable by the
        take-profit or stop-loss approved for it. It could only ever be closed
        by hand.

        ``peak_price`` restores the trailing high-water mark where it is known;
        falling back to the entry price is deliberately conservative, since it
        can only place the trailing stop lower than the true peak would.
        """
        if entry_price <= 0 or quantity <= 0:
            _logger.warning(
                "position_not_resumed",
                position_id=position_id,
                reason="non-positive entry price or quantity",
            )
            return False
        if position_id in self._positions:
            return False
        peak = peak_price if peak_price is not None and peak_price > entry_price else entry_price
        trailing_enabled = tags.get(TAG_TRAILING_ENABLED, "false") == "true"
        activation = _as_float(tags.get(TAG_TRAILING_ACTIVATION_PCT), 0.0)
        monitored = _Monitored(
            position_id=position_id,
            token=token,
            entry_price=entry_price,
            quantity=quantity,
            take_profit_pct=_as_float(tags.get(TAG_TAKE_PROFIT_PCT), 0.0),
            stop_loss_pct=_as_float(tags.get(TAG_STOP_LOSS_PCT), 0.0),
            trailing_enabled=trailing_enabled,
            trailing_activation_pct=activation,
            trailing_distance_pct=_as_float(tags.get(TAG_TRAILING_DISTANCE_PCT), 0.0),
            peak_price=peak,
        )
        # Re-arm the trailing stop if the recovered peak already cleared the
        # activation level, so a restart cannot silently disarm it.
        monitored.trailing_armed = trailing_enabled and peak >= monitored.trailing_arm_price()
        self._positions[position_id] = monitored
        _logger.info(
            "position_resumed",
            position_id=position_id,
            mint=str(token.mint),
            symbol=token.symbol or str(token.mint)[:8],
            entry_price=float(entry_price),
            take_profit_pct=monitored.take_profit_pct,
            stop_loss_pct=monitored.stop_loss_pct,
            trailing_armed=monitored.trailing_armed,
        )
        return True

    async def _on_closed(self, event: DomainEvent) -> None:
        self._requested_exits.pop(str(event.aggregate_id), None)
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
            # Silence here is indistinguishable from a quiet market: the book
            # simply stops being marked, unrealised PnL freezes at its last
            # value, and no stop-loss can fire — while every component still
            # reports itself healthy. An oracle that cannot price the book is an
            # outage in the exit path and has to say so.
            _logger.error(
                "position_book_unpriced",
                positions=len(open_positions),
                note="price oracle returned nothing — marks and exits are stalled",
            )
            return
        unpriced = [p for p in open_positions if str(p.token.mint) not in prices]
        if unpriced:
            _logger.warning(
                "positions_unpriced",
                count=len(unpriced),
                mints=[str(p.token.mint) for p in unpriced[:10]],
            )

        for position in open_positions:
            price = prices.get(str(position.token.mint))
            if price is None or price <= 0:
                continue
            try:
                await self._mark(position, price)
                # The approved envelope is checked first. A strategy asking to
                # leave must never override a stop-loss that has already been
                # crossed: the stop is the loss the Risk Manager sized the trade
                # around, and it should be the reason of record for the exit.
                reason = self._breached(position, price)
                if reason is None:
                    reason = self._requested_exits.pop(position.position_id, None)
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
