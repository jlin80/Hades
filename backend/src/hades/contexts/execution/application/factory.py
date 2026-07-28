"""Factory — assembles a fully-wired Execution Engine from Settings.

Keeps construction in one place. The safety-critical decision lives here: the
**paper executor is always built** and is the default; the **live executor is
only built when the hard live gate is enabled** *and* the collaborators it needs
(a signer, a quote provider, an RPC gateway) are present. When live cannot be
built, the engine simply has no live executor and the mode resolver falls back to
paper — a config file alone can never route real orders.
"""

from __future__ import annotations

from dataclasses import dataclass

from hades.contexts.execution.application.confirmation import ConfirmationEngine
from hades.contexts.execution.application.engine import ExecutionEngine, ModeProvider
from hades.contexts.execution.application.fees import FeeEngine
from hades.contexts.execution.application.live_executor import LiveExecutor
from hades.contexts.execution.application.metrics import ExecutionMetrics
from hades.contexts.execution.application.order_manager import OrderManager
from hades.contexts.execution.application.paper_executor import PaperExecutor
from hades.contexts.execution.application.retry import RetryEngine, RetryPolicy
from hades.contexts.execution.application.slippage import SlippageEngine
from hades.contexts.execution.application.swap_manager import SwapManager
from hades.contexts.execution.application.transaction_manager import TransactionManager
from hades.contexts.execution.domain.models import ExecutionMode
from hades.contexts.execution.domain.ports import (
    Executor,
    OrderStore,
    PriceOracle,
    QuoteProvider,
    RpcGateway,
    TransactionSigner,
    TransactionStore,
)
from hades.contexts.execution.infrastructure.stores import (
    InMemoryOrderStore,
    InMemoryTransactionStore,
)
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.shared_kernel.config import Settings
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.factory")


@dataclass
class ExecutionEngineBundle:
    """The engine plus the managers the runtime/API need to read status."""

    engine: ExecutionEngine
    order_manager: OrderManager
    transaction_manager: TransactionManager
    live_enabled: bool


def build_slippage_engine(settings: Settings) -> SlippageEngine:
    e = settings.execution
    return SlippageEngine(
        base_bps=e.slippage_bps,
        hard_max_bps=e.hard_max_slippage_bps,
    )


def build_fee_engine(settings: Settings) -> FeeEngine:
    e = settings.execution
    return FeeEngine(
        priority_fee_microlamports=e.priority_fee_microlamports,
        dex_fee_bps=e.dex_fee_bps,
        sol_price_usd=e.sol_price_usd,
        jito_enabled=e.jito_enabled,
        jito_tip_microlamports=e.jito_tip_microlamports,
    )


def build_execution_engine(
    settings: Settings,
    *,
    event_bus: EventBus,
    notifier: NotificationPublisher,
    mode_provider: ModeProvider,
    metrics: ExecutionMetrics | None = None,
    price_oracle: PriceOracle | None = None,
    signer: TransactionSigner | None = None,
    quote_provider: QuoteProvider | None = None,
    rpc: RpcGateway | None = None,
    order_store: OrderStore | None = None,
    transaction_store: TransactionStore | None = None,
) -> ExecutionEngineBundle:
    """Wire the Execution Engine. Paper is mandatory; live is gated + optional."""
    e = settings.execution

    slippage = build_slippage_engine(settings)
    fees = build_fee_engine(settings)
    order_manager = OrderManager(store=order_store or InMemoryOrderStore())
    transaction_manager = TransactionManager(store=transaction_store or InMemoryTransactionStore())

    paper = PaperExecutor(
        slippage_engine=slippage,
        fee_engine=fees,
        price_oracle=price_oracle,
        latency_ms=settings.paper.simulated_latency_ms,
        base_slippage_bps=settings.paper.simulated_slippage_bps,
    )
    executors: dict[str, Executor] = {ExecutionMode.PAPER.value: paper}

    live = _maybe_build_live(
        settings, fees=fees, signer=signer, quote_provider=quote_provider, rpc=rpc
    )
    if live is not None:
        executors[ExecutionMode.LIVE.value] = live

    engine = ExecutionEngine(
        executors=executors,
        mode_provider=mode_provider,
        order_manager=order_manager,
        transaction_manager=transaction_manager,
        event_bus=event_bus,
        notifier=notifier,
        metrics=metrics,
        default_max_slippage_bps=e.max_slippage_bps,
    )
    return ExecutionEngineBundle(
        engine=engine,
        order_manager=order_manager,
        transaction_manager=transaction_manager,
        live_enabled=live is not None,
    )


def _maybe_build_live(
    settings: Settings,
    *,
    fees: FeeEngine,
    signer: TransactionSigner | None,
    quote_provider: QuoteProvider | None,
    rpc: RpcGateway | None,
) -> LiveExecutor | None:
    """Build the live executor only when the hard gate + collaborators are present."""
    if not settings.live_trading_enabled:
        _logger.info("live_executor_not_built", reason="live gate disabled")
        return None
    missing = [
        name
        for name, obj in (("signer", signer), ("quote_provider", quote_provider), ("rpc", rpc))
        if obj is None
    ]
    if missing:
        _logger.warning("live_executor_not_built", reason="missing collaborators", missing=missing)
        return None
    assert signer is not None and quote_provider is not None and rpc is not None
    e = settings.execution
    swap = SwapManager(quote_provider, quote_mint=e.quote_mint, sol_price_usd=e.sol_price_usd)
    confirmation = ConfirmationEngine(
        rpc,
        timeout_seconds=e.confirmation_timeout_seconds,
        poll_interval_seconds=e.confirmation_poll_seconds,
    )
    retry = RetryEngine(
        RetryPolicy(
            max_attempts=e.retry_max_attempts,
            base_delay_seconds=e.retry_base_delay_seconds,
        )
    )
    _logger.warning("live_executor_built", note="real funds may be at risk when mode=live")
    return LiveExecutor(
        swap_manager=swap,
        signer=signer,
        confirmation=confirmation,
        fee_engine=fees,
        rpc=rpc,
        retry=retry,
    )
