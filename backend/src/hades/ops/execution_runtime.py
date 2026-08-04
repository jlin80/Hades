"""Execution Engine runtime composition — wires and runs the executor.

Process-level wiring (the ops layer may import application + infrastructure). It
assembles the Execution Engine from the container and subscribes it to the end of
the decision pipeline:

    risk ─TradeApproved─► [Execution Engine → paper|live executor]
        ─► OrderFilled/OrderFailed
        ─► PositionOpened/PositionClosed ─► [Portfolio Manager]

The engine is the only component that knows the mode; the runtime feeds it a mode
provider backed by the guarded :class:`TradingModeService` (DB authority ANDed
with the hard env gate). The **live executor is only built when the live gate is
on and its adapters (signer/quote/RPC) are supplied** — otherwise the engine has
paper only and can never route real orders. Status snapshots are published to
Redis for the dashboard, exactly like the Risk runtime.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from hades.bootstrap import Container
from hades.contexts.execution.application.factory import (
    ExecutionEngineBundle,
    build_execution_engine,
)
from hades.contexts.execution.application.metrics import ExecutionMetrics
from hades.contexts.execution.application.position_monitor import PositionMonitor
from hades.contexts.execution.application.trading_mode import TradingModeService
from hades.contexts.execution.application.wallet_manager import WalletManager
from hades.contexts.execution.infrastructure.trading_mode_repository import TradingModeRepository
from hades.contexts.market.infrastructure.price_oracle import DexScreenerPriceOracle
from hades.contexts.portfolio.domain.ports import PortfolioStateStore
from hades.contexts.portfolio.infrastructure.stores import (
    InMemoryPortfolioStateStore,
    PostgresPortfolioStateStore,
)
from hades.contexts.risk.domain.events import TradeApproved
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.runtime")

#: Redis namespace/key the runtime publishes execution status under.
EXECUTION_STATUS_NAMESPACE = "execution"
STATUS_KEY = "status"
_STATUS_TTL_SECONDS = 30


class ExecutionRuntime:
    """Owns the wired Execution Engine and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._metrics = ExecutionMetrics(container.metrics)
        self._cache = CacheService(container.redis, namespace=EXECUTION_STATUS_NAMESPACE)

        repo = TradingModeRepository(container.database) if container.database is not None else None
        self._mode_service = TradingModeService(
            container.settings,
            event_bus=container.event_bus,
            notifier=container.notification,
            repository=repo,
        )
        self._wallet = WalletManager(container.settings.wallet)

        # A real price grounds the paper fill (without it every entry price is
        # 1.0) and is what the Position Monitor marks against. Shared by both.
        self._oracle = self._build_oracle()

        self._bundle: ExecutionEngineBundle = build_execution_engine(
            container.settings,
            event_bus=container.event_bus,
            notifier=container.notification,
            mode_provider=self._resolve_mode,
            metrics=self._metrics,
            price_oracle=self._oracle,
            # Live adapters (signer/quote/rpc) are supplied in a later phase; until
            # then the engine is paper-only regardless of the gate — fail-safe.
        )
        self._monitor = self._build_monitor()
        self._register()

    def _build_oracle(self) -> DexScreenerPriceOracle | None:
        m = self._c.settings.market
        if not m.price_oracle_enabled:
            _logger.warning(
                "price_oracle_disabled",
                note="paper fills use a unit price and positions cannot be marked",
            )
            return None
        return DexScreenerPriceOracle(
            url=m.price_source_url,
            cache_ttl_seconds=m.price_cache_ttl_seconds,
            timeout_seconds=m.price_timeout_seconds,
            batch_size=m.price_batch_size,
        )

    def _build_monitor(self) -> PositionMonitor | None:
        e = self._c.settings.execution
        if not e.position_monitor_enabled:
            _logger.warning(
                "position_monitor_disabled",
                note="positions will be opened and never closed",
            )
            return None
        if self._oracle is None:
            _logger.warning(
                "position_monitor_not_built",
                reason="no price oracle — an exit cannot be evaluated without a mark",
            )
            return None
        return PositionMonitor(
            engine=self._bundle.engine,
            price_oracle=self._oracle,
            event_bus=self._c.event_bus,
            interval_seconds=e.position_monitor_interval_seconds,
            max_slippage_bps=e.max_slippage_bps,
            # One switch turns the Strategy Engine on in both directions: it may
            # veto an entry in the Risk Manager, and request an exit here.
            honour_strategy_exits=self._c.settings.strategy.gate_risk,
        )

    async def _resolve_mode(self) -> str:
        status = await self._mode_service.current()
        return status.mode

    def _register(self) -> None:
        self._c.event_bus.subscribe(TradeApproved.__name__, self._on_trade_approved)
        if self._monitor is not None:
            self._monitor.register(self._c.event_bus)

    async def _on_trade_approved(self, event: DomainEvent) -> None:
        if not isinstance(event, TradeApproved):
            return
        try:
            await self._bundle.engine.on_trade_approved(event)
        except Exception as exc:  # one bad approval must not kill the runtime
            _logger.error("execution_failed", mint=str(event.token.mint), error=str(exc))

    # -- lifecycle ------------------------------------------------------------

    def _build_portfolio_store(self) -> PortfolioStateStore:
        if self._c.database is not None:
            return PostgresPortfolioStateStore(self._c.database)
        return InMemoryPortfolioStateStore()

    async def _resume_open_positions(self) -> None:
        """Re-arm the monitor over positions that survived the last restart.

        The Portfolio Manager rehydrates its book on startup but the monitor had
        no equivalent, so a recovered position was owned by the book and watched
        by nobody — never marked to market, and beyond the reach of the exit the
        Risk Manager approved for it. Reading the same durable snapshot closes
        that asymmetry.

        Best-effort by construction: failing to resume must not stop the engine
        from starting, but it must be loud, because the cost is a position that
        can only be closed by hand.
        """
        if self._monitor is None:
            return
        store = self._build_portfolio_store()
        try:
            book = await store.load(mode=self._c.settings.trading_mode.value)
        except Exception as exc:
            _logger.error("position_resume_failed", error=str(exc))
            return
        if book is None or not book.positions:
            return

        resumed = 0
        stranded: list[str] = []
        for position_id, persisted in book.positions.items():
            if not persisted.is_monitorable:
                stranded.append(str(persisted.token.mint))
                continue
            assert persisted.entry_price is not None and persisted.quantity is not None
            if self._monitor.resume(
                position_id=position_id,
                token=persisted.token,
                entry_price=persisted.entry_price,
                quantity=persisted.quantity,
                tags=persisted.tags,
            ):
                resumed += 1
        _logger.info("positions_resumed", resumed=resumed, stranded=len(stranded))
        if stranded:
            # Written before this snapshot carried entry facts. Nothing can mark
            # these; say so rather than let them sit at a frozen PnL forever.
            _logger.error(
                "positions_not_resumable",
                count=len(stranded),
                mints=stranded[:10],
                note="snapshot predates entry-price persistence; close these manually",
            )

    async def start(self) -> list[asyncio.Task[None]]:
        await self._resume_open_positions()
        tasks = [asyncio.create_task(self._publish_status_loop(), name="execution-status")]
        if self._monitor is not None:
            tasks.extend(await self._monitor.start())
        _logger.info(
            "execution_runtime_started",
            enabled=self._c.settings.execution.enabled,
            live_executor=self._bundle.live_enabled,
            price_oracle=self._oracle is not None,
            position_monitor=self._monitor is not None,
        )
        return tasks

    async def stop(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            await self._monitor.stop()
        if self._oracle is not None:
            await self._oracle.aclose()
        _logger.info("execution_runtime_stopped")

    # -- dashboard status -----------------------------------------------------

    async def _snapshot(self) -> dict[str, Any]:
        mode = await self._mode_service.current()
        wallet = await self._wallet.health()
        return {
            "enabled": self._c.settings.execution.enabled,
            "mode": mode.mode,
            "is_live": mode.is_live,
            "live_gate_enabled": mode.live_gate_enabled,
            "live_executor_available": self._bundle.live_enabled,
            "price_oracle_available": self._oracle is not None,
            "position_monitor_running": self._monitor is not None,
            "positions_monitored": self._monitor.tracked if self._monitor else 0,
            "orders": self._bundle.order_manager.counts(),
            "recent_orders": self._bundle.order_manager.recent(limit=20),
            "recent_transactions": self._bundle.transaction_manager.recent(limit=20),
            "transaction_stats": self._bundle.transaction_manager.stats(),
            "wallet": {
                "configured": wallet.configured,
                "public_key": wallet.public_key,
                "balance_sol": wallet.balance_sol,
                "healthy": wallet.healthy,
                "detail": wallet.detail,
            },
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        interval = self._c.settings.execution.status_interval_seconds
        while not self._stop.is_set():
            try:
                snapshot = await self._snapshot()
                await self._cache.set(STATUS_KEY, snapshot, ttl_seconds=_STATUS_TTL_SECONDS)
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("execution_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    # -- accessors for the API ------------------------------------------------

    @property
    def bundle(self) -> ExecutionEngineBundle:
        return self._bundle
