"""Importing Research Lab candidates — the Core's side of the one-way bridge.

The Research Lab writes candidate bundles into its own artifacts directory. An
operator carries them into the Core's **inbox** directory. This service reads that
inbox, validates each bundle against the contract in
:mod:`hades.contexts.learning.domain.candidates`, and appends the survivors to the
Model Registry as ``TRAINED`` candidates.

The properties that make this safe to run against production:

* **The Core pulls; the lab never pushes.** There is no network call, no shared
  database and no import of lab code. The lab cannot reach this module, and this
  module cannot reach the lab. Dropping a file in a directory is the entire
  protocol, so the isolation is a property of the deployment, not of etiquette.
* **Imported ≠ trusted.** Every bundle lands as ``TRAINED`` — the same status a
  locally-trained model gets before validation. It is *not* validated, *not*
  shadowed and emphatically *not* promoted. The existing human-gated ladder is
  unchanged and remains the only way a model becomes active.
* **Fail-closed and per-file.** A malformed bundle is rejected with its reason and
  moved aside; it never aborts the run and never half-registers. One bad file
  cannot block the good ones, and no file is deleted — rejects are kept for
  forensics exactly like the Security Engine keeps rejected verdicts.
* **Nothing here executes an artifact.** Bundles are JSON weight sets. There is no
  unpickling, no import, no eval.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hades.contexts.learning.application.registry import ModelRegistryService
from hades.contexts.learning.domain.candidates import (
    CandidateBundle,
    CandidateRejectedError,
    parse_candidate_bundle,
)
from hades.contexts.learning.domain.models import ModelCard, ModelStatus
from hades.contexts.learning.domain.ports import ModelRegistry
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.logging import get_logger

_logger = get_logger("committee.candidates.import")

#: Sub-directories of the inbox. Processed files are moved, never deleted, so the
#: import history is reconstructable from the filesystem alone.
ACCEPTED_DIR = "accepted"
REJECTED_DIR = "rejected"


@dataclass(frozen=True)
class ImportOutcome:
    """What happened to one bundle file."""

    path: str
    accepted: bool
    model_id: str | None = None
    name: str | None = None
    version: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ImportReport:
    """The result of one inbox sweep."""

    outcomes: tuple[ImportOutcome, ...] = ()

    @property
    def accepted(self) -> tuple[ImportOutcome, ...]:
        return tuple(o for o in self.outcomes if o.accepted)

    @property
    def rejected(self) -> tuple[ImportOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.accepted)


def bundle_to_card(bundle: CandidateBundle, version: str) -> ModelCard:
    """Translate a validated bundle into a registry card — always ``TRAINED``.

    The status is hardcoded rather than taken from the bundle: a candidate's own
    opinion of its readiness is evidence, never authority. Provenance is written
    into ``notes`` so a reviewer can always tell an imported model from a
    locally-trained one without consulting anything else.
    """
    provenance = f"research-lab-candidate:{bundle.candidate_id}"
    if bundle.produced_by:
        provenance += f" from:{bundle.produced_by}"
    if bundle.notes:
        provenance += f" | {bundle.notes}"
    return ModelCard(
        model_id=new_id(),
        name=bundle.name,
        version=version,
        kind="logistic_meta" if bundle.is_meta else "logistic",
        created_at=datetime.now(UTC),
        dataset_id=bundle.dataset_id,
        feature_names=bundle.feature_names,
        weights=bundle.weights,
        heads=bundle.heads,
        metrics=bundle.metrics,
        status=ModelStatus.TRAINED,
        trained_on_samples=bundle.trained_on_samples,
        notes=provenance,
    )


class CandidateImportService:
    """Sweeps the inbox and appends valid candidates to the registry."""

    def __init__(
        self,
        registry: ModelRegistry,
        registry_service: ModelRegistryService,
        inbox: Path | str,
    ) -> None:
        self._registry = registry
        self._service = registry_service
        self._inbox = Path(inbox)

    async def import_all(self) -> ImportReport:
        """Import every ``*.json`` bundle sitting in the inbox root.

        A missing inbox is not an error — it is the normal state of a deployment
        that has never received a candidate.
        """
        if not self._inbox.is_dir():
            return ImportReport()
        outcomes = [await self.import_file(path) for path in sorted(self._inbox.glob("*.json"))]
        report = ImportReport(tuple(outcomes))
        _logger.info(
            "candidates.import.swept",
            inbox=str(self._inbox),
            accepted=len(report.accepted),
            rejected=len(report.rejected),
        )
        return report

    async def import_file(self, path: Path) -> ImportOutcome:
        """Import one bundle file. Never raises: the reason travels in the outcome."""
        try:
            bundle = parse_candidate_bundle(json.loads(path.read_text(encoding="utf-8")))
        except CandidateRejectedError as exc:
            return self._reject(path, str(exc))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._reject(path, f"unreadable bundle: {exc}")

        try:
            version = await self._registry.next_version(bundle.name)
            card = bundle_to_card(bundle, version)
            # Goes through the registry *service*, not the raw port, so an imported
            # candidate emits the same ModelTrained event and increments the same
            # metrics as a locally-trained one. It is a first-class candidate.
            await self._service.register(card)
        except Exception as exc:
            return self._reject(path, f"registration failed: {exc}")

        self._move(path, ACCEPTED_DIR)
        _logger.info(
            "candidates.import.accepted",
            candidate_id=bundle.candidate_id,
            name=card.name,
            model_version=card.version,
            model_id=card.model_id,
        )
        return ImportOutcome(
            path=str(path),
            accepted=True,
            model_id=card.model_id,
            name=card.name,
            version=card.version,
        )

    # -- internals ------------------------------------------------------------

    def _reject(self, path: Path, reason: str) -> ImportOutcome:
        _logger.warning("candidates.import.rejected", path=str(path), reason=reason)
        self._move(path, REJECTED_DIR)
        return ImportOutcome(path=str(path), accepted=False, reason=reason)

    def _move(self, path: Path, bucket: str) -> None:
        """Move a processed file aside. Best-effort: a failed move never fails an
        import that already succeeded — at worst the file is swept again, and the
        registry's append-only ``(name, version)`` rule makes that visible rather
        than silently duplicating an active model."""
        target_dir = self._inbox / bucket
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            shutil.move(str(path), str(target_dir / f"{stamp}-{path.name}"))
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            _logger.warning("candidates.import.move_failed", path=str(path), error=str(exc))


__all__ = [
    "CandidateImportService",
    "ImportOutcome",
    "ImportReport",
    "bundle_to_card",
]
