"""The API boots and its health/meta endpoints answer."""

from __future__ import annotations

from unittest import mock

import httpx
from fastapi.testclient import TestClient

from hades.api.app import create_app


def _via(client: TestClient):
    """Route the healthcheck's httpx.get through the TestClient."""
    return lambda url, **kwargs: client.get("/health")


def test_health_endpoint() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/health")
        # Liveness answers 200 whatever the dependencies are doing: this endpoint
        # is Docker's restart trigger, and a Redis blip must not recycle a
        # perfectly healthy API. Dependency state is reported, not enforced here.
        assert resp.status_code == 200
        body = resp.json()
        # Safety: a fresh, unconfigured instance must never report live trading.
        assert body["is_live"] is False

        components = {c["name"]: c for c in body["components"]}
        assert components["api"]["status"] == "healthy"


def test_health_reports_dependencies_and_processes_not_just_itself() -> None:
    # The old endpoint hardcoded a single "api" component, so the page read
    # "healthy" even with Postgres, Redis and the Worker all down.
    with TestClient(create_app()) as client:
        components = {c["name"]: c for c in client.get("/health").json()["components"]}

    assert "redis" in components
    # Background processes are reported from their heartbeat files. None runs in
    # a test process, and "not deployed here" must read as unknown, not as broken.
    assert components["worker"]["status"] == "unknown"
    assert "no heartbeat file" in components["worker"]["detail"]


def test_readiness_gates_on_dependencies_unlike_liveness() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/ready")

    # No Redis/Postgres in a bare test process, so readiness must refuse traffic
    # even though liveness stays 200.
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


def test_meta_lists_contexts() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/meta")
        assert resp.status_code == 200
        body = resp.json()
        assert "scanner" in body["contexts"]
        assert "execution" in body["contexts"]
        assert body["version"]


def test_container_healthcheck_ignores_dependency_failures() -> None:
    """Docker restarts the API when this check fails, so it must key off the one
    thing a restart can fix: this process. /health aggregates Postgres, Redis, RPC
    and every background process — a restart repairs none of them, and keying off
    that aggregate would put the API in a restart loop during a Redis blip."""
    from hades.ops.healthcheck import _check_http

    with TestClient(create_app()) as client:
        # No Postgres/Redis/RPC in a test process, so the aggregate is unhealthy.
        assert client.get("/health").json()["status"] == "unhealthy"

        # httpx is imported inside _check_http (keeping the liveness path cheap), so
        # the patch targets the module itself rather than an attribute of this one.
        with mock.patch("httpx.get", side_effect=_via(client)):
            assert _check_http() == 0


def test_container_healthcheck_fails_when_the_api_itself_cannot_answer() -> None:
    from hades.ops.healthcheck import _check_http

    with mock.patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        assert _check_http() == 1
