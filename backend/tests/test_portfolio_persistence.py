"""The portfolio book survives a restart.

Before this, ``PortfolioManager`` was purely in-memory: every worker restart
reset cash to the configured starting balance and dropped every open position,
so the balance appeared frozen at its opening value no matter what had traded.
"""

from __future__ import annotations

from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.portfolio.application.portfolio_manager import PortfolioManager
from hades.contexts.portfolio.domain.events import PositionClosed, PositionOpened
from hades.contexts.portfolio.domain.ports import PortfolioStateStore
from hades.contexts.portfolio.infrastructure.stores import InMemoryPortfolioStateStore
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import InMemoryEventBus

_MINT = "So11111111111111111111111111111111111111112"
_START = 1_000.0


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


def _manager(store: InMemoryPortfolioStateStore) -> tuple[PortfolioManager, InMemoryEventBus]:
    bus = InMemoryEventBus()
    manager = PortfolioManager(
        starting_balance_usd=_START,
        mode="paper",
        event_bus=bus,
        state_store=store,
    )
    manager.register(bus)
    return manager, bus


async def _open(bus: InMemoryEventBus, position_id: str, notional: float) -> None:
    await bus.publish(
        PositionOpened(
            aggregate_id=position_id,
            token=_token(),
            entry_price=Money(amount=Decimal(1)),
            quantity=Decimal(str(notional)),
            notional=Money(amount=Decimal(str(notional))),
            tags={"strategy": "momentum", "developer": "dev-1", "regime": "bull"},
        )
    )


def test_the_in_memory_store_satisfies_the_port() -> None:
    assert isinstance(InMemoryPortfolioStateStore(), PortfolioStateStore)


async def test_opening_a_position_persists_the_book() -> None:
    store = InMemoryPortfolioStateStore()
    _, bus = _manager(store)

    await _open(bus, new_id(), 250.0)

    saved = await store.load(mode="paper")
    assert saved is not None
    assert saved.cash_usd == 750.0
    assert len(saved.positions) == 1


async def test_a_restart_recovers_cash_and_open_positions() -> None:
    store = InMemoryPortfolioStateStore()
    first, bus = _manager(store)
    position_id = new_id()
    await _open(bus, position_id, 300.0)
    before = first.state()

    # A brand-new manager, exactly as a restarted worker would build it.
    second, _ = _manager(store)
    assert second.state().cash_usd == _START  # cold, before restoring

    saved = await store.load(mode="paper")
    assert saved is not None
    second.restore(saved)

    after = second.state()
    assert after.cash_usd == before.cash_usd == 700.0
    assert after.invested_usd == before.invested_usd == 300.0
    assert after.open_positions == 1
    assert after.equity_usd == before.equity_usd


async def test_realized_pnl_survives_a_restart() -> None:
    store = InMemoryPortfolioStateStore()
    first, bus = _manager(store)
    position_id = new_id()
    await _open(bus, position_id, 200.0)
    await bus.publish(
        PositionClosed(
            aggregate_id=position_id,
            exit_price=Money(amount=Decimal("1.25")),
            realized_pnl=Money(amount=Decimal("50")),
            reason="take_profit",
        )
    )
    assert first.state().realized_pnl_usd == 50.0

    saved = await store.load(mode="paper")
    assert saved is not None
    second, _ = _manager(store)
    second.restore(saved)

    state = second.state()
    assert state.realized_pnl_usd == 50.0
    assert state.cash_usd == 1_050.0
    assert state.open_positions == 0
    assert len(second.closed_trades()) == 1


async def test_the_position_attribution_survives_a_restart() -> None:
    """Exposure and correlation caps depend on these tags being recovered."""
    store = InMemoryPortfolioStateStore()
    _, bus = _manager(store)
    await _open(bus, new_id(), 100.0)

    saved = await store.load(mode="paper")
    assert saved is not None
    second, _ = _manager(store)
    second.restore(saved)

    risk_state = await second.risk_state()
    position = risk_state.open_positions[0]
    assert position.strategy == "momentum"
    assert position.developer == "dev-1"
    assert position.regime == "bull"


async def test_the_configured_starting_balance_wins_over_a_stored_one() -> None:
    """Otherwise raising PAPER_STARTING_BALANCE_USD would silently do nothing."""
    store = InMemoryPortfolioStateStore()
    _, bus = _manager(store)
    await _open(bus, new_id(), 100.0)
    saved = await store.load(mode="paper")
    assert saved is not None
    assert saved.starting_balance_usd == _START

    bus2 = InMemoryEventBus()
    reconfigured = PortfolioManager(
        starting_balance_usd=5_000.0, mode="paper", event_bus=bus2, state_store=store
    )
    reconfigured.restore(saved)

    assert reconfigured.state().starting_balance_usd == 5_000.0
    assert reconfigured.state().cash_usd == 900.0  # the book itself is unchanged


async def test_a_book_is_kept_per_mode() -> None:
    store = InMemoryPortfolioStateStore()
    _, bus = _manager(store)
    await _open(bus, new_id(), 400.0)

    assert await store.load(mode="paper") is not None
    assert await store.load(mode="live") is None


async def test_a_failing_store_never_blocks_the_manager() -> None:
    class _BrokenStore:
        async def load(self, *, mode: str) -> None:
            raise RuntimeError("down")

        async def save(self, state: object, *, mode: str) -> None:
            raise RuntimeError("down")

    bus = InMemoryEventBus()
    manager = PortfolioManager(
        starting_balance_usd=_START,
        mode="paper",
        event_bus=bus,
        state_store=_BrokenStore(),  # type: ignore[arg-type]
    )
    manager.register(bus)

    await _open(bus, new_id(), 100.0)

    assert manager.state().cash_usd == 900.0  # live state is unaffected
