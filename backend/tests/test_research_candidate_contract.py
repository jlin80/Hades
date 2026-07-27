"""Cross-repository contract test: real lab output must import into this Core.

The fixtures in ``tests/fixtures/research_lab/`` were produced by the actual
``CandidateExporter`` in the Hades Research Lab repository — they are not
hand-written by hand-copying this parser's expectations. Since the two systems
never import each other, this is the only place a silent divergence between the
lab's writer and Core's reader can be caught, and it is why the fixtures are
committed rather than generated at test time.

If one of these ever fails, the two sides of ``hades.candidate/v1`` have drifted.
The fix is to reconcile the contract on both sides and regenerate the fixtures —
never to loosen the parser so a malformed bundle slips through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hades.contexts.learning.application.candidate_import import bundle_to_card
from hades.contexts.learning.domain.candidates import (
    BUNDLE_FORMAT,
    META_HEADS,
    parse_candidate_bundle,
)
from hades.contexts.learning.domain.models import META_MODEL_NAME, ModelStatus

_FIXTURES = Path(__file__).parent / "fixtures" / "research_lab"


def _load(name: str) -> dict[str, object]:
    payload = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_a_real_specialist_bundle_from_the_lab_is_accepted() -> None:
    bundle = parse_candidate_bundle(_load("specialist_liquidity.json"))

    assert bundle.name == "liquidity"
    assert bundle.produced_by == "hades-research-lab"
    assert bundle.dataset_id == "ds-2026-07"
    assert bundle.trained_on_samples == 1200
    assert bundle.metrics.auc == pytest.approx(0.72)
    assert bundle.weights.coefficients["liquidity_usd"] == pytest.approx(1.21)
    # Evidence travels for the human reviewer; it is not a registry metric.
    assert bundle.evidence["walkforward_folds"] == 5


def test_a_real_meta_bundle_from_the_lab_is_accepted() -> None:
    bundle = parse_candidate_bundle(_load("meta_model.json"))

    assert bundle.is_meta
    assert bundle.name == META_MODEL_NAME
    assert sorted(bundle.heads) == sorted(META_HEADS)


def test_both_sides_agree_on_the_format_identifier() -> None:
    for fixture in ("specialist_liquidity.json", "meta_model.json"):
        assert _load(fixture)["bundle_format"] == BUNDLE_FORMAT


def test_lab_output_becomes_an_unpromoted_card() -> None:
    card = bundle_to_card(parse_candidate_bundle(_load("specialist_liquidity.json")), "1")

    assert card.status is ModelStatus.TRAINED
    assert not card.is_active
    assert card.kind == "logistic"
    assert "research-lab-candidate" in card.notes


def test_the_meta_card_keeps_all_three_fusion_heads() -> None:
    card = bundle_to_card(parse_candidate_bundle(_load("meta_model.json")), "1")

    assert card.kind == "logistic_meta"
    assert sorted(card.heads) == sorted(META_HEADS)
    assert card.status is ModelStatus.TRAINED


def test_a_lab_bundle_carries_no_executable_artifact() -> None:
    # The lab exports weights, never a pickled estimator: Core must never have to
    # execute something produced in another repository.
    for fixture in _FIXTURES.glob("*"):
        assert fixture.suffix == ".json"
        assert isinstance(json.loads(fixture.read_text(encoding="utf-8")), dict)
