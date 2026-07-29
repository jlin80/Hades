"""Inbox ingestion — the sweep, and its filesystem audit trail."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hades.contexts.knowledge.application.ingest_service import (
    ACCEPTED_DIR,
    REJECTED_DIR,
    KnowledgeIngestService,
)
from hades.contexts.knowledge.application.recorder import KnowledgeRecorder
from hades.contexts.knowledge.domain.bundles import BUNDLE_FORMAT, canonical_checksum
from hades.contexts.knowledge.domain.models import Verification
from hades.contexts.knowledge.infrastructure.stores import InMemoryKnowledgeStore

_AT = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)


def _bundle(bundle_id: str = "b-1", *, records: int = 1) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "bundle_format": BUNDLE_FORMAT,
        "bundle_id": bundle_id,
        "produced_by": "hades-research-lab",
        "produced_at": _AT.isoformat(),
        "notes": "",
        "records": [
            {
                "source": "backtest",
                "kind": "simulation",
                "subject": f"strategy_{index}",
                "subject_type": "strategy",
                "occurred_at": _AT.isoformat(),
                "payload": {"total_return": 0.3},
                "features": {"sharpe": 1.2},
                "correlation_id": None,
            }
            for index in range(records)
        ],
    }
    payload["checksum"] = canonical_checksum(payload)
    return payload


def _write(directory: Path, name: str, payload: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _service(inbox: Path) -> tuple[KnowledgeIngestService, InMemoryKnowledgeStore]:
    store = InMemoryKnowledgeStore()
    return KnowledgeIngestService(KnowledgeRecorder(store), inbox), store


@pytest.mark.asyncio
async def test_a_missing_inbox_is_not_an_error(tmp_path: Path) -> None:
    """The normal state of a deployment that has never been handed a bundle."""
    service, store = _service(tmp_path / "nope")
    report = await service.import_all()
    assert report.outcomes == ()
    assert store.rows == []


@pytest.mark.asyncio
async def test_a_valid_bundle_is_recorded_and_filed_under_accepted(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", _bundle(records=3))
    service, store = _service(tmp_path)

    report = await service.import_all()

    assert len(report.accepted) == 1
    assert report.records_imported == 3
    assert len(store.rows) == 3
    assert {row.verification for row in store.rows} == {Verification.SIMULATED}
    # The artifact survives next to the decision, for anyone auditing later.
    assert list((tmp_path / ACCEPTED_DIR).glob("*.json"))
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_a_malformed_bundle_is_filed_under_rejected_with_its_reason(
    tmp_path: Path,
) -> None:
    """Rejections are kept, not deleted: the failing file is the one you need."""
    bad = _bundle()
    bad["records"][0]["source"] = "executed_trade"
    # Re-checksummed on purpose: this must fail on the source allowlist, not on
    # integrity. A well-formed file making a forbidden claim is the case worth
    # pinning — a corrupt one is caught by a much earlier and dumber check.
    bad["checksum"] = canonical_checksum(bad)
    _write(tmp_path, "bad.json", bad)
    service, store = _service(tmp_path)

    report = await service.import_all()

    assert len(report.rejected) == 1
    assert "not accepted from an external producer" in (report.rejected[0].reason or "")
    assert store.rows == []
    assert list((tmp_path / REJECTED_DIR).glob("*.json"))


@pytest.mark.asyncio
async def test_unreadable_json_is_an_outcome_not_a_crash(tmp_path: Path) -> None:
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "truncated.json").write_text('{"bundle_format": ', encoding="utf-8")
    service, _ = _service(tmp_path)

    report = await service.import_all()

    assert len(report.rejected) == 1
    assert "unreadable" in (report.rejected[0].reason or "")


@pytest.mark.asyncio
async def test_one_bad_bundle_does_not_abort_the_sweep(tmp_path: Path) -> None:
    """A sweep that stops halfway leaves the operator unable to tell what landed."""
    _write(tmp_path, "1-good.json", _bundle("good-1"))
    _write(tmp_path, "2-bad.json", {"bundle_format": "nope"})
    _write(tmp_path, "3-good.json", _bundle("good-2"))
    service, store = _service(tmp_path)

    report = await service.import_all()

    assert len(report.accepted) == 2
    assert len(report.rejected) == 1
    assert len(store.rows) == 2


@pytest.mark.asyncio
async def test_processed_subdirectories_are_never_re_swept(tmp_path: Path) -> None:
    """Otherwise every sweep would re-import the entire history of the inbox."""
    _write(tmp_path, "a.json", _bundle())
    service, store = _service(tmp_path)

    await service.import_all()
    second = await service.import_all()

    assert second.outcomes == ()
    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_a_store_failure_files_the_bundle_for_retry(tmp_path: Path) -> None:
    """Not the bundle's fault. It goes to rejected/ so an operator can move it
    back once the platform is healthy, rather than vanishing unwritten."""

    class Broken(InMemoryKnowledgeStore):
        async def append(self, observations: object) -> None:  # type: ignore[override]
            raise RuntimeError("database is down")

    _write(tmp_path, "a.json", _bundle())
    service = KnowledgeIngestService(KnowledgeRecorder(Broken()), tmp_path)

    report = await service.import_all()

    assert len(report.rejected) == 1
    assert "recording failed" in (report.rejected[0].reason or "")
    assert list((tmp_path / REJECTED_DIR).glob("*.json"))
