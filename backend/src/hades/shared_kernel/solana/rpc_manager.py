"""Multi-provider Solana RPC manager with health-aware routing and failover.

Design goals (it must run for years without degrading):

* **Never depend on one provider.** Any number of endpoints are configured; the
  manager routes each call through the currently healthiest one.
* **Measure constantly.** Every call updates the chosen endpoint's latency
  (EWMA) and failure streak. A provider that starts failing is demoted; one that
  recovers is promoted again after a probe window.
* **Failover automatically.** A failed call is retried on the next-best provider,
  up to ``max_attempts`` across providers, before surfacing an error.
* **Respect rate limits.** Each provider has a per-second cap; when it would be
  exceeded the manager skips to another provider instead of blowing the quota.
* **Announce switches.** Whenever the active provider changes the manager invokes
  an injected ``on_switch`` callback (contexts turn this into a domain event) and
  records it in metrics + logs.

The manager is transport-agnostic: it is given an :class:`RpcTransport` (the
default wraps ``httpx``); tests inject a fake, so no network is needed to verify
the routing/health/failover logic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx

from hades.shared_kernel.config.settings import RpcEndpointConfig, RpcSettings
from hades.shared_kernel.errors.exceptions import InfrastructureError
from hades.shared_kernel.logging import describe, get_logger
from hades.shared_kernel.observability import MetricsRegistry

_logger = get_logger("solana.rpc")

Clock = Callable[[], float]


class RpcUnavailableError(InfrastructureError):
    """Every configured provider failed (or is rate-limited) for one call."""


class EndpointStatus(StrEnum):
    """Coarse health state of a single provider."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # reachable but slow
    UNHEALTHY = "unhealthy"  # failing; parked until a recovery probe succeeds


@runtime_checkable
class RpcTransport(Protocol):
    """Sends one JSON-RPC POST to a URL and returns the decoded body.

    Implementations MUST raise on any transport/HTTP error so the manager can
    fail over; a JSON-RPC ``error`` object is returned as-is (the caller decides).
    """

    async def send(
        self, url: str, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]: ...


class HttpxRpcTransport:
    """Default transport — one shared ``httpx.AsyncClient`` with keep-alive."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(headers={"content-type": "application/json"})
        self._owns_client = client is None

    async def send(self, url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        resp = await self._client.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass
class RpcSwitch:
    """A record that the active provider changed (fed to ``on_switch``)."""

    previous: str | None
    current: str
    reason: str
    at: float


@dataclass
class _Window:
    """A fixed one-second rate-limit window for a provider."""

    second: int = 0
    count: int = 0


@dataclass
class RpcEndpointState:
    """Mutable runtime health of one provider. Pure data + derived score."""

    name: str
    http_url: str
    ws_url: str
    priority: int
    rate_limit_per_second: int
    weight: float
    enabled: bool
    status: EndpointStatus = EndpointStatus.HEALTHY
    latency_ewma_ms: float = 0.0
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_rate_limited: int = 0
    last_ok_at: float = 0.0
    last_failure_at: float = 0.0
    _window: _Window = field(default_factory=_Window)

    def health_score(self, *, degraded_ms: float) -> float:
        """A 0-100 score (times weight) used to rank providers. 0 when unhealthy."""
        if self.status is EndpointStatus.UNHEALTHY or not self.enabled:
            return 0.0
        latency_penalty = min(60.0, (self.latency_ewma_ms / degraded_ms) * 40.0)
        failure_penalty = min(30.0, self.consecutive_failures * 15.0)
        base = max(1.0, 100.0 - latency_penalty - failure_penalty)
        return base * max(0.0, self.weight)

    def snapshot(self, *, degraded_ms: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "priority": self.priority,
            "latency_ms": round(self.latency_ewma_ms, 1),
            "health_score": round(self.health_score(degraded_ms=degraded_ms), 1),
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_rate_limited": self.total_rate_limited,
            "enabled": self.enabled,
        }


def _states_from_config(
    endpoints: Sequence[RpcEndpointConfig],
) -> list[RpcEndpointState]:
    return [
        RpcEndpointState(
            name=e.name,
            http_url=e.http_url,
            ws_url=e.ws_url,
            priority=e.priority,
            rate_limit_per_second=max(1, e.rate_limit_per_second),
            weight=e.weight,
            enabled=e.enabled,
        )
        for e in endpoints
    ]


class RpcManager:
    """Routes JSON-RPC calls across providers by live health, with failover."""

    def __init__(
        self,
        endpoints: Sequence[RpcEndpointConfig],
        *,
        transport: RpcTransport | None = None,
        settings: RpcSettings | None = None,
        metrics: MetricsRegistry | None = None,
        on_switch: Callable[[RpcSwitch], Awaitable[None]] | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        if not endpoints:
            raise ValueError("RpcManager requires at least one endpoint")
        cfg = settings or RpcSettings()
        self._states = _states_from_config(endpoints)
        self._by_name = {s.name: s for s in self._states}
        self._transport = transport or HttpxRpcTransport()
        self._timeout = cfg.request_timeout_seconds
        self._max_attempts = max(1, cfg.max_attempts)
        self._alpha = cfg.latency_ewma_alpha
        self._unhealthy_after = cfg.unhealthy_after_failures
        self._recovery_seconds = cfg.recovery_probe_seconds
        self._degraded_ms = cfg.degraded_latency_ms
        self._on_switch = on_switch
        self._clock = clock
        self._active: str | None = None
        self._request_id = 0
        self._metrics = metrics
        if metrics is not None:
            self._m_calls = metrics.counter(
                "hades_rpc_calls_total",
                "RPC calls by endpoint and outcome",
                ("endpoint", "outcome"),
            )
            self._m_switches = metrics.counter(
                "hades_rpc_switches_total", "Active RPC endpoint switches"
            )
            self._m_latency = metrics.gauge(
                "hades_rpc_latency_ms", "EWMA latency per endpoint (ms)", ("endpoint",)
            )
            self._m_health = metrics.gauge(
                "hades_rpc_endpoint_health", "Endpoint health score 0-100", ("endpoint",)
            )

    # -- public API -----------------------------------------------------------

    @property
    def active_endpoint(self) -> str | None:
        """Name of the provider the last successful call used."""
        return self._active

    def endpoints(self) -> list[dict[str, Any]]:
        """Health snapshot of every provider (for the dashboard / metrics)."""
        return [s.snapshot(degraded_ms=self._degraded_ms) for s in self._states]

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Issue one JSON-RPC ``method`` call, failing over across providers.

        Returns the JSON-RPC ``result``. Raises :class:`RpcUnavailableError` when
        every provider fails or is rate-limited within ``max_attempts``.
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }
        last_error: str = "no eligible endpoint"
        attempts = 0
        rate_limited_only = True
        for state in self._ranked_candidates():
            if attempts >= self._max_attempts:
                break
            if not self._admit(state):
                state.total_rate_limited += 1
                self._count(state, "rate_limited")
                last_error = f"{state.name} rate-limited"
                continue
            rate_limited_only = False
            attempts += 1
            ok, value, error = await self._attempt(state, method, payload)
            if ok:
                await self._mark_active(state, reason=f"call:{method}")
                return value
            last_error = error

        if rate_limited_only:
            # Everything was throttled; brief pause then one more full pass.
            await asyncio.sleep(0.05)
            for state in self._ranked_candidates():
                if self._admit(state):
                    ok, value, error = await self._attempt(state, method, payload)
                    if ok:
                        await self._mark_active(state, reason=f"call:{method}")
                        return value
                    last_error = error
                    break
        raise RpcUnavailableError(
            f"all RPC providers failed for {method}: {last_error}",
            context={"method": method, "attempts": attempts},
        )

    async def health_check(self) -> bool:
        """Probe the healthiest provider with ``getHealth`` (used by watchdog)."""
        try:
            await self.call("getHealth")
            return True
        except RpcUnavailableError:
            return False

    async def aclose(self) -> None:
        close = getattr(self._transport, "aclose", None)
        if close is not None:
            await close()

    # -- internals ------------------------------------------------------------

    def _ranked_candidates(self) -> list[RpcEndpointState]:
        now = self._clock()
        eligible = [s for s in self._states if s.enabled and self._selectable(s, now)]
        eligible.sort(key=lambda s: (-s.health_score(degraded_ms=self._degraded_ms), s.priority))
        return eligible

    def _selectable(self, state: RpcEndpointState, now: float) -> bool:
        """Unhealthy providers are parked until a recovery-probe window elapses."""
        if state.status is not EndpointStatus.UNHEALTHY:
            return True
        return (now - state.last_failure_at) >= self._recovery_seconds

    def _admit(self, state: RpcEndpointState) -> bool:
        """Fixed-window per-second rate limit; True if a call is allowed now."""
        second = int(self._clock())
        window = state._window
        if window.second != second:
            window.second = second
            window.count = 0
        if window.count >= state.rate_limit_per_second:
            return False
        window.count += 1
        return True

    async def _attempt(
        self, state: RpcEndpointState, method: str, payload: dict[str, Any]
    ) -> tuple[bool, Any, str]:
        started = self._clock()
        state.total_calls += 1
        try:
            body = await self._transport.send(state.http_url, payload, timeout=self._timeout)
        except Exception as exc:  # any transport error → failover
            self._record_failure(state, str(exc))
            self._count(state, "error")
            _logger.warning(
                "rpc_call_failed", endpoint=state.name, method=method, error=describe(exc)
            )
            return False, None, str(exc)

        if isinstance(body, dict) and body.get("error") is not None:
            err = str(body["error"])
            self._record_failure(state, err)
            self._count(state, "rpc_error")
            return False, None, err

        latency_ms = (self._clock() - started) * 1000.0
        self._record_success(state, latency_ms)
        self._count(state, "ok")
        return True, body.get("result") if isinstance(body, dict) else body, ""

    def _record_success(self, state: RpcEndpointState, latency_ms: float) -> None:
        if state.latency_ewma_ms == 0.0:
            state.latency_ewma_ms = latency_ms
        else:
            state.latency_ewma_ms = (
                self._alpha * latency_ms + (1 - self._alpha) * state.latency_ewma_ms
            )
        state.consecutive_failures = 0
        state.last_ok_at = self._clock()
        prior = state.status
        state.status = (
            EndpointStatus.DEGRADED
            if state.latency_ewma_ms > self._degraded_ms
            else EndpointStatus.HEALTHY
        )
        if prior is EndpointStatus.UNHEALTHY:
            _logger.info("rpc_endpoint_recovered", endpoint=state.name)
        self._export(state)

    def _record_failure(self, state: RpcEndpointState, error: str) -> None:
        state.consecutive_failures += 1
        state.total_failures += 1
        state.last_failure_at = self._clock()
        if state.consecutive_failures >= self._unhealthy_after:
            if state.status is not EndpointStatus.UNHEALTHY:
                _logger.warning("rpc_endpoint_unhealthy", endpoint=state.name, error=error)
            state.status = EndpointStatus.UNHEALTHY
        self._export(state)

    async def _mark_active(self, state: RpcEndpointState, *, reason: str) -> None:
        if self._active == state.name:
            return
        switch = RpcSwitch(
            previous=self._active, current=state.name, reason=reason, at=self._clock()
        )
        self._active = state.name
        if self._metrics is not None:
            self._m_switches.inc()
        _logger.info("rpc_endpoint_switched", previous=switch.previous, current=switch.current)
        if self._on_switch is not None:
            try:
                await self._on_switch(switch)
            except Exception as exc:  # a listener must never break routing
                _logger.error("rpc_on_switch_failed", error=str(exc))

    def _count(self, state: RpcEndpointState, outcome: str) -> None:
        if self._metrics is not None:
            self._m_calls.labels(endpoint=state.name, outcome=outcome).inc()

    def _export(self, state: RpcEndpointState) -> None:
        if self._metrics is not None:
            self._m_latency.labels(endpoint=state.name).set(state.latency_ewma_ms)
            self._m_health.labels(endpoint=state.name).set(
                state.health_score(degraded_ms=self._degraded_ms)
            )


def build_endpoints(settings: RpcSettings, fallback_http_url: str) -> list[RpcEndpointConfig]:
    """Resolve the provider list, always guaranteeing at least one endpoint.

    Uses the explicitly configured ``RPC_ENDPOINTS`` when present; otherwise
    synthesises a single default provider from ``SOLANA_RPC_HTTP_URL`` so the
    platform can always start.
    """
    enabled = [e for e in settings.endpoints if e.enabled]
    if enabled:
        return enabled
    return [RpcEndpointConfig(name="default", http_url=fallback_http_url, priority=100)]
