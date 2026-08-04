"""FastAPI dependency helpers to reach the wired container and app services."""

from __future__ import annotations

from fastapi import Depends, Request

from hades.bootstrap import Container
from hades.contexts.audit.application.recorder import AuditRecorder
from hades.contexts.audit.infrastructure.store import InMemoryAuditStore, PostgresAuditStore
from hades.contexts.execution.application.trading_mode import TradingModeService
from hades.contexts.execution.infrastructure.trading_mode_repository import (
    TradingModeRepository,
)
from hades.ops.config_manager import (
    ConfigManager,
    ConfigSnapshotStore,
    InMemoryConfigSnapshotStore,
    InMemoryRuntimeConfigStore,
    PostgresConfigSnapshotStore,
    PostgresRuntimeConfigStore,
    RuntimeConfigStore,
)
from hades.ops.execution_runtime import (
    EXECUTION_STATUS_NAMESPACE,
)
from hades.ops.execution_runtime import (
    STATUS_KEY as EXECUTION_STATUS_KEY,
)
from hades.ops.preflight import build_production_checklist
from hades.shared_kernel.cache import CacheService


def get_container(request: Request) -> Container:
    """Return the process container stored on app state at startup."""
    return request.app.state.container  # type: ignore[no-any-return]


def get_config_manager(container: Container = Depends(get_container)) -> ConfigManager:
    """Construct the Configuration Manager bound to the process collaborators."""
    snapshots: ConfigSnapshotStore
    runtime: RuntimeConfigStore
    if container.database is not None:
        snapshots = PostgresConfigSnapshotStore(container.database)
        runtime = PostgresRuntimeConfigStore(container.database)
        audit = AuditRecorder(PostgresAuditStore(container.database))
    else:
        snapshots = InMemoryConfigSnapshotStore()
        runtime = InMemoryRuntimeConfigStore()
        audit = AuditRecorder(InMemoryAuditStore())
    return ConfigManager(container.settings, snapshots=snapshots, runtime=runtime, audit=audit)


def get_trading_mode_service(
    container: Container = Depends(get_container),
) -> TradingModeService:
    """Construct the trading-mode service bound to the process collaborators.

    The Production Checklist is folded in as the live-readiness gate: switching to
    LIVE is refused if any required platform check fails or Emergency Mode is on.
    """
    repository = (
        TradingModeRepository(container.database) if container.database is not None else None
    )

    async def live_executor_available() -> bool:
        """Ask the Worker whether it actually built a live executor.

        The API cannot answer this from its own process — the Execution Engine
        lives in the Worker, which publishes the fact in its status snapshot.
        A missing or unreadable snapshot yields False, so an unreachable Worker
        blocks the switch to live rather than waving it through.
        """
        cache = CacheService(container.redis, namespace=EXECUTION_STATUS_NAMESPACE)
        try:
            snapshot = await cache.get(EXECUTION_STATUS_KEY)
        except Exception:
            return False
        if not isinstance(snapshot, dict):
            return False
        return snapshot.get("live_executor_available") is True

    return TradingModeService(
        container.settings,
        event_bus=container.event_bus,
        notifier=container.notification,
        repository=repository,
        checklist=build_production_checklist(container),
        live_executor_probe=live_executor_available,
    )
