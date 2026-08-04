"""A single failed probe must not trigger a destructive remedy.

`PostgresReconnectAction` disposes the connection pool. Firing it on the first
non-healthy probe means one timed-out check against a perfectly healthy database
drops every pooled connection and forces them all to be rebuilt — on a contended
host, a way to cause the outage it is meant to repair.

`WATCHDOG_UNHEALTHY_AFTER_MISSED_BEATS` existed as a setting the whole time.
Nothing read it.
"""

from __future__ import annotations

import asyncio

from hades.contexts.monitoring.application.watchdog import Watchdog
from hades.contexts.monitoring.domain.models import (
    ComponentHealth,
    HealthStatus,
    SystemHealth,
)


class _Recovery:
    def __init__(self) -> None:
        self.recovered: list[str] = []
        self.reset_calls: list[str] = []

    async def recover(self, component: str, detail: str) -> bool:
        self.recovered.append(component)
        return True

    def reset(self, component: str) -> None:
        self.reset_calls.append(component)


class _Probe:
    def sample(self) -> dict[str, float]:
        return {"cpu_pct": 0.0, "memory_pct": 0.0, "disk_pct": 0.0}


def _watchdog(recovery: _Recovery, *, unhealthy_after: int = 3) -> Watchdog:
    from hades.shared_kernel.observability import MetricsRegistry

    return Watchdog(
        health_monitor=None,  # type: ignore[arg-type]
        metrics=MetricsRegistry(),
        resource_probe=_Probe(),  # type: ignore[arg-type]
        watched_roles=[],
        liveness_dir="/tmp",
        liveness_max_age_seconds=60.0,
        interval_seconds=1.0,
        recovery=recovery,  # type: ignore[arg-type]
        unhealthy_after=unhealthy_after,
    )


def _health(status: HealthStatus) -> SystemHealth:
    return SystemHealth(
        status=status,
        components=[ComponentHealth(name="postgres", status=status, detail="probe timed out")],
    )


def _drive(wd: Watchdog, statuses: list[HealthStatus]) -> None:
    async def run() -> None:
        for s in statuses:
            await wd._drive_recovery(_health(s))

    asyncio.run(run())


def test_one_bad_probe_does_not_trigger_recovery() -> None:
    recovery = _Recovery()

    _drive(_watchdog(recovery), [HealthStatus.UNHEALTHY])

    assert recovery.recovered == []


def test_recovery_fires_only_on_the_configured_consecutive_failure() -> None:
    recovery = _Recovery()

    _drive(_watchdog(recovery), [HealthStatus.UNHEALTHY] * 3)

    assert recovery.recovered == ["postgres"]


def test_a_healthy_probe_clears_the_streak() -> None:
    """Two bad, one good, two bad must not reach the threshold."""
    recovery = _Recovery()

    _drive(
        _watchdog(recovery),
        [
            HealthStatus.UNHEALTHY,
            HealthStatus.UNHEALTHY,
            HealthStatus.HEALTHY,
            HealthStatus.UNHEALTHY,
            HealthStatus.UNHEALTHY,
        ],
    )

    assert recovery.recovered == []
    assert "postgres" in recovery.reset_calls


def test_a_persistent_outage_is_still_acted_on_every_cycle() -> None:
    """Debouncing must not become inaction — a real outage still gets treated."""
    recovery = _Recovery()

    _drive(_watchdog(recovery), [HealthStatus.UNHEALTHY] * 6)

    # Three to arm, then one per subsequent cycle.
    assert len(recovery.recovered) == 4


def test_degraded_counts_toward_the_streak_too() -> None:
    recovery = _Recovery()

    _drive(_watchdog(recovery), [HealthStatus.DEGRADED] * 3)

    assert recovery.recovered == ["postgres"]
