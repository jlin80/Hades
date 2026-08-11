"""The book refuses what it cannot attribute, and counts nothing twice.

Every test here was run against the reintroduced defect and confirmed to fail
without the fix. That matters more than usual: the bug these cover was silent by
construction — no exception, no failed healthcheck, nine green containers — and a
test that passes either way would be worse than no test at all.

The production state that motivated them, read off ``portfolio_state`` on CT 203:

    starting_balance     1,000.00
    cash_usd           -37,713.68
    realized_pnl_usd   -36,990.70
    peak_equity_usd    935,055.02
    open_positions             29
    closed_trades               0     <- 2,420 closes, none attributable
"""

from __future__ import annotations

from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.portfolio.application.portfolio_manager import PortfolioManager
from hades.contexts.portfolio.domain.events import PositionClosed, PositionOpened
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus

_MINT = "So11111111111111111111111111111111111111112"
_OTHER_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _token(mint: str = _MINT, symbol: str = "WSOL") -> TokenRef:
    return TokenRef(mint=TokenMint(address=mint), symbol=symbol)


def _manager() -> tuple[PortfolioManager, InMemoryEventBus]:
    bus = InMemoryEventBus()
    pm = PortfolioManager(starting_balance_usd=1000.0, reserve_pct=35.0, event_bus=bus)
    pm.register(bus)
    return pm, bus


def _opened(position_id: str, *, mint: str = _MINT, notional: str = "100") -> PositionOpened:
    return PositionOpened(
        aggregate_id=position_id,
        token=_token(mint),
        entry_price=Money(amount="1"),
        quantity=Decimal(100),
        notional=Money(amount=notional),
        tags={"strategy": "momentum"},
    )


async def test_redelivered_open_does_not_debit_cash_twice() -> None:
    """At-least-once delivery must not move the book twice.

    ``_on_opened`` overwrote its dict entry (idempotent) while decrementing cash
    unconditionally (not), so a reclaimed message lowered equity by one whole
    notional with the position count unchanged.
    """
    pm, bus = _manager()
    event = _opened(new_id())

    await bus.publish(event)
    await bus.publish(event)  # the orphan reclaim, handing back an unacked message

    st = pm.state()
    assert st.cash_usd == 900.0
    assert st.invested_usd == 100.0
    assert st.open_positions == 1
    assert st.equity_usd == 1000.0


async def test_redelivered_close_does_not_credit_realized_twice() -> None:
    """The mechanism behind a realised PnL that grew without bound."""
    pm, bus = _manager()
    pid = new_id()
    await bus.publish(_opened(pid))

    close = PositionClosed(
        aggregate_id=pid,
        token=_token(),
        exit_price=Money(amount="1.2"),
        realized_pnl=Money(amount="20"),
        reason="take_profit",
    )
    await bus.publish(close)
    await bus.publish(close)

    st = pm.state()
    assert st.realized_pnl_usd == 20.0
    assert st.cash_usd == 1020.0
    assert st.open_positions == 0
    assert len(pm.closed_trades()) == 1


async def test_close_with_lost_id_is_attributed_by_token() -> None:
    """A restart loses the engine's mint -> id map; the mint still identifies it.

    The engine now carries the token and the gross exit proceeds, and the book
    finishes the arithmetic from the entry notional it holds:
    ``130 - 100 - 2 = 28``.
    """
    pm, bus = _manager()
    await bus.publish(_opened(new_id()))

    await bus.publish(
        PositionClosed(
            aggregate_id=new_id(),  # an id no PositionOpened ever used
            token=_token(),
            exit_price=Money(amount="1.3"),
            realized_pnl=Money(amount="-2"),  # the engine knew only the exit fee
            exit_notional=Money(amount="130"),
            reason="take_profit",
        )
    )

    st = pm.state()
    assert st.open_positions == 0
    assert st.realized_pnl_usd == 28.0
    assert st.cash_usd == 1028.0  # principal returned as well as the profit
    trades = pm.closed_trades()
    assert len(trades) == 1
    assert trades[0].pnl_usd == 28.0


async def test_unattributable_close_moves_no_money() -> None:
    """The core refusal: no position, no accounting entry.

    Previously this credited ``realized_pnl`` to the running total and to cash,
    recorded no closed trade to explain either, and left every open position
    exactly where it was. Repeated 2,420 times it is the whole defect.
    """
    pm, bus = _manager()
    await bus.publish(_opened(new_id()))
    before = pm.state()

    await bus.publish(
        PositionClosed(
            aggregate_id=new_id(),
            token=_token(_OTHER_MINT, "USDC"),  # a mint the book does not hold
            exit_price=Money(amount="1.3"),
            realized_pnl=Money(amount="-9999"),
            reason="stop_loss",
        )
    )

    after = pm.state()
    assert after.realized_pnl_usd == before.realized_pnl_usd == 0.0
    assert after.cash_usd == before.cash_usd == 900.0
    assert after.open_positions == 1  # the unrelated position is untouched
    assert pm.closed_trades() == ()


async def test_close_without_token_on_an_unknown_id_is_refused() -> None:
    """An envelope predating the token field cannot be attributed, so it is not."""
    pm, bus = _manager()
    await bus.publish(_opened(new_id()))

    await bus.publish(
        PositionClosed(
            aggregate_id=new_id(),
            exit_price=Money(amount="0.5"),
            realized_pnl=Money(amount="-50"),
            reason="stop_loss",
        )
    )

    st = pm.state()
    assert st.realized_pnl_usd == 0.0
    assert st.cash_usd == 900.0
    assert st.open_positions == 1


async def test_idempotency_guard_survives_a_restart() -> None:
    """The redelivery this guards against is *caused* by the restart.

    A container killed mid-dispatch leaves messages unacked and the reclaim hands
    them back minutes after it comes up, so a guard that reset on startup would be
    empty at precisely the moment it is needed.
    """
    pm, bus = _manager()
    pid = new_id()
    await bus.publish(_opened(pid))
    close = PositionClosed(
        aggregate_id=pid,
        token=_token(),
        exit_price=Money(amount="1.2"),
        realized_pnl=Money(amount="20"),
        reason="take_profit",
    )
    await bus.publish(close)
    snapshot = pm.snapshot()

    revived_bus = InMemoryEventBus()
    revived = PortfolioManager(
        starting_balance_usd=1000.0, reserve_pct=35.0, event_bus=revived_bus
    )
    revived.restore(snapshot)
    revived.register(revived_bus)

    await revived_bus.publish(close)  # reclaimed after the restart

    st = revived.state()
    assert st.realized_pnl_usd == 20.0
    assert st.cash_usd == 1020.0
