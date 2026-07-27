"""Health + readiness endpoints (consumed by Docker/K8s probes and the UI)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from hades.api.dependencies import get_container
from hades.bootstrap import Container
from hades.contexts.monitoring.domain.models import (
    ComponentHealth,
    HealthStatus,
    SystemHealth,
)
from hades.ops.liveness import Liveness
from hades.ops.preflight import (
    DeploymentValidator,
    build_probes,
    build_production_checklist,
)

router = APIRouter(tags=["health"])


#: Background roles that touch a liveness file. The API has no file of its own —
#: answering this request *is* its liveness proof.
_BACKGROUND_ROLES = ("worker", "engine", "scheduler", "notification", "watchdog")


async def _dependency_components(container: Container) -> list[ComponentHealth]:
    """Probe Postgres/Redis/RPC. A probe that raises is reported unhealthy, never
    propagated: a health endpoint that 500s tells an operator nothing."""
    components: list[ComponentHealth] = []
    for probe in build_probes(container):
        # The API's own HTTP probe would have this endpoint call itself.
        if getattr(probe, "name", "") in {"api", "dashboard"}:
            continue
        try:
            components.append(await probe.check())
        except Exception as exc:
            components.append(
                ComponentHealth(
                    name=getattr(probe, "name", "probe"),
                    status=HealthStatus.UNHEALTHY,
                    detail=f"probe failed: {exc}",
                )
            )
    return components


def _process_components(container: Container) -> list[ComponentHealth]:
    """Report each background process from its liveness heartbeat file.

    A process that has never started has no file at all — reported as ``unknown``
    rather than unhealthy, because "not deployed" and "hung" are different facts
    and conflating them would make the endpoint lie in single-service setups.
    """
    max_age = container.settings.watchdog.liveness_max_age_seconds
    directory = container.settings.watchdog.liveness_dir
    components: list[ComponentHealth] = []
    for role in _BACKGROUND_ROLES:
        age = Liveness(role, directory=directory).age_seconds()
        if age is None:
            status, detail = HealthStatus.UNKNOWN, "no heartbeat file — not running here"
        elif age <= max_age:
            status, detail = HealthStatus.HEALTHY, f"heartbeat {age:.0f}s ago"
        else:
            status, detail = HealthStatus.UNHEALTHY, f"stale heartbeat ({age:.0f}s ago)"
        components.append(ComponentHealth(name=role, status=status, detail=detail))
    return components


@router.get("/health", summary="Liveness + aggregate system health")
async def health(
    response: Response, container: Container = Depends(get_container)
) -> dict[str, object]:
    """Report this process's liveness plus what it can see of everything else.

    Previously this returned a single hardcoded ``api`` component, so the page
    said "healthy" whether or not Postgres, Redis or the Worker were alive — the
    one question an operator opens it to answer. It now probes dependencies and
    reads each background process's heartbeat.

    **This is the liveness probe**, so the status code reflects *this process*
    only: a Redis blip must not make Docker restart a perfectly healthy API and
    turn a dependency hiccup into an outage. Dependency failures are visible in
    ``components`` and are what ``/ready`` gates on.
    """
    components = [
        ComponentHealth(name="api", status=HealthStatus.HEALTHY, detail="process alive"),
        *await _dependency_components(container),
        *_process_components(container),
    ]
    system = SystemHealth.from_components(components)
    return {
        "status": system.status,
        "instance_id": container.settings.instance_id,
        "trading_mode": container.settings.trading_mode,
        "is_live": container.settings.is_live,
        "components": [c.model_dump() for c in system.components],
    }


@router.get("/ready", summary="Readiness probe (gates on dependencies)")
async def ready(
    response: Response, container: Container = Depends(get_container)
) -> dict[str, object]:
    """Ready only when the dependencies this process needs to serve are answering.

    Unlike ``/health`` this *does* fail on a dependency outage — that is the
    point: a load balancer should stop sending traffic while Postgres is gone,
    without the container being restarted underneath it.
    """
    components = await _dependency_components(container)
    system = SystemHealth.from_components(components)
    ready_now = system.status is not HealthStatus.UNHEALTHY
    if not ready_now:
        response.status_code = 503
    return {
        "status": "ready" if ready_now else "not_ready",
        "components": [c.model_dump() for c in system.components],
    }


@router.get("/health/preflight", summary="Deployment validation (config/deps/schema)")
async def preflight(container: Container = Depends(get_container)) -> dict[str, object]:
    """Run the deployment validator on demand (same checks as ``hades-preflight``)."""
    report = await DeploymentValidator(container).validate()
    return report.as_dict()


@router.get("/health/production-checklist", summary="Pre-LIVE readiness of every subsystem")
async def production_checklist(
    response: Response, container: Container = Depends(get_container)
) -> dict[str, object]:
    """The aggregated pre-LIVE checklist. ``ready`` is false — and LIVE stays
    blocked — if any required subsystem fails or Emergency Mode is active."""
    report = await build_production_checklist(container).report()
    if not report["ready"]:
        response.status_code = 503
    return report
