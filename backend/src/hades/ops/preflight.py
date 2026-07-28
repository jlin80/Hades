"""Deployment Validator — the startup preflight for ``docker compose up``.

Before the platform starts doing real work it should prove its footing:
configuration is coherent, dependencies import, the datastores and RPC answer,
and the database schema is migrated to head. :class:`DeploymentValidator` runs
those checks, produces a :class:`PreflightReport`, logs it and (best-effort)
posts a Discord summary. The ``hades-preflight`` entrypoint runs it once and exits
non-zero if any *required* check fails, so it can gate a deployment.

It is read-only and side-effect free apart from the notification — it never
migrates, writes or trades. It only *reports*.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import text

from hades.bootstrap import Container, build_container
from hades.contexts.monitoring.application.production_checklist import ProductionChecklist
from hades.contexts.monitoring.application.recovery import EmergencyFlagStore
from hades.contexts.monitoring.domain.models import HealthStatus
from hades.contexts.monitoring.domain.ports import HealthProbe
from hades.contexts.monitoring.infrastructure.probes import (
    ClickHouseProbe,
    HttpProbe,
    PostgresProbe,
    RedisProbe,
    RpcProbe,
)
from hades.contexts.monitoring.infrastructure.recovery_actions import (
    InMemoryEmergencyFlagStore,
    PostgresEmergencyFlagStore,
)
from hades.contexts.notification.domain.ports import Severity
from hades.shared_kernel.logging import get_logger

_logger = get_logger("preflight")

# The Alembic revision the schema should be at once every migration is applied.
MIGRATIONS_HEAD = "0007_config_snapshots"


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class PreflightReport:
    checks: list[PreflightCheck]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def required_failed(self) -> list[str]:
        return [c.name for c in self.checks if c.required and not c.ok]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "required_failed": self.required_failed,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required}
                for c in self.checks
            ],
        }


def build_probes(container: Container) -> list[HealthProbe]:
    """Assemble the standard dependency probes from the container."""
    s = container.settings
    probes: list[HealthProbe] = []
    if container.database is not None:
        probes.append(PostgresProbe(container.database))
    probes.append(RedisProbe(container.redis))
    probes.append(
        RpcProbe(
            s.solana.rpc_http_url,
            timeout_seconds=s.timeouts.rpc_seconds,
            latency_max_ms=s.watchdog.rpc_latency_max_ms,
            http=container.http,
        )
    )
    http_timeout = s.timeouts.http_seconds
    probes.append(
        HttpProbe(
            "api", s.watchdog.api_health_url, timeout_seconds=http_timeout, http=container.http
        )
    )
    probes.append(
        HttpProbe(
            "dashboard", s.watchdog.dashboard_url, timeout_seconds=http_timeout, http=container.http
        )
    )
    probes.append(ClickHouseProbe(container.clickhouse))
    return probes


def build_production_checklist(container: Container) -> ProductionChecklist:
    """Assemble the Production Checklist bound to this process's collaborators."""
    emergency: EmergencyFlagStore = (
        PostgresEmergencyFlagStore(container.database)
        if container.database is not None
        else InMemoryEmergencyFlagStore()
    )
    return ProductionChecklist(
        container.settings, probes=build_probes(container), emergency=emergency
    )


class DeploymentValidator:
    """Validates configuration, connectivity and schema at startup."""

    #: Probes whose failure blocks startup readiness.
    _REQUIRED = frozenset({"postgres", "redis", "rpc"})

    def __init__(self, container: Container) -> None:
        self._c = container

    async def validate(self) -> PreflightReport:
        checks: list[PreflightCheck] = []
        checks.extend(self._config_checks())
        checks.extend(await self._connectivity_checks())
        checks.append(await self._migrations_check())
        return PreflightReport(checks=checks)

    def _config_checks(self) -> list[PreflightCheck]:
        s = self._c.settings
        risk_ok = s.risk.max_position_size_usd > 0 and s.risk.max_concurrent_positions > 0
        gate = "on" if s.live_trading_enabled else "off"
        return [
            PreflightCheck(
                "config_database",
                bool(s.postgres.host and s.postgres.db),
                "postgres target configured",
            ),
            PreflightCheck("config_risk_limits", risk_ok, "risk limits positive"),
            PreflightCheck(
                "config_live_gate",
                True,
                f"live_gate={gate}, mode={s.trading_mode.value}",
                required=False,
            ),
        ]

    async def _connectivity_checks(self) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        for probe in build_probes(self._c):
            try:
                health = await probe.check()
                ok = health.status is HealthStatus.HEALTHY
                detail = health.detail
            except Exception as exc:
                ok, detail = False, str(exc)
            checks.append(
                PreflightCheck(probe.name, ok, detail, required=probe.name in self._REQUIRED)
            )
        return checks

    async def _migrations_check(self) -> PreflightCheck:
        if self._c.database is None:
            return PreflightCheck("migrations", False, "no database configured")
        try:
            async with self._c.database.session() as session:
                current = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
        except Exception as exc:
            return PreflightCheck("migrations", False, f"alembic_version unreadable: {exc}")
        if current == MIGRATIONS_HEAD:
            return PreflightCheck("migrations", True, f"at head {MIGRATIONS_HEAD}")
        return PreflightCheck(
            "migrations", False, f"at {current!r}, expected head {MIGRATIONS_HEAD!r}"
        )

    async def notify(self, report: PreflightReport) -> None:
        category = "SUCCESS" if report.ok else "ERROR"
        severity = Severity.INFO if report.ok else Severity.CRITICAL
        failed = ", ".join(report.required_failed) or "none"
        try:
            await self._c.notification.notify(
                title=f"Preflight {'passed' if report.ok else 'FAILED'}",
                body=f"{len(report.checks)} checks. Required failures: {failed}.",
                severity=severity,
                tags={
                    "category": category,
                    "topic": "health",
                    "container": "preflight",
                    "mode": self._c.settings.trading_mode.value,
                },
            )
        except Exception as exc:  # notification is best-effort
            _logger.warning("preflight_notify_failed", error=str(exc))


async def _run() -> int:
    container = build_container(role="preflight")
    try:
        validator = DeploymentValidator(container)
        report = await validator.validate()
        for check in report.checks:
            _logger.info(
                "preflight_check",
                name=check.name,
                ok=check.ok,
                required=check.required,
                detail=check.detail,
            )
        await validator.notify(report)
        _logger.info("preflight_complete", ok=report.ok, required_failed=report.required_failed)
        return 0 if report.ok else 1
    finally:
        await container.shutdown()


def run_preflight() -> None:
    """Console entrypoint: run the preflight once and exit with its status."""
    raise SystemExit(asyncio.run(_run()))
