"""`lab_enabled` must describe the lab, not the process reporting on it.

The Worker hosts the Research Lab; the API only reads its published snapshot.
Both load `.env` independently, so sourcing `lab_enabled` from the API's own
settings answered a different question than the one asked. Enabling
`RESEARCH_LAB_ENABLED` and restarting only the Worker produced a response that
contradicted itself — `lab_enabled: false` next to `running: true` and a `live`
payload listing ten shadow strategies — and the dashboard rendered "Disabled
(RESEARCH_LAB_ENABLED=false)" over a lab that was visibly working. Seen on a
live deployment.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.api.routers.research import _lab_enabled


class _Container:
    """Just enough container to answer `settings.research.lab_enabled`."""

    def __init__(self, lab_enabled: bool) -> None:
        self.settings = type(
            "S", (), {"research": type("R", (), {"lab_enabled": lab_enabled})()}
        )()


def _snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "lab_enabled": True,
        "auto_research": True,
        "candidates": 10,
        "shadow_strategies": [],
        "historical_samples": 230,
    }
    return {**base, **overrides}


def test_the_workers_view_wins_over_the_apis_own_settings() -> None:
    """The exact production contradiction: worker restarted, API not."""
    result = _lab_enabled(_Container(lab_enabled=False), _snapshot())  # type: ignore[arg-type]

    assert result is True


def test_a_worker_reporting_disabled_is_also_believed() -> None:
    """Authority runs both ways — this is not a one-directional override."""
    result = _lab_enabled(_Container(lab_enabled=True), _snapshot(lab_enabled=False))  # type: ignore[arg-type]

    assert result is False


def test_without_a_snapshot_it_falls_back_to_local_settings() -> None:
    """No Worker reporting: local config is the only evidence there is."""
    assert _lab_enabled(_Container(lab_enabled=True), None) is True  # type: ignore[arg-type]
    assert _lab_enabled(_Container(lab_enabled=False), None) is False  # type: ignore[arg-type]


def test_a_snapshot_missing_the_field_falls_back_rather_than_guessing() -> None:
    """An older Worker's snapshot predates the field; absence is not `false`."""
    stale = _snapshot()
    del stale["lab_enabled"]

    assert _lab_enabled(_Container(lab_enabled=True), stale) is True  # type: ignore[arg-type]


def test_a_non_boolean_value_is_not_trusted() -> None:
    """A corrupt snapshot must not make a truthy string mean "enabled"."""
    result = _lab_enabled(_Container(lab_enabled=False), _snapshot(lab_enabled="yes"))  # type: ignore[arg-type]

    assert result is False


def test_status_endpoint_answers_without_a_worker() -> None:
    """With no Redis snapshot the endpoint still renders, reporting not-running."""
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/research/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert isinstance(body["lab_enabled"], bool)
        assert body["live"] == {}
