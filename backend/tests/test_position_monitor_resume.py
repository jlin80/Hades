"""A position must not survive a restart into a state where nobody watches it.

The Portfolio Manager was made durable (0008) but the Position Monitor was not,
and the monitor learns about positions only from live ``PositionOpened`` events.
So after a worker restart the book held a position that the monitor had never
heard of: never marked to market, unrealised PnL frozen at exactly zero, and
unreachable by the take-profit and stop-loss the Risk Manager approved at entry.
The position could only ever be closed by hand.

These cover the round trip — the entry facts reach storage, and a fresh monitor
rebuilt from that storage marks and exits the position normally.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.execution.application.position_monitor import (
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    PositionMonitor,
)
from hades.contexts.execution.domain.models import (
    TAG_STOP_LOSS_PCT,
    TAG_TAKE_PROFIT_PCT,
    TAG_TRAILING_ACTIVATION_PCT,
    TAG_TRAILING_DISTANCE_PCT,
    TAG_TRAILING_ENABLED,
)
from hades.contexts.portfolio.application.portfolio_manager import PortfolioManager
from hades.contexts.portfolio.domain.events import PositionOpened, PositionUpdated
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus

_MINT = "D" * 44
# Position ids are UUIDs on the wire — the events validate them.
_POSITION_ID = str(new_id())
_OTHER_ID = str(new_id())


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="TKN")


def _envelope() -> dict[str, str]:
    return {
        TAG_TAKE_PROFIT_PCT: "50",
        TAG_STOP_LOSS_PCT: "20",
        TAG_TRAILING_ENABLED: "false",
        TAG_TRAILING_ACTIVATION_PCT: "0",
        TAG_TRAILING_DISTANCE_PCT: "0",
        "strategy": "momentum",
    }


class _Oracle:
    """A price oracle pinned to one price."""

    def __init__(self, price: Decimal) -> None:
        self.price = price

    async def price_usd(self, token: TokenRef) -> Decimal | None:
        return self.price

    async def prices_usd(self, tokens: list[TokenRef]) -> dict[str, Decimal]:
        return {str(t.mint): self.price for t in tokens}


class _Engine:
    """Captures the exit order instead of executing one."""

    def __init__(self) -> None:
        self.orders: list[object] = []

    async def execute(self, request: object) -> None:
        self.orders.append(request)


def _open_event() -> PositionOpened:
    return PositionOpened(
        aggregate_id=_POSITION_ID,
        token=_token(),
        entry_price=Money(amount=Decimal("2.00")),
        quantity=Decimal("25"),
        notional=Money(amount=Decimal("50")),
        tags=_envelope(),
    )


# -- the entry facts must reach storage ---------------------------------------


def test_the_snapshot_carries_what_the_monitor_needs_to_resume() -> None:
    manager = PortfolioManager(starting_balance_usd=1000.0)

    asyncio.run(manager._on_opened(_open_event()))
    snapshot = manager.snapshot()

    persisted = snapshot.positions[_POSITION_ID]
    # notional alone is enough for the book's accounting, and useless to a
    # monitor that must compare a live price against an entry price.
    assert persisted.entry_price == Decimal("2.00")
    assert persisted.quantity == Decimal("25")
    assert persisted.tags[TAG_TAKE_PROFIT_PCT] == "50"
    assert persisted.is_monitorable


def test_restore_round_trips_the_entry_facts() -> None:
    manager = PortfolioManager(starting_balance_usd=1000.0)
    asyncio.run(manager._on_opened(_open_event()))

    revived = PortfolioManager(starting_balance_usd=1000.0)
    revived.restore(manager.snapshot())

    assert revived.snapshot().positions[_POSITION_ID].entry_price == Decimal("2.00")


def test_a_snapshot_without_entry_facts_is_reported_not_silently_stranded() -> None:
    """Snapshots written before this fix cannot be resumed — and must say so."""
    manager = PortfolioManager(starting_balance_usd=1000.0)
    asyncio.run(manager._on_opened(_open_event()))
    snapshot = manager.snapshot()
    legacy = snapshot.positions[_POSITION_ID].model_copy(
        update={"entry_price": None, "quantity": None}
    )

    assert not legacy.is_monitorable


# -- a rebuilt monitor behaves like one that never restarted -------------------


def _monitor(price: str) -> tuple[PositionMonitor, _Engine, InMemoryEventBus]:
    bus = InMemoryEventBus()
    engine = _Engine()
    monitor = PositionMonitor(
        engine=engine,  # type: ignore[arg-type]
        price_oracle=_Oracle(Decimal(price)),  # type: ignore[arg-type]
        event_bus=bus,
    )
    return monitor, engine, bus


def test_a_resumed_position_is_marked_to_market() -> None:
    monitor, _engine, bus = _monitor("2.40")
    marks: list[PositionUpdated] = []
    bus.subscribe(PositionUpdated.__name__, lambda e: marks.append(e))  # type: ignore[arg-type,misc]

    assert monitor.resume(
        position_id=_POSITION_ID,
        token=_token(),
        entry_price=Decimal("2.00"),
        quantity=Decimal("25"),
        tags=_envelope(),
    )
    asyncio.run(monitor.tick())

    # This is the frozen-PnL symptom: without resume, tick() has nothing to mark.
    assert len(marks) == 1
    assert marks[0].unrealized_pnl.amount == Decimal("10.00")  # (2.40-2.00)*25


def test_a_resumed_position_still_takes_profit() -> None:
    monitor, engine, _bus = _monitor("3.10")  # entry 2.00, TP at +50% = 3.00

    monitor.resume(
        position_id=_POSITION_ID,
        token=_token(),
        entry_price=Decimal("2.00"),
        quantity=Decimal("25"),
        tags=_envelope(),
    )
    asyncio.run(monitor.tick())

    assert len(engine.orders) == 1
    assert engine.orders[0].tags["exit_reason"] == EXIT_TAKE_PROFIT  # type: ignore[attr-defined]


def test_a_resumed_position_still_stops_out() -> None:
    monitor, engine, _bus = _monitor("1.50")  # entry 2.00, SL at -20% = 1.60

    monitor.resume(
        position_id=_POSITION_ID,
        token=_token(),
        entry_price=Decimal("2.00"),
        quantity=Decimal("25"),
        tags=_envelope(),
    )
    asyncio.run(monitor.tick())

    assert len(engine.orders) == 1
    assert engine.orders[0].tags["exit_reason"] == EXIT_STOP_LOSS  # type: ignore[attr-defined]


def test_resume_is_idempotent_and_rejects_nonsense() -> None:
    monitor, _engine, _bus = _monitor("2.00")

    assert monitor.resume(
        position_id=_POSITION_ID,
        token=_token(),
        entry_price=Decimal("2.00"),
        quantity=Decimal("25"),
        tags=_envelope(),
    )
    # A live PositionOpened must not be clobbered by a late restore.
    assert not monitor.resume(
        position_id=_POSITION_ID,
        token=_token(),
        entry_price=Decimal("9.00"),
        quantity=Decimal("1"),
        tags=_envelope(),
    )
    assert not monitor.resume(
        position_id=_OTHER_ID,
        token=_token(),
        entry_price=Decimal("0"),
        quantity=Decimal("25"),
        tags=_envelope(),
    )
    assert monitor.tracked == 1


def test_a_recovered_peak_rearms_the_trailing_stop() -> None:
    """A restart must not silently disarm a trailing stop that had triggered."""
    monitor, _engine, _bus = _monitor("2.00")
    tags = {
        **_envelope(),
        TAG_TRAILING_ENABLED: "true",
        TAG_TRAILING_ACTIVATION_PCT: "10",
        TAG_TRAILING_DISTANCE_PCT: "5",
    }

    monitor.resume(
        position_id=_POSITION_ID,
        token=_token(),
        entry_price=Decimal("2.00"),
        quantity=Decimal("25"),
        tags=tags,
        peak_price=Decimal("2.50"),  # already past the +10% activation
    )

    assert monitor._positions[_POSITION_ID].trailing_armed
