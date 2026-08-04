"""Pipeline funnel endpoint: shape, bounds, and the diagnosis it derives.

The funnel exists to answer "the bot runs but the portfolio never moves", which
no single context could answer about itself. ``_diagnose`` is the part worth
testing directly — it turns nine counters into the one sentence an operator
acts on, and getting the cliff wrong would point them at the wrong component.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from hades.api.app import create_app
from hades.api.routers.funnel import _diagnose, _stage


def test_funnel_degrades_to_empty_without_a_database() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/funnel")

        assert resp.status_code == 200
        body = resp.json()
        assert body["stages"] == []
        assert body["window_hours"] == 24
        assert "diagnosis" in body


def test_funnel_bounds_the_window() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/funnel?hours=720").status_code == 200
        assert client.get("/api/v1/funnel?hours=721").status_code == 422
        assert client.get("/api/v1/funnel?hours=0").status_code == 422


def _stages(**counts: int) -> list[dict[str, object]]:
    order = [
        ("discovered", "Tokens discovered by the Scanner"),
        ("features", "Reached the Feature Engine"),
        ("security_assessed", "Assessed by Security"),
        ("security_approved", "Passed Security"),
        ("committee_predicted", "Scored by the AI Committee"),
        ("risk_evaluated", "Reached the Risk Manager"),
        ("risk_approved", "Approved by Risk"),
        ("orders_filled", "Orders filled by Execution"),
        ("positions_opened", "Positions opened"),
    ]
    return [_stage(key, label, counts.get(key, 0)) for key, label in order]


def test_diagnosis_names_the_stage_where_the_funnel_collapses() -> None:
    stages = _stages(
        discovered=2500,
        features=2400,
        security_assessed=2400,
        security_approved=0,
    )
    assert "Passed Security" in _diagnose(stages, {}, 0)


def test_diagnosis_surfaces_the_top_rejection_reason_at_the_risk_gate() -> None:
    stages = _stages(
        discovered=2500,
        features=2400,
        security_assessed=2400,
        security_approved=800,
        committee_predicted=800,
        risk_evaluated=800,
        risk_approved=0,
    )
    reasons = {"conviction_too_low": 700, "exposure_cap": 100}

    diagnosis = _diagnose(stages, reasons, 0)

    # "Risk rejected everything" is not actionable; which policy did it, is.
    assert "conviction_too_low" in diagnosis
    assert "700" in diagnosis


def test_a_trading_pipeline_is_reported_as_healthy() -> None:
    stages = _stages(
        discovered=2500,
        features=2400,
        security_assessed=2400,
        security_approved=800,
        committee_predicted=800,
        risk_evaluated=800,
        risk_approved=12,
        orders_filled=12,
        positions_opened=12,
    )
    diagnosis = _diagnose(stages, {}, 3)
    assert "12 position(s) opened" in diagnosis
    assert "3 open now" in diagnosis


def test_an_idle_scanner_points_at_the_sources_not_at_trading() -> None:
    assert "Scanner's sources" in _diagnose(_stages(), {}, 0)


def test_an_empty_funnel_names_the_bus_before_the_scanner() -> None:
    """The regression this endpoint itself caused.

    The funnel reported zero at all nine stages and told the operator to look at
    the Scanner's sources. The Scanner was publishing normally — its events were
    landing in a stream nobody consumed, and the platform stayed in that state
    for four days. Every counter here is written by a handler on the far side of
    the bus, so a stopped consumer empties the funnel from the top and is
    indistinguishable from a Scanner that found nothing.
    """
    diagnosis = _diagnose(_stages(), {}, 0, stalled_groups=["watchdog", "worker"])

    assert "not being consumed" in diagnosis
    assert "worker" in diagnosis
    assert "Scanner's sources" not in diagnosis, (
        "naming the Scanner here sends the operator to the wrong component"
    )


def test_an_empty_funnel_still_names_the_scanner_when_the_bus_is_fine() -> None:
    """The bus check must narrow the diagnosis, never replace it."""
    assert "Scanner's sources" in _diagnose(_stages(), {}, 0, stalled_groups=[])


def test_the_bus_check_does_not_override_a_real_cliff() -> None:
    """A stalled group is only the answer when nothing was discovered at all."""
    stages = _stages(discovered=100, features=100, security_assessed=100)

    diagnosis = _diagnose(stages, {}, 0, stalled_groups=["worker"])

    assert "not being consumed" not in diagnosis
    assert "Passed Security" in diagnosis
