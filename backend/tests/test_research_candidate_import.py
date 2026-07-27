"""The Research Lab → Core bridge: bundle validation and human-gated import.

These tests pin the trust boundary. The external Research Lab is a separate repo
and a separate stack; the only thing that crosses is a JSON bundle. What must hold:

* a well-formed bundle becomes a ``TRAINED`` registry candidate — never promoted;
* anything the Core cannot fully vouch for is rejected, with the registry untouched;
* one bad bundle never blocks the good ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hades.contexts.learning.application.candidate_import import (
    ACCEPTED_DIR,
    REJECTED_DIR,
    CandidateImportService,
)
from hades.contexts.learning.application.metrics import LearningMetrics
from hades.contexts.learning.application.registry import ModelRegistryService
from hades.contexts.learning.domain.candidates import (
    BUNDLE_FORMAT,
    BUNDLE_KIND,
    CandidateRejectedError,
    canonical_checksum,
    parse_candidate_bundle,
)
from hades.contexts.learning.domain.models import META_MODEL_NAME, ModelStatus
from hades.contexts.learning.infrastructure.model_registry import InMemoryModelRegistry
from hades.shared_kernel.events import InMemoryEventBus
from hades.shared_kernel.observability import MetricsRegistry


def _bundle(**overrides: Any) -> dict[str, Any]:
    """A valid specialist bundle, checksummed last so overrides stay consistent."""
    payload: dict[str, Any] = {
        "bundle_format": BUNDLE_FORMAT,
        "kind": BUNDLE_KIND,
        "candidate_id": "cand-001",
        "produced_by": "hades-research-lab",
        "produced_at": "2026-07-25T00:00:00+00:00",
        "name": "liquidity",
        "model_kind": "logistic",
        "dataset_id": "ds-42",
        "feature_names": ["liquidity_usd", "lp_locked_pct"],
        "weights": {"bias": -0.4, "coefficients": {"liquidity_usd": 1.2, "lp_locked_pct": 0.7}},
        "metrics": {"samples": 800, "auc": 0.71, "brier": 0.19},
        "trained_on_samples": 800,
        "notes": "walk-forward validated",
        "evidence": {"walkforward_folds": 5},
    }
    payload.update(overrides)
    payload.pop("checksum", None)
    payload["checksum"] = canonical_checksum(payload)
    return payload


def _meta_bundle(**overrides: Any) -> dict[str, Any]:
    head = {"bias": 0.0, "coefficients": {"liquidity": 0.5, "momentum": 0.3}}
    fields: dict[str, Any] = {
        "name": META_MODEL_NAME,
        "feature_names": ["liquidity", "momentum"],
        "weights": head,
        "heads": {"roi_positive": head, "hit_tp": head, "hit_sl": head},
    }
    fields.update(overrides)
    return _bundle(**fields)


def _service(inbox: Path) -> tuple[CandidateImportService, InMemoryModelRegistry, InMemoryEventBus]:
    registry = InMemoryModelRegistry()
    bus = InMemoryEventBus()
    service = CandidateImportService(
        registry, ModelRegistryService(registry, bus, LearningMetrics(MetricsRegistry())), inbox
    )
    return service, registry, bus


def _write(inbox: Path, name: str, payload: Any) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- the contract -------------------------------------------------------------


def test_a_well_formed_specialist_bundle_parses() -> None:
    bundle = parse_candidate_bundle(_bundle())
    assert bundle.name == "liquidity"
    assert bundle.weights.coefficients["liquidity_usd"] == pytest.approx(1.2)
    assert bundle.feature_names == ("liquidity_usd", "lp_locked_pct")
    assert bundle.metrics.samples == 800
    assert not bundle.is_meta


def test_the_meta_bundle_must_carry_exactly_its_three_heads() -> None:
    assert parse_candidate_bundle(_meta_bundle()).is_meta
    with pytest.raises(CandidateRejectedError, match="heads"):
        parse_candidate_bundle(_meta_bundle(heads={"roi_positive": {"coefficients": {"a": 1.0}}}))


def test_only_the_meta_model_may_declare_heads() -> None:
    with pytest.raises(CandidateRejectedError, match="heads"):
        parse_candidate_bundle(_bundle(heads={"roi_positive": {"coefficients": {"a": 1.0}}}))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"bundle_format": "hades.candidate/v2"}, "bundle_format"),
        ({"kind": "strategy_params"}, "kind"),
        ({"name": "not_a_member"}, "name"),
        ({"model_kind": "xgboost"}, "model_kind"),
        ({"weights": {"bias": 0.0, "coefficients": {}}}, "coefficients"),
        ({"trained_on_samples": -1}, "trained_on_samples"),
        ({"candidate_id": ""}, "candidate_id"),
    ],
)
def test_bundles_the_core_cannot_vouch_for_are_rejected(
    overrides: dict[str, Any], match: str
) -> None:
    with pytest.raises(CandidateRejectedError, match=match):
        parse_candidate_bundle(_bundle(**overrides))


def test_non_finite_weights_are_rejected() -> None:
    # NaN/inf survive a JSON round-trip in Python and would poison every
    # prediction the model ever makes.
    payload = _bundle()
    payload["weights"]["coefficients"]["liquidity_usd"] = float("inf")
    payload["checksum"] = canonical_checksum(payload)
    with pytest.raises(CandidateRejectedError, match="finite"):
        parse_candidate_bundle(payload)


def test_a_tampered_bundle_fails_its_checksum() -> None:
    payload = _bundle()
    payload["weights"]["coefficients"]["liquidity_usd"] = 99.0
    with pytest.raises(CandidateRejectedError, match="checksum"):
        parse_candidate_bundle(payload)


def test_declared_features_must_match_the_coefficients() -> None:
    with pytest.raises(CandidateRejectedError, match="feature_names"):
        parse_candidate_bundle(_bundle(feature_names=["liquidity_usd"]))


def test_unknown_top_level_keys_are_rejected_rather_than_ignored() -> None:
    with pytest.raises(CandidateRejectedError, match="unknown keys"):
        parse_candidate_bundle(_bundle(surprise="payload"))


# -- the import ---------------------------------------------------------------


async def test_an_imported_candidate_is_registered_as_trained_never_promoted(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    _write(inbox, "cand.json", _bundle())
    service, registry, _ = _service(inbox)

    report = await service.import_all()

    assert len(report.accepted) == 1
    cards = await registry.versions("liquidity")
    assert len(cards) == 1
    card = cards[0]
    assert card.status is ModelStatus.TRAINED
    assert not card.is_active
    assert await registry.active("liquidity") is None
    assert await registry.list_active() == ()
    # Provenance is legible from the card alone.
    assert "research-lab-candidate:cand-001" in card.notes


async def test_an_import_emits_the_same_event_a_local_training_run_would(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    _write(inbox, "cand.json", _bundle())
    service, _, bus = _service(inbox)
    seen: list[str] = []
    bus.subscribe("ModelTrained", lambda e: seen.append(type(e).__name__))  # type: ignore[arg-type]

    await service.import_all()

    assert seen == ["ModelTrained"]


async def test_a_rejected_bundle_leaves_the_registry_untouched(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _write(inbox, "bad.json", _bundle(name="not_a_member"))
    service, registry, _ = _service(inbox)

    report = await service.import_all()

    assert len(report.rejected) == 1
    assert report.rejected[0].reason is not None
    assert await registry.list_active() == ()
    assert (inbox / REJECTED_DIR).exists()
    assert not list(inbox.glob("*.json"))


async def test_one_bad_bundle_does_not_block_the_good_ones(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _write(inbox, "a-bad.json", {"nonsense": True})
    _write(inbox, "b-good.json", _bundle())
    _write(inbox, "c-good-meta.json", _meta_bundle(candidate_id="cand-002"))
    service, registry, _ = _service(inbox)

    report = await service.import_all()

    assert len(report.accepted) == 2
    assert len(report.rejected) == 1
    assert len(await registry.versions("liquidity")) == 1
    assert len(await registry.versions(META_MODEL_NAME)) == 1
    assert (inbox / ACCEPTED_DIR).exists()


async def test_unparseable_json_is_a_rejection_not_a_crash(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "truncated.json").write_text("{not json", encoding="utf-8")
    service, _, _ = _service(inbox)

    report = await service.import_all()

    assert len(report.rejected) == 1
    assert "unreadable" in (report.rejected[0].reason or "")


async def test_successive_imports_append_versions_and_never_overwrite(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    service, registry, _ = _service(inbox)

    _write(inbox, "v1.json", _bundle())
    await service.import_all()
    _write(inbox, "v2.json", _bundle(candidate_id="cand-002"))
    await service.import_all()

    versions = sorted(c.version for c in await registry.versions("liquidity"))
    assert versions == ["1", "2"]


async def test_a_missing_inbox_is_a_no_op_not_an_error(tmp_path: Path) -> None:
    service, registry, _ = _service(tmp_path / "never-created")

    report = await service.import_all()

    assert report.outcomes == ()
    assert await registry.list_active() == ()
