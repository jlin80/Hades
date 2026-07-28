"""The risk + portfolio read endpoints answer and degrade without Redis/DB.

A fresh instance has no Redis snapshot and no database; the endpoints must still
render (empty/default payloads) rather than error - the dashboard has to load.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from hades.api.app import create_app


def test_risk_status_answers() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/risk/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "enabled" in body
        assert body["approvals_total"] == 0
        assert body["rejections_total"] == 0


def test_kill_switch_endpoint_defaults() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/risk/kill-switch").json()
        assert body["level"] == 0
        assert body["label"] == "none"
        assert body["levels"]["halt_after"] == 5


def test_circuit_breaker_endpoint_defaults() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/risk/circuit-breaker").json()
        assert body["open"] is False
        assert body["reasons"] == []


def test_exposure_and_drawdown_endpoints() -> None:
    with TestClient(create_app()) as client:
        exp = client.get("/api/v1/risk/exposure").json()
        assert exp["caps"]["max_portfolio_pct"] == 50
        dd = client.get("/api/v1/risk/drawdown").json()
        assert dd["limits"]["daily_pct"] == 10.0


def test_risk_decisions_empty_without_db() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/risk/decisions").json()
        assert body["decisions"] == []


def test_portfolio_endpoints_answer() -> None:
    with TestClient(create_app()) as client:
        pf = client.get("/api/v1/portfolio").json()
        assert pf["running"] is False
        cap = client.get("/api/v1/portfolio/capital").json()
        assert "available_usd" in cap
        analytics = client.get("/api/v1/portfolio/analytics").json()
        assert "metrics" in analytics


def test_open_positions_endpoint_answers_without_a_worker() -> None:
    """`invested_usd` says capital left cash; this says which tokens hold it.

    The dashboard could report "1 open position" with no way to learn which
    token it was — the API had no endpoint for the open book at all. Like every
    dashboard endpoint it degrades to empty rather than failing when the
    Worker's snapshot is absent.
    """
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/portfolio/positions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["positions"] == []
        assert body["open_positions"] == 0
