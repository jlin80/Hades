"""Health probes — one per dependency/resource the Watchdog watches.

Each implements the :class:`HealthProbe` port: a ``name`` and an async ``check``
returning a :class:`ComponentHealth`. A probe must never raise — an exception is
itself an ``UNHEALTHY`` result — so the Health Monitor can always assemble a full
:class:`SystemHealth`.
"""

from __future__ import annotations

import time

import httpx
import psutil
from sqlalchemy import text

from hades.contexts.monitoring.domain.models import ComponentHealth, HealthStatus
from hades.shared_kernel.analytics import ClickHouseProvider
from hades.shared_kernel.cache import RedisProvider
from hades.shared_kernel.http import HttpClientProvider
from hades.shared_kernel.persistence.database import Database


class PostgresProbe:
    """Checks the operational database answers a trivial query."""

    def __init__(self, database: Database, *, degraded_ms: float = 500.0) -> None:
        self._db = database
        self._degraded_ms = degraded_ms

    @property
    def name(self) -> str:
        return "postgres"

    async def check(self) -> ComponentHealth:
        start = time.monotonic()
        try:
            async with self._db.session() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            return ComponentHealth(name=self.name, status=HealthStatus.UNHEALTHY, detail=str(exc))
        latency_ms = (time.monotonic() - start) * 1000
        status = HealthStatus.DEGRADED if latency_ms > self._degraded_ms else HealthStatus.HEALTHY
        return ComponentHealth(name=self.name, status=status, detail=f"{latency_ms:.1f}ms")


class RedisProbe:
    """Checks Redis is reachable (PING)."""

    def __init__(self, provider: RedisProvider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        return "redis"

    async def check(self) -> ComponentHealth:
        ok = await self._provider.ping()
        status = HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY
        return ComponentHealth(name=self.name, status=status, detail="ping" if ok else "no reply")


class ClickHouseProbe:
    """Checks ClickHouse when the analytics layer is enabled (else healthy)."""

    def __init__(self, provider: ClickHouseProvider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        return "clickhouse"

    async def check(self) -> ComponentHealth:
        if not self._provider.enabled:
            return ComponentHealth(name=self.name, status=HealthStatus.HEALTHY, detail="disabled")
        ok = await self._provider.ping()
        status = HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY
        return ComponentHealth(name=self.name, status=status)


# The HTTP-based probes are rebuilt on every ``/health`` request, so they cannot
# own a connection pool themselves — it would be discarded before the second use
# and leak the socket. They borrow the process-wide client from the Container
# instead. Passing ``None`` falls back to a client per check: correct, just slow,
# which keeps these constructible in a test without wiring a container.


class HttpProbe:
    """Generic HTTP GET probe (used for the API and dashboard endpoints)."""

    def __init__(
        self,
        name: str,
        url: str,
        *,
        timeout_seconds: float = 5.0,
        degraded_ms: float = 1000.0,
        http: HttpClientProvider | None = None,
    ) -> None:
        self._name = name
        self._url = url
        self._timeout = timeout_seconds
        self._degraded_ms = degraded_ms
        self._http = http

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> ComponentHealth:
        start = time.monotonic()
        try:
            if self._http is not None:
                resp = await self._http.acquire().get(self._url)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(self._url)
        except Exception as exc:  # a probe must never raise
            if self._http is not None:
                self._http.discard()
            return ComponentHealth(name=self._name, status=HealthStatus.UNHEALTHY, detail=str(exc))
        latency_ms = (time.monotonic() - start) * 1000
        if resp.status_code >= 500:
            return ComponentHealth(
                name=self._name, status=HealthStatus.UNHEALTHY, detail=f"HTTP {resp.status_code}"
            )
        status = HealthStatus.DEGRADED if latency_ms > self._degraded_ms else HealthStatus.HEALTHY
        return ComponentHealth(name=self._name, status=status, detail=f"{latency_ms:.0f}ms")


class RpcProbe:
    """Checks the Solana RPC endpoint via ``getHealth``."""

    def __init__(
        self,
        http_url: str,
        *,
        timeout_seconds: float = 15.0,
        latency_max_ms: float = 2000.0,
        http: HttpClientProvider | None = None,
    ) -> None:
        self._url = http_url
        self._timeout = timeout_seconds
        self._latency_max_ms = latency_max_ms
        self._http = http

    @property
    def name(self) -> str:
        return "rpc"

    async def check(self) -> ComponentHealth:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        start = time.monotonic()
        try:
            if self._http is not None:
                resp = await self._http.acquire().post(self._url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # a probe must never raise
            if self._http is not None:
                self._http.discard()
            return ComponentHealth(name=self.name, status=HealthStatus.UNHEALTHY, detail=str(exc))
        latency_ms = (time.monotonic() - start) * 1000
        status = (
            HealthStatus.DEGRADED if latency_ms > self._latency_max_ms else HealthStatus.HEALTHY
        )
        return ComponentHealth(name=self.name, status=status, detail=f"{latency_ms:.0f}ms")


class SystemResourceProbe:
    """Checks host CPU / memory / disk against configured ceilings."""

    def __init__(
        self,
        *,
        cpu_max_pct: float = 90.0,
        memory_max_pct: float = 90.0,
        disk_max_pct: float = 90.0,
        disk_path: str = "/",
    ) -> None:
        self._cpu_max = cpu_max_pct
        self._mem_max = memory_max_pct
        self._disk_max = disk_max_pct
        self._disk_path = disk_path

    @property
    def name(self) -> str:
        return "resources"

    async def check(self) -> ComponentHealth:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(self._disk_path).percent
        detail = f"cpu={cpu:.0f}% mem={mem:.0f}% disk={disk:.0f}%"
        if cpu >= self._cpu_max or mem >= self._mem_max or disk >= self._disk_max:
            return ComponentHealth(name=self.name, status=HealthStatus.DEGRADED, detail=detail)
        return ComponentHealth(name=self.name, status=HealthStatus.HEALTHY, detail=detail)

    def sample(self) -> dict[str, float]:
        """Raw resource sample (for metrics / health-check rows)."""
        return {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "memory_pct": psutil.virtual_memory().percent,
            "disk_pct": psutil.disk_usage(self._disk_path).percent,
        }
