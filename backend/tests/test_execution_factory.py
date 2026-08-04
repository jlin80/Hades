"""The factory builds paper by default and gates live behind the hard switch."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hades.contexts.execution.application.factory import build_execution_engine
from hades.contexts.execution.domain.models import ExecutionMode, Quote
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.shared_kernel.config.settings import Settings
from hades.shared_kernel.events import InMemoryEventBus


async def _mode() -> str:
    return "paper"


def _deps() -> tuple[InMemoryEventBus, NotificationPublisher]:
    bus = InMemoryEventBus()
    return bus, NotificationPublisher(bus)


class _Signer:
    @property
    def public_key(self) -> str:
        return "owner"

    async def sign_and_send(self, serialized_tx: bytes) -> str:
        return "sig"


class _Quotes:
    async def quote(
        self, *, input_mint: str, output_mint: str, amount: Decimal, slippage_bps: int
    ) -> Quote:
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=amount,
            price=Decimal(1),
        )

    async def build_swap_tx(self, quote: Quote, *, owner: str) -> bytes:
        return b"tx"


class _Rpc:
    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        return {"value": []}

    @property
    def active_endpoint(self) -> str | None:
        return "rpc-1"


def test_paper_is_always_built_and_live_is_not_by_default() -> None:
    bus, notifier = _deps()
    bundle = build_execution_engine(
        Settings(), event_bus=bus, notifier=notifier, mode_provider=_mode
    )
    assert bundle.live_enabled is False
    assert ExecutionMode.PAPER.value in bundle.engine._executors
    assert ExecutionMode.LIVE.value not in bundle.engine._executors


def test_live_not_built_even_with_gate_when_adapters_missing() -> None:
    bus, notifier = _deps()
    settings = Settings().model_copy(update={"live_trading_enabled": True})
    bundle = build_execution_engine(
        settings, event_bus=bus, notifier=notifier, mode_provider=_mode
    )
    # Gate on but no signer/quote/rpc → live still not built (fail-safe).
    assert bundle.live_enabled is False


def test_live_built_only_with_gate_and_all_adapters() -> None:
    bus, notifier = _deps()
    settings = Settings().model_copy(update={"live_trading_enabled": True})
    bundle = build_execution_engine(
        settings,
        event_bus=bus,
        notifier=notifier,
        mode_provider=_mode,
        signer=_Signer(),
        quote_provider=_Quotes(),
        rpc=_Rpc(),
    )
    assert bundle.live_enabled is True
    assert ExecutionMode.LIVE.value in bundle.engine._executors
    # The original live adapter remains the default — the fast path is opt-in.
    assert bundle.live_adapter == "live"


def test_fast_path_flag_alone_never_opens_the_live_gate() -> None:
    """The flag chooses *between* live adapters; it can never enable live."""
    bus, notifier = _deps()
    settings = Settings()
    settings.execution.fast_path_enabled = True
    bundle = build_execution_engine(
        settings,
        event_bus=bus,
        notifier=notifier,
        mode_provider=_mode,
        signer=_Signer(),
        quote_provider=_Quotes(),
        rpc=_Rpc(),
    )
    assert bundle.live_enabled is False
    assert bundle.live_adapter is None
    assert ExecutionMode.LIVE.value not in bundle.engine._executors


def test_fast_path_adapter_is_selected_only_when_the_flag_is_on() -> None:
    bus, notifier = _deps()
    settings = Settings().model_copy(update={"live_trading_enabled": True})
    settings.execution.fast_path_enabled = True
    bundle = build_execution_engine(
        settings,
        event_bus=bus,
        notifier=notifier,
        mode_provider=_mode,
        signer=_Signer(),
        quote_provider=_Quotes(),
        rpc=_Rpc(),
    )
    assert bundle.live_enabled is True
    assert bundle.live_adapter == "fast_path"


def test_fast_path_is_off_by_default_in_settings() -> None:
    assert Settings().execution.fast_path_enabled is False
