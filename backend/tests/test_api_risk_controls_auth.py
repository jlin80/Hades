"""The defence layer's control endpoints are gated, not merely labelled gated.

All five carried "(human-gated)" in their OpenAPI summary and none of them
depended on ``get_principal``. On the live deployment ``API_BIND`` is the host's
LAN address and the api container publishes 8000 on it, so the kill switch,
circuit breaker and emergency mode were reachable from any machine on the network
with no credential at all.

Turning ``API_AUTH_ENABLED`` on would not have closed it either: a router that
never asks for a principal is unaffected by the switch that decides what a
principal must prove. That is the property these tests pin -- a gate that only
exists in a docstring is the kind this project's own log keeps finding.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.shared_kernel.config.settings import get_settings

_KEY = "test-operator-key-not-a-real-secret"

#: Lifting a protection re-exposes capital, so it needs a real operator.
_LIFTING = [
    "/api/v1/risk/kill-switch/reset",
    "/api/v1/risk/circuit-breaker/reset",
    "/api/v1/risk/emergency/exit",
]
#: Raising one only ever stops trading, so the implicit principal may do it.
_RAISING = [
    "/api/v1/risk/circuit-breaker/trip",
    "/api/v1/risk/emergency/enter",
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


@pytest.mark.parametrize("path", _LIFTING)
def test_lifting_a_protection_is_refused_without_an_operator(path: str) -> None:
    """Auth off is not consent. The implicit `system` principal is not an operator."""
    with _client() as client:
        resp = client.post(path)
        assert resp.status_code == 403, f"{path} answered {resp.status_code}"


@pytest.mark.parametrize("path", _RAISING)
def test_raising_a_protection_stays_available(path: str) -> None:
    """A halt is conservative; gating it behind a credential is the wrong trade.

    An operator watching the platform misbehave must be able to stop it without
    first finding an API key.

    Asserted as "not refused" rather than "200": a control action publishes a
    ``RiskControlCommandIssued`` for the Worker to act on, and there is no Redis
    in the unit environment. The property under test is the authorisation
    decision, which happens before any of that, so pinning the transport here
    would only make the test fail for a reason it is not about.
    """
    with _client() as client:
        resp = client.post(path)
        assert resp.status_code != 403, f"{path} was refused for the implicit principal"


@pytest.mark.parametrize("path", _LIFTING)
def test_lifting_is_refused_with_a_wrong_key(path: str, auth_on: None) -> None:
    with _client() as client:
        resp = client.post(path, headers={"X-API-Key": "not-the-key"})
        assert resp.status_code == 403


@pytest.mark.parametrize("path", _LIFTING)
def test_lifting_succeeds_for_an_authenticated_operator(path: str, auth_on: None) -> None:
    """The gate opens for a real operator — see the note above on "not 403"."""
    with _client() as client:
        resp = client.post(path, headers={"X-API-Key": _KEY})
        assert resp.status_code != 403, resp.text
