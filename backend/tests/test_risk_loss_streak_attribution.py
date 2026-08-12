"""The loss streak counts trades, not the engine's failures to name one.

Found live. Hours after the book was reset to a clean 1,000 USD, the persisted
control state read:

    kill_switch_level    2
    reason               "2530 consecutive losses"
    since                2026-08-11T21:46:39Z

There had been no 2,530 trades. When the Execution Engine cannot attribute the
entry it is closing against it reports the exit fee as the realised PnL — a small
negative number — and the runtime asked only whether that number was below zero.
Every unattributable close was therefore a loss, and 2,530 of them halted the
guardian on a losing streak that never happened.

``exit_notional`` is set on exactly those events. The Portfolio Manager already
refuses to apply them to the book (see ``test_portfolio_reconciliation``); this
pins the matching rule on the other consumer of the same event.
"""

from __future__ import annotations

from decimal import Decimal

from hades.contexts.common.domain.value_objects import Money, TokenMint, TokenRef
from hades.contexts.portfolio.domain.events import PositionClosed
from hades.shared_kernel.domain.identifiers import new_id

_MINT = "So11111111111111111111111111111111111111112"


def _token() -> TokenRef:
    return TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL")


class _RecordingManager:
    """Stands in for the Risk Manager: records what the streak was fed."""

    def __init__(self) -> None:
        self.results: list[bool] = []

    async def record_trade_result(self, *, is_win: bool) -> None:
        self.results.append(is_win)


def _handler(manager: _RecordingManager):  # type: ignore[no-untyped-def]
    """Bind the runtime's handler to a stub manager without building a container.

    The subscriber is a small method on ``RiskRuntime`` and everything it needs is
    ``self._manager``, so binding it directly keeps this test about the rule and
    not about wiring twelve runtimes.
    """
    from hades.ops.risk_runtime import RiskRuntime

    stub = object.__new__(RiskRuntime)
    stub._manager = manager  # type: ignore[attr-defined]
    return stub._on_position_closed  # type: ignore[attr-defined]


def _close(*, unattributed: bool, pnl: str) -> PositionClosed:
    return PositionClosed(
        aggregate_id=new_id(),
        token=_token(),
        exit_price=Money(amount="1"),
        realized_pnl=Money(amount=Decimal(pnl)),
        # Set by the engine only when it could not name the entry.
        exit_notional=Money(amount="100") if unattributed else None,
        reason="stop_loss",
    )


async def test_an_unattributed_close_does_not_count_as_a_loss() -> None:
    manager = _RecordingManager()
    handler = _handler(manager)

    await handler(_close(unattributed=True, pnl="-0.0008"))

    assert manager.results == [], "an unattributable close fed the loss streak"


async def test_many_unattributed_closes_cannot_halt_the_guardian() -> None:
    """The production shape: thousands of them, none a trade."""
    manager = _RecordingManager()
    handler = _handler(manager)

    for _ in range(2_530):
        await handler(_close(unattributed=True, pnl="-0.0008"))

    assert manager.results == []


async def test_a_real_close_still_counts() -> None:
    """The streak must keep working — this is a filter, not a mute."""
    manager = _RecordingManager()
    handler = _handler(manager)

    await handler(_close(unattributed=False, pnl="-5"))
    await handler(_close(unattributed=False, pnl="12"))

    assert manager.results == [False, True]
