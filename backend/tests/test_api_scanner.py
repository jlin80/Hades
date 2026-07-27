"""Scanner API endpoint: shape + degrades gracefully without Redis/DB."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hades.api.app import create_app


def test_scanner_status_shape() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/v1/scanner/status").json()
        # Discovery-only fields — never risk/scoring.
        assert "enabled" in body
        assert "running" in body
        assert "configured_sources" in body
        assert "tokens_total" in body
        assert "anomalies_total" in body
        assert "live" in body
        # No risk information is exposed by the scanner surface.
        assert "risk" not in body
        assert "score" not in body


def test_anomalies_endpoint_answers_without_a_database() -> None:
    """The status endpoint reports `anomalies_total`, which tells an operator a
    number is large without telling them whether it matters. This endpoint exposes
    the kind, field and subject so the count becomes actionable — and, like every
    dashboard endpoint, it degrades to empty rather than failing when the database
    is unreachable."""
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/scanner/anomalies")

        assert resp.status_code == 200
        body = resp.json()
        assert body["anomalies"] == []
        assert body["by_kind"] == {}


def test_anomalies_endpoint_bounds_the_page_size() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/scanner/anomalies?limit=500").status_code == 200
        assert client.get("/api/v1/scanner/anomalies?limit=501").status_code == 422
        assert client.get("/api/v1/scanner/anomalies?limit=0").status_code == 422
