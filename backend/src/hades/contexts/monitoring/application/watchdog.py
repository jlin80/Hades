"""Watchdog — the independent monitoring loop.

Runs continuously and, on every tick: (1) drives the Health Monitor over all
dependency/resource probes, (2) verifies each background service is still
touching its liveness file, and (3) exports resource metrics. Any failure is
logged, persisted as a risk event, and surfaced as a notification (via the
event → Notification Service path — never Discord directly).
"""

from __future__ import annotations

import asyncio

from hades.contexts.monitoring.application.health_monitor import HealthMonitor
from hades.contexts.monitoring.application.recovery import RecoveryOrchestrator
from hades.contexts.monitoring.domain.models import HealthStatus, SystemHealth
from hades.contexts.monitoring.infrastructure.probes import SystemResourceProbe
from hades.contexts.monitoring.infrastructure.repository import MonitoringRepository
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.ops.liveness import Liveness
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.observability import MetricsRegistry

_logger = get_logger("watchdog")


class Watchdog:
    """Periodically probes health, service liveness and host resources."""

    def __init__(
        self,
        health_monitor: HealthMonitor,
        *,
        metrics: MetricsRegistry,
        resource_probe: SystemResourceProbe,
        watched_roles: list[str],
        liveness_dir: str,
        liveness_max_age_seconds: float,
        interval_seconds: float,
        notifier: NotificationPublisher | None = None,
        repository: MonitoringRepository | None = None,
        recovery: RecoveryOrchestrator | None = None,
        unhealthy_after: int = 3,
    ) -> None:
        self._health = health_monitor
        self._metrics = metrics
        self._resource_probe = resource_probe
        self._watched = watched_roles
        self._liveness_dir = liveness_dir
        self._max_age = liveness_max_age_seconds
        self._interval = interval_seconds
        self._notifier = notifier
        self._repo = repository
        self._recovery = recovery
        #: Consecutive failed probes required before a component is acted on.
        self._unhealthy_after = max(1, unhealthy_after)
        self._consecutive_bad: dict[str, int] = {}
        self._stale_reported: set[str] = set()
        self._cpu_gauge = metrics.gauge("hades_cpu_percent", "Host CPU utilisation percent")
        self._mem_gauge = metrics.gauge("hades_memory_percent", "Host memory utilisation percent")
        self._disk_gauge = metrics.gauge("hades_disk_percent", "Host disk utilisation percent")

    async def run(self, stop: asyncio.Event) -> None:
        _logger.info("watchdog_started", interval_seconds=self._interval, watched=self._watched)
        while not stop.is_set():
            await self._tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
        _logger.info("watchdog_stopped")

    async def _tick(self) -> None:
        try:
            health = await self._health.run_once()
            await self._drive_recovery(health)
        except Exception as exc:  # never let the loop die
            _logger.error("health_monitor_tick_failed", error=str(exc))

        self._export_resources()
        await self._check_liveness()

    async def _drive_recovery(self, health: SystemHealth) -> None:
        """Heal components that are *persistently* unhealthy; reset healthy ones.

        Recovery used to fire on the first non-healthy probe. That is too eager
        when the remedy is destructive: ``PostgresReconnectAction`` disposes the
        connection pool, so one timed-out probe against a perfectly healthy
        database tore down every pooled connection and forced them all to be
        rebuilt. On a contended host that is a way to *cause* the outage it is
        meant to fix, and a deployment showed the shape of it — recoveries every
        few seconds alongside ``FATAL: sorry, too many clients already``.

        A component must therefore fail ``unhealthy_after`` consecutive probes
        before anything is done to it. A single blip is now noise to be ignored,
        which is what it always was.
        """
        if self._recovery is None:
            return
        for component in health.components:
            if component.status is HealthStatus.HEALTHY:
                if self._consecutive_bad.pop(component.name, 0):
                    _logger.debug("component_probe_recovered_on_its_own", component=component.name)
                self._recovery.reset(component.name)
                continue

            strikes = self._consecutive_bad.get(component.name, 0) + 1
            self._consecutive_bad[component.name] = strikes
            if strikes < self._unhealthy_after:
                _logger.debug(
                    "component_unhealthy_below_threshold",
                    component=component.name,
                    strikes=strikes,
                    needed=self._unhealthy_after,
                )
                continue

            try:
                await self._recovery.recover(component.name, component.detail or "")
            except Exception as exc:  # recovery must never break the loop
                _logger.error("recovery_failed", component=component.name, error=str(exc))

    def _export_resources(self) -> None:
        sample = self._resource_probe.sample()
        self._cpu_gauge.set(sample["cpu_pct"])
        self._mem_gauge.set(sample["memory_pct"])
        self._disk_gauge.set(sample["disk_pct"])

    async def _check_liveness(self) -> None:
        for role in self._watched:
            liveness = Liveness(role, directory=self._liveness_dir)
            fresh = liveness.is_fresh(self._max_age)
            if not fresh and role not in self._stale_reported:
                self._stale_reported.add(role)
                age = liveness.age_seconds()
                detail = "missing" if age is None else f"stale ({age:.0f}s)"
                _logger.error("service_liveness_lost", role=role, detail=detail)
                await self._report_stale(role, detail)
            elif fresh and role in self._stale_reported:
                self._stale_reported.discard(role)
                _logger.info("service_liveness_recovered", role=role)

    async def _report_stale(self, role: str, detail: str) -> None:
        if self._repo is not None:
            try:
                await self._repo.record_risk_event(
                    kind="service_liveness_lost",
                    severity="critical",
                    message=f"service '{role}' liveness {detail}",
                    details={"role": role},
                )
            except Exception as exc:
                _logger.error("risk_event_persist_failed", error=str(exc))
        if self._notifier is not None:
            try:
                await self._notifier.notify(
                    title=f"Service down: {role}",
                    body=f"Liveness {detail}. Docker should restart it.",
                    severity=Severity.CRITICAL,
                    tags={"topic": "health", "role": role},
                )
            except Exception as exc:
                _logger.error("stale_notify_failed", error=str(exc))
