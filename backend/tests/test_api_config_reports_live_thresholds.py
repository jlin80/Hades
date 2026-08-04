"""The Configuration screen must report thresholds that actually decide.

`ScoringSettings` and `SecuritySettings` both carry a `min_security_score`, both
defaulting to 60. Only the second one is read: the `scoring` context is not
wired into any runtime — nothing constructs it and no pipeline stage consults
it — so its fields change nothing. `/api/v1/config` published the inert ones.

While the defaults agreed the two were indistinguishable. The moment an operator
tuned security, the screen would confirm a value that had no effect, and there
was nothing anywhere to reveal the mistake.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.shared_kernel.config import Settings


def _config() -> dict[str, object]:
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/config")
        assert resp.status_code == 200
        body = resp.json()
    assert isinstance(body, dict)
    return body


def test_the_reported_security_threshold_is_the_one_the_engine_enforces() -> None:
    settings = Settings()
    section = _config()["security"]

    assert isinstance(section, dict)
    assert section["min_security_score"] == settings.security.min_security_score


def test_the_inert_scoring_section_is_no_longer_published() -> None:
    """Publishing it invited operators to tune a setting nothing reads."""
    assert "scoring" not in _config()


def test_the_signal_gates_come_from_the_risk_manager() -> None:
    """These are the thresholds a candidate is actually measured against."""
    settings = Settings()
    gates = _config()["signal_gates"]

    assert isinstance(gates, dict)
    assert gates["min_prob_roi_positive"] == settings.risk.min_prob_roi_positive
    assert gates["min_confidence"] == settings.risk.min_confidence


def test_the_two_similarly_named_settings_are_still_distinct_objects() -> None:
    """A guard on the trap itself.

    If someone later collapses these into one setting the collision disappears
    and this test should be deleted. Until then it documents that
    `SCORING_MIN_SECURITY_SCORE` and `SECURITY_MIN_SECURITY_SCORE` are separate
    knobs whose defaults coincide — which is precisely why the bug was silent.
    """
    settings = Settings()

    assert hasattr(settings.scoring, "min_security_score")
    assert hasattr(settings.security, "min_security_score")
    assert settings.scoring.min_security_score == settings.security.min_security_score
