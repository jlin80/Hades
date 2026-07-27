"""Monitoring value objects (used by the live /health endpoint)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from hades.shared_kernel.domain.base import ValueObject


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    #: The component was not observed at all — e.g. a background process that
    #: is not deployed on this host. Deliberately distinct from UNHEALTHY:
    #: "not running here" and "running but broken" demand different actions,
    #: and it never drags the aggregate down (see ``from_components``).
    UNKNOWN = "unknown"


class ComponentHealth(ValueObject):
    """Health of one component or dependency."""

    name: str
    status: HealthStatus
    detail: str = ""


class SystemHealth(ValueObject):
    """Aggregate system health; ``status`` is the worst of its components."""

    status: HealthStatus
    components: list[ComponentHealth] = Field(default_factory=list)

    @classmethod
    def from_components(cls, components: list[ComponentHealth]) -> SystemHealth:
        # UNKNOWN is intentionally ignored here: an undeployed component must
        # not make a correctly-running deployment look sick.
        if any(c.status is HealthStatus.UNHEALTHY for c in components):
            overall = HealthStatus.UNHEALTHY
        elif any(c.status is HealthStatus.DEGRADED for c in components):
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.HEALTHY
        return cls(status=overall, components=components)
