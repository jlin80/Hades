"""Inbox ingestion — the Core's only path from the external Research Lab.

It is a **pull**, never a push. The lab writes bundles to a directory it owns;
an operator (or a mounted volume) places them in the Core's inbox; this service
sweeps it. Nothing about that chain gives an external process a live handle on
the platform: with nobody sweeping, a bundle sitting on disk does exactly
nothing.

Every processed file is **moved aside** into ``accepted/`` or ``rejected/`` with
a timestamped name. That is the audit trail, and it is a filesystem one on
purpose — the artifact that was imported is still there, byte for byte, next to
the reason it was accepted, and can be re-read months later by someone who does
not have this codebase. Rejections are kept, not deleted: the file that failed is
usually the one you need to look at.

This service never raises. An unreadable file, a malformed bundle and a dead
database are all *outcomes*, reported per file, because a sweep that aborts
halfway leaves the operator unable to tell which bundles landed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hades.contexts.knowledge.application.recorder import KnowledgeRecorder
from hades.contexts.knowledge.domain.bundles import (
    KnowledgeBundleRejectedError,
    parse_knowledge_bundle,
)
from hades.shared_kernel.logging import get_logger

_logger = get_logger("knowledge.ingest")

ACCEPTED_DIR = "accepted"
REJECTED_DIR = "rejected"


@dataclass(frozen=True)
class IngestOutcome:
    """What happened to one bundle file."""

    path: str
    accepted: bool
    bundle_id: str | None = None
    produced_by: str | None = None
    records: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class IngestReport:
    """The result of one sweep."""

    outcomes: tuple[IngestOutcome, ...] = field(default_factory=tuple)

    @property
    def accepted(self) -> tuple[IngestOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.accepted)

    @property
    def rejected(self) -> tuple[IngestOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.accepted)

    @property
    def records_imported(self) -> int:
        return sum(outcome.records for outcome in self.accepted)


class KnowledgeIngestService:
    """Sweeps the inbox and records valid bundles into permanent memory."""

    def __init__(self, recorder: KnowledgeRecorder, inbox: Path | str) -> None:
        self._recorder = recorder
        self._inbox = Path(inbox)

    async def import_all(self) -> IngestReport:
        """Import every ``*.json`` bundle in the inbox root.

        A missing inbox is not an error — it is the normal state of a deployment
        that has never been handed a bundle. Sub-directories are not walked, so
        ``accepted/`` and ``rejected/`` are never re-swept.
        """
        if not self._inbox.is_dir():
            _logger.info("knowledge.ingest.no_inbox", inbox=str(self._inbox))
            return IngestReport()

        outcomes = [await self.import_file(path) for path in sorted(self._inbox.glob("*.json"))]
        report = IngestReport(tuple(outcomes))
        _logger.info(
            "knowledge.ingest.swept",
            inbox=str(self._inbox),
            accepted=len(report.accepted),
            rejected=len(report.rejected),
            records=report.records_imported,
        )
        return report

    async def import_file(self, path: Path) -> IngestOutcome:
        """Import one bundle. Never raises: the reason travels in the outcome."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._reject(path, f"unreadable bundle: {exc}")

        try:
            bundle = parse_knowledge_bundle(raw)
        except KnowledgeBundleRejectedError as exc:
            return self._reject(path, str(exc))

        try:
            accepted = await self._recorder.record(bundle.envelopes)
        except Exception as exc:
            # The store is down, not the bundle's fault. It goes to rejected/ so
            # the operator can move it back and retry once the platform is
            # healthy, rather than the file vanishing with its contents unwritten.
            return self._reject(path, f"recording failed: {exc}")

        self._move(path, ACCEPTED_DIR)
        _logger.info(
            "knowledge.ingest.accepted",
            bundle_id=bundle.bundle_id,
            produced_by=bundle.produced_by,
            records=len(accepted),
            declared=len(bundle),
        )
        return IngestOutcome(
            path=str(path),
            accepted=True,
            bundle_id=bundle.bundle_id,
            produced_by=bundle.produced_by,
            records=len(accepted),
        )

    # -- internals ------------------------------------------------------------

    def _reject(self, path: Path, reason: str) -> IngestOutcome:
        _logger.warning("knowledge.ingest.rejected", path=str(path), reason=reason)
        self._move(path, REJECTED_DIR)
        return IngestOutcome(path=str(path), accepted=False, reason=reason)

    def _move(self, path: Path, bucket: str) -> None:
        """Move a processed file aside. Best-effort.

        A failed move never fails an import that already succeeded — at worst the
        file is swept again. Re-ingestion duplicates observations rather than
        corrupting anything: the store is append-only and observations are
        statements about the past, so a second copy is redundant, not wrong. (A
        *lesson* would be a different matter, which is exactly why an external
        bundle cannot express one.)
        """
        target_dir = self._inbox / bucket
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            shutil.move(str(path), str(target_dir / f"{stamp}-{path.name}"))
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            _logger.warning("knowledge.ingest.move_failed", path=str(path), error=str(exc))


__all__ = ["IngestOutcome", "IngestReport", "KnowledgeIngestService"]
