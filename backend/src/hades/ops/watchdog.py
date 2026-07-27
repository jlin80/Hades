"""Watchdog process — the independent monitoring service.

Assembles the dependency/resource probes and the Health Monitor, then runs the
:class:`Watchdog` loop that continuously checks Postgres, Redis, RPC, the API,
the dashboard, host resources and background-service liveness. Failures are
logged, persisted and surfaced as notifications (via events, never Discord
directly).
"""

from __future__ import annotations

import asyncio

from hades.contexts.monitoring.application.health_monitor import HealthMonitor
from hades.contexts.monitoring.application.recovery import (
    EmergencyMode,
    RecoveryAction,
    RecoveryOrchestrator,
)
from hades.contexts.monitoring.application.watchdog import Watchdog
from hades.contexts.monitoring.domain.ports import HealthProbe
from hades.contexts.monitoring.infrastructure.probes import (
    ClickHouseProbe,
    HttpProbe,
    PostgresProbe,
    RedisProbe,
    RpcProbe,
    SystemResourceProbe,
)
from hades.contexts.monitoring.infrastructure.recovery_actions import (
    ConfigReloadAction,
    PostgresEmergencyFlagStore,
    PostgresReconnectAction,
    RedisReconnectAction,
)
from hades.contexts.monitoring.infrastructure.repository import MonitoringRepository
from hades.ops.service import ServiceProcess


class WatchdogProcess(ServiceProcess):
    """Hosts the Health Monitor + Watchdog loop."""

    role = "watchdog"

    async def setup(self) -> None:
        settings = self._container.settings
        wd = settings.watchdog
        database = self._container.database
        assert database is not None  # the watchdog always runs with a database

        resource_probe = SystemResourceProbe(
            cpu_max_pct=wd.cpu_max_pct,
            memory_max_pct=wd.memory_max_pct,
            disk_max_pct=wd.disk_max_pct,
        )
        probes: list[HealthProbe] = [
            PostgresProbe(database),
            RedisProbe(self._container.redis),
            RpcProbe(
                settings.solana.rpc_http_url,
                timeout_seconds=settings.timeouts.rpc_seconds,
                latency_max_ms=wd.rpc_latency_max_ms,
                http=self._container.http,
            ),
            HttpProbe(
                "api",
                wd.api_health_url,
                timeout_seconds=settings.timeouts.http_seconds,
                http=self._container.http,
            ),
            HttpProbe(
                "dashboard",
                wd.dashboard_url,
                timeout_seconds=settings.timeouts.http_seconds,
                http=self._container.http,
            ),
            ClickHouseProbe(self._container.clickhouse),
            resource_probe,
        ]

        repository = MonitoringRepository(database, instance_id=settings.instance_id)
        health_monitor = HealthMonitor(
            probes,
            event_bus=self._container.event_bus,
            metrics=self._container.metrics,
            repository=repository,
            notifier=self._container.notification,
        )

        # Auto-recovery: heal what this process can reach (its own Redis/Postgres
        # connections, the cached config) before escalating to Emergency Mode.
        emergency = EmergencyMode(
            event_bus=self._container.event_bus,
            notifier=self._container.notification,
            flags=PostgresEmergencyFlagStore(database),
        )
        actions: list[RecoveryAction] = [
            RedisReconnectAction(self._container.redis),
            PostgresReconnectAction(database),
            ConfigReloadAction(),
        ]
        recovery = RecoveryOrchestrator(
            actions,
            emergency=emergency,
            notifier=self._container.notification,
            metrics=self._container.metrics,
            max_attempts=wd.unhealthy_after_missed_beats,
        )

        watchdog = Watchdog(
            health_monitor,
            metrics=self._container.metrics,
            resource_probe=resource_probe,
            watched_roles=wd.watched_role_list,
            liveness_dir=wd.liveness_dir,
            liveness_max_age_seconds=wd.liveness_max_age_seconds,
            interval_seconds=wd.probe_interval_seconds,
            notifier=self._container.notification,
            repository=repository,
            recovery=recovery,
        )
        self._tasks.append(asyncio.create_task(watchdog.run(self._stop)))
        self._log.info("watchdog_process_ready", probes=[p.name for p in probes])
