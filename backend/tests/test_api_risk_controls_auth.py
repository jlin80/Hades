"""The defence layer's control endpoints obey API_AUTH_ENABLED.

All five carried "(human-gated)" in their OpenAPI summary and none of them
depended on ``get_principal``. That made the label false in a specific and
dangerous way: on the live deployment ``API_BIND`` is the host's LAN address and
the api container publishes 8000 on it, and turning ``API_AUTH_ENABLED`` on --
with a key already provisioned -- would not have changed anything here. A router
that never asks for a principal is unaffected by the switch that decides what a
principal must prove, so the flag bought the appearance of protection and none of
it.

The fix is not a stricter posture, it is an honest one: the flag now works. With
auth off (a single-operator LAN, which is this deployment) every endpoint behaves
exactly as before, and the dashboard keeps working untouched. With auth on they
require the key. The operator chooses; the code stops lying about which choice is
in effect.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.shared_kernel.config.settings import get_settings

_KEY = "test-operator-key-not-a-real-secret"

_CONTROLS = [
    "/api/v1/risk/kill-switch/reset",
    "/api/v1/risk/circuit-breaker/reset",
    "/api/v1/risk/circuit-breaker/trip",
    "/api/v1/risk/emergency/enter",
    "/api/v1/risk/emergency/exit",
]


def _client() -> TestClient:
    """A client that reports server errors as responses instead of raising.

    A control action publishes ``RiskControlCommandIssued`` and there is no Redis
    here, so the handler raises past the gate. These tests are about the gate, and
    a client that re-raises would hide the status code that answers the question.
    """
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("API_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_API_KEY", _KEY)
    # ``get_settings`` is ``lru_cache``d, so the environment alone is not enough:
    # without clearing it the app would be built from whatever the first test in
    # the session happened to load, and these tests would pass or fail on
    # ordering rather than on behaviour.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("path", _CONTROLS)
def test_controls_stay_open_while_auth_is_off(path: str) -> None:
    """The default posture is unchanged, which is the point.

    This deployment runs with auth off and a dashboard that sends no key. Closing
    these by default would have broken the Risk screen to enforce a policy the
    operator did not ask for.
    """
    with _client() as client:
        resp = client.post(path)
        assert resp.status_code != 403, f"{path} was refused with auth disabled"


@pytest.mark.parametrize("path", _CONTROLS)
def test_controls_require_the_key_once_auth_is_on(path: str, auth_on: None) -> None:
    """The regression that mattered: the flag used not to reach these at all."""
    with _client() as client:
        resp = client.post(path)
        assert resp.status_code == 403, f"{path} answered {resp.status_code} with no key"


@pytest.mark.parametrize("path", _CONTROLS)
def test_a_wrong_key_is_refused(path: str, auth_on: None) -> None:
    with _client() as client:
        resp = client.post(path, headers={"X-API-Key": "not-the-key"})
        assert resp.status_code == 403


@pytest.mark.parametrize("path", _CONTROLS)
def test_the_right_key_opens_the_gate(path: str, auth_on: None) -> None:
    """See the note in ``_client`` on asserting "not 403" rather than 200."""
    with _client() as client:
        resp = client.post(path, headers={"X-API-Key": _KEY})
        assert resp.status_code != 403, resp.text
