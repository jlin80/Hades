"""The Position Monitor marks positions to market and closes them.

These tests pin the half of the trade lifecycle that did not exist: before the
monitor, nothing ever called ``mark`` and nothing ever issued a SELL, so a
position opened was a position held forever and equity never moved.
"""

from __future__ import annotations

from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.execution.application.engine import ExecutionEngine
from hades.contexts.execution.application.order_manager import OrderManager
from hades.contexts.execution.application.position_monitor import (
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TRAILING_STOP,
    PositionMonitor,
)
from hades.contexts.execution.application.transaction_manager import TransactionManager
from hades.contexts.execution.domain.models import (
    TAG_EXIT_REASON,
    TAG_STOP_LOSS_PCT,
    TAG_TAKE_PROFIT_PCT,
    TAG_TRAILING_ACTIVATION_PCT,
    TAG_TRAILING_DISTANCE_PCT,
    TAG_TRAILING_ENABLED,
    FillReport,
    OrderRequest,
    OrderSide,
    OrderStatus,
)
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.portfolio.domain.events import (
    PositionClosed,
    PositionOpened,
    PositionUpdated,
)
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus

_MINT = "So11111111111111111111111111111111111111112"


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


class _RecordingExecutor:
    """Fills whatever it is asked at the price the test dictates."""

    def __init__(self, price: Decimal = Decimal(1)) -> None:
        self.price = price
        self.calls: list[OrderRequest] = []

    @property
    def mode(self) -> str:
        return "paper"

    async def execute(self, request: OrderRequest) -> FillReport:
        self.calls.append(request)
        return FillReport(
            token=request.token,
            side=request.side,
            status=OrderStatus.FILLED,
            filled_quantity=Decimal(str(request.notional.amount)) / self.price,
            average_price=Money(amount=self.price),
            fees=Money(amount=Decimal("0.10")),
            mode="paper",
            slippage_bps=50,
            notional=request.notional,
        )


class _StubOracle:
    """A price oracle the test drives directly."""

    def __init__(self, price: Decimal | None) -> None:
        self.price = price
        self.calls = 0

    async def price_usd(self, token: TokenRef) -> Decimal | None:
        self.calls += 1
        return self.price


def _build(
    price: Decimal | None, *, fill_price: Decimal = Decimal(1)
) -> tuple[PositionMonitor, InMemoryEventBus, _RecordingExecutor]:
    bus = InMemoryEventBus()

    async def mode_provider() -> str:
        return "paper"

    executor = _RecordingExecutor(fill_price)
    engine = ExecutionEngine(
        executors={"paper": executor},
        mode_provider=mode_provider,
        order_manager=OrderManager(),
        transaction_manager=TransactionManager(),
        event_bus=bus,
        notifier=NotificationPublisher(bus),
    )
    monitor = PositionMonitor(
        engine=engine,
        price_oracle=_StubOracle(price),
        event_bus=bus,
        interval_seconds=0.5,
    )
    monitor.register(bus)
    return monitor, bus, executor


def _capture(bus: InMemoryEventBus, name: str) -> list[DomainEvent]:
    events: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        events.append(event)

    bus.subscribe(name, handler)
    return events


async def _open(
    bus: InMemoryEventBus,
    *,
    entry: Decimal = Decimal(1),
    quantity: Decimal = Decimal(100),
    take_profit: str = "40",
    stop_loss: str = "20",
    trailing: bool = False,
    activation: str = "25",
    distance: str = "10",
    position_id: str = "",
) -> None:
    await bus.publish(
        PositionOpened(
            aggregate_id=position_id or new_id(),
            token=_token(),
            entry_price=Money(amount=entry),
            quantity=quantity,
            notional=Money(amount=entry * quantity),
            tags={
                TAG_TAKE_PROFIT_PCT: take_profit,
                TAG_STOP_LOSS_PCT: stop_loss,
                TAG_TRAILING_ENABLED: "true" if trailing else "false",
                TAG_TRAILING_ACTIVATION_PCT: activation,
                TAG_TRAILING_DISTANCE_PCT: distance,
            },
        )
    )


async def test_a_flat_price_marks_the_position_without_exiting() -> None:
    monitor, bus, executor = _build(Decimal(1))
    marks = _capture(bus, PositionUpdated.__name__)
    await _open(bus)

    await monitor.tick()

    assert len(marks) == 1
    mark = marks[0]
    assert isinstance(mark, PositionUpdated)
    assert mark.mark_price.amount == Decimal(1)
    assert mark.unrealized_pnl.amount == Decimal(0)
    assert executor.calls == []  # nothing breached, so no exit


async def test_a_rising_price_reports_unrealized_profit() -> None:
    monitor, bus, _ = _build(Decimal("1.10"))
    marks = _capture(bus, PositionUpdated.__name__)
    await _open(bus, entry=Decimal(1), quantity=Decimal(100))

    await monitor.tick()

    mark = marks[0]
    assert isinstance(mark, PositionUpdated)
    # 100 units bought at 1.00, now worth 1.10 → +10.00 unrealised.
    assert mark.unrealized_pnl.amount == Decimal("10.00")


async def test_take_profit_issues_a_sell_for_the_whole_position() -> None:
    monitor, bus, executor = _build(Decimal("1.50"), fill_price=Decimal("1.50"))
    closed = _capture(bus, PositionClosed.__name__)
    await _open(bus, entry=Decimal(1), quantity=Decimal(100), take_profit="40")

    await monitor.tick()

    assert len(executor.calls) == 1
    sell = executor.calls[0]
    assert sell.side is OrderSide.SELL
    assert sell.tags[TAG_EXIT_REASON] == EXIT_TAKE_PROFIT
    # The whole position at the mark: 100 units x 1.50.
    assert sell.notional.amount == Decimal("150.00")
    assert len(closed) == 1


async def test_stop_loss_exits_the_position() -> None:
    monitor, bus, executor = _build(Decimal("0.75"), fill_price=Decimal("0.75"))
    await _open(bus, entry=Decimal(1), quantity=Decimal(100), stop_loss="20")

    await monitor.tick()

    assert len(executor.calls) == 1
    assert executor.calls[0].tags[TAG_EXIT_REASON] == EXIT_STOP_LOSS


async def test_stop_loss_wins_when_a_tick_spans_both_levels() -> None:
    """A single tick through both levels must assume the worse outcome."""
    monitor, bus, executor = _build(Decimal("0.50"), fill_price=Decimal("0.50"))
    # A 1% take-profit and a 20% stop are both breached by a price of 0.50.
    await _open(bus, entry=Decimal(1), quantity=Decimal(100), take_profit="1", stop_loss="20")

    await monitor.tick()

    assert executor.calls[0].tags[TAG_EXIT_REASON] == EXIT_STOP_LOSS


async def test_trailing_stop_arms_then_fires_on_the_pullback() -> None:
    monitor, bus, executor = _build(Decimal("1.30"), fill_price=Decimal("1.30"))
    # Take-profit far away so only the trailing stop can fire.
    await _open(
        bus,
        entry=Decimal(1),
        quantity=Decimal(100),
        take_profit="500",
        stop_loss="90",
        trailing=True,
        activation="25",
        distance="10",
    )

    # 1.30 arms the trail (>= +25%) and sets the peak; no exit yet.
    await monitor.tick()
    assert executor.calls == []

    # Pull back to 1.15: below peak 1.30 less 10% = 1.17 → trailing stop fires.
    monitor._oracle.price = Decimal("1.15")  # type: ignore[attr-defined]
    await monitor.tick()

    assert len(executor.calls) == 1
    assert executor.calls[0].tags[TAG_EXIT_REASON] == EXIT_TRAILING_STOP


async def test_an_unarmed_trailing_stop_never_fires() -> None:
    """Below the activation threshold the trail must not exist yet."""
    monitor, bus, executor = _build(Decimal("1.05"), fill_price=Decimal("1.05"))
    await _open(
        bus,
        entry=Decimal(1),
        quantity=Decimal(100),
        take_profit="500",
        stop_loss="90",
        trailing=True,
        activation="25",
        distance="1",
    )

    await monitor.tick()
    monitor._oracle.price = Decimal("1.00")  # type: ignore[attr-defined]
    await monitor.tick()

    assert executor.calls == []


async def test_a_position_is_only_sold_once() -> None:
    monitor, bus, executor = _build(Decimal("1.50"), fill_price=Decimal("1.50"))
    await _open(bus, entry=Decimal(1), quantity=Decimal(100), take_profit="40")

    await monitor.tick()
    await monitor.tick()
    await monitor.tick()

    assert len(executor.calls) == 1


async def test_closing_a_position_stops_monitoring_it() -> None:
    monitor, bus, _ = _build(Decimal(1))
    position_id = new_id()
    await _open(bus, position_id=position_id)
    assert monitor.tracked == 1

    await bus.publish(
        PositionClosed(
            aggregate_id=position_id,
            exit_price=Money(amount=Decimal(1)),
            realized_pnl=Money(amount=Decimal(0)),
            reason="manual",
        )
    )

    assert monitor.tracked == 0


async def test_an_unknown_price_leaves_the_position_untouched() -> None:
    """No price is not a reason to invent a mark or panic-sell."""
    monitor, bus, executor = _build(None)
    marks = _capture(bus, PositionUpdated.__name__)
    await _open(bus)

    await monitor.tick()

    assert marks == []
    assert executor.calls == []
    assert monitor.tracked == 1


async def test_a_position_without_an_envelope_is_never_exited() -> None:
    """Zero percentages mean "no level set" — not "exit immediately"."""
    monitor, bus, executor = _build(Decimal("0.01"))
    await _open(bus, take_profit="0", stop_loss="0")

    await monitor.tick()

    assert executor.calls == []


async def test_a_zero_entry_price_is_refused_rather_than_dividing_by_it() -> None:
    monitor, bus, _ = _build(Decimal(1))
    await _open(bus, entry=Decimal(0))

    assert monitor.tracked == 0
