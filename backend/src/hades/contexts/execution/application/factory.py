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
from hades.contexts.execution.application.fast_path_executor import FastPathExecutor
from hades.contexts.execution.application.fees import FeeEngine
from hades.contexts.execution.application.live_executor import LiveExecutor
from hades.contexts.execution.application.metrics import ExecutionMetrics
from hades.contexts.execution.application.order_manager import OrderManager
from hades.contexts.execution.application.paper_executor import PaperExecutor
from hades.contexts.execution.application.retry import RetryEngine, RetryPolicy
from hades.contexts.execution.application.shadow import ShadowExecutor
from hades.contexts.execution.application.slippage import SlippageEngine
from hades.contexts.execution.application.submitters import SignerSubmitter
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
    TransactionSubmitter,
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
    #: Which live adapter was built ("live" | "fast_path"), or ``None`` when the
    #: live gate is closed. Reported so an operator can tell from status alone
    #: which path would carry an order, without inferring it from config.
    live_adapter: str | None = None


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
    submitter: TransactionSubmitter | None = None,
    shadow_candidate: Executor | None = None,
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
    paper_seam = _maybe_shadow(settings, primary=paper, candidate=shadow_candidate)
    executors: dict[str, Executor] = {ExecutionMode.PAPER.value: paper_seam}

    live = _maybe_build_live(
        settings,
        fees=fees,
        signer=signer,
        quote_provider=quote_provider,
        rpc=rpc,
        submitter=submitter,
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
        live_adapter=_adapter_label(live),
    )


def _adapter_label(live: LiveExecutor | FastPathExecutor | None) -> str | None:
    if live is None:
        return None
    return "fast_path" if isinstance(live, FastPathExecutor) else "live"


def _maybe_shadow(
    settings: Settings, *, primary: Executor, candidate: Executor | None
) -> Executor:
    """Wrap ``primary`` in a shadow comparison, or return it untouched.

    Fail-safe in both directions: no flag means no wrapping, and a flag with no
    candidate means no wrapping either (loudly — an operator who turned shadow on
    and got silence would reasonably assume it was measuring something).
    """
    if not settings.execution.shadow_enabled:
        return primary
    if candidate is None:
        _logger.warning(
            "shadow_not_built",
            reason="no candidate adapter supplied",
            note="shadow mode is enabled but has nothing to compare against",
        )
        return primary
    _logger.info("shadow_built", primary_mode=primary.mode, candidate_mode=candidate.mode)
    return ShadowExecutor(
        primary=primary,
        candidate=candidate,
        timeout_seconds=settings.execution.shadow_timeout_seconds,
    )


def _maybe_build_live(
    settings: Settings,
    *,
    fees: FeeEngine,
    signer: TransactionSigner | None,
    quote_provider: QuoteProvider | None,
    rpc: RpcGateway | None,
    submitter: TransactionSubmitter | None = None,
) -> LiveExecutor | FastPathExecutor | None:
    """Build the live executor only when the hard gate + collaborators are present.

    Which live adapter gets built is a second, independent decision: the original
    :class:`LiveExecutor` unless ``execution.fast_path_enabled`` selects the
    instrumented :class:`FastPathExecutor`. The flag can only ever *choose between
    two live adapters* — it can never open the live gate, which is checked first
    and unconditionally.
    """
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
    if e.fast_path_enabled:
        transport = submitter or SignerSubmitter(signer)
        _logger.warning(
            "fast_path_executor_built",
            route=transport.route,
            note="real funds may be at risk when mode=live",
        )
        return FastPathExecutor(
            swap_manager=swap,
            submitter=transport,
            confirmation=confirmation,
            fee_engine=fees,
            retry=retry,
            owner=signer.public_key,
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
