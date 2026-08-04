"""The knowledge bundle — the one contract the memory accepts from outside.

Hades Research Lab is an **independent repository and an independent stack**. It
has its own database, its own collectors and its own ML toolchain, and the Core
has no runtime dependency on it whatsoever: no shared library, no import, no
network call, no schema in common. The two are joined by exactly one thing — a
declarative JSON bundle the lab writes to disk and the Core reads.

That is deliberate. A shared package would couple two release cycles; an HTTP
endpoint would give an external process a live handle on the platform's memory.
A file is inspectable before it is trusted, diffable in review, and produces
exactly nothing if nobody sweeps the inbox.

---

**The invariant that matters, and why it is enforced here rather than trusted.**

A bundle **cannot declare its own verification level.** The field does not exist
in the format. The parser derives it from the record's declared ``source``, and
every source an external producer is permitted to claim maps to
:attr:`~.models.Verification.SIMULATED`.

This is not tidiness. ``Verification.REALISED`` means *settled reality* — a trade
that opened, ran and paid out — and the platform's training path treats it as
ground truth. If an external repository could stamp ``realised`` on a backtest
result, it could inject a simulation into the ledger the AI Committee learns
from, and the resulting model would be trained on its own assumptions. Nothing
downstream would notice: the rows would look exactly like real ones.

So the trust boundary is drawn in the type system:

* the permitted ``source`` values are an allowlist (:data:`_EXTERNAL_SOURCES`) —
  a bundle claiming ``paper_trading`` or ``executed_trade`` is **rejected**, so
  the lab cannot impersonate the trading path;
* ``verification`` is assigned by the Core, never read from the payload;
* an external bundle produces :class:`~.models.Observation` records only. It has
  no way to express a :class:`~.models.Lesson`, and lessons are the only thing
  the committee trains on. Ground truth remains producible **exclusively** by the
  platform settling a trade it actually took.

Parsing is fail-closed: the first thing this module cannot fully vouch for
raises :class:`KnowledgeBundleRejectedError`, and a rejected bundle changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any, Final, NoReturn

from hades.contexts.knowledge.domain.models import (
    KnowledgeEnvelope,
    KnowledgeKind,
    KnowledgeSource,
    SubjectType,
    Verification,
)
from hades.shared_kernel.errors.exceptions import DomainError

#: The only bundle format this Core understands. A bundle declaring anything
#: else is rejected rather than best-effort parsed — version skew must be loud.
BUNDLE_FORMAT: Final = "hades.knowledge/v1"

#: The sources an *external* producer may claim. Every one is a research
#: activity, and every one maps to SIMULATED below. `paper_trading`,
#: `executed_trade`, `scanner`, `security`, `wallet_intelligence` and `committee`
#: are deliberately absent: those are the platform's own observations of itself,
#: and a file on disk must not be able to pose as one.
_EXTERNAL_SOURCES: Final[frozenset[KnowledgeSource]] = frozenset(
    {
        KnowledgeSource.RESEARCH_LAB,
        KnowledgeSource.BACKTEST,
        KnowledgeSource.WALK_FORWARD,
        KnowledgeSource.MONTE_CARLO,
        KnowledgeSource.SHADOW_TRADING,
    }
)

#: The kinds an external producer may claim. A research artifact is a simulation
#: or an experiment; it is never an OUTCOME, which is the vocabulary of settled
#: reality.
_EXTERNAL_KINDS: Final[frozenset[KnowledgeKind]] = frozenset(
    {
        KnowledgeKind.SIMULATION,
        KnowledgeKind.EXPERIMENT,
        KnowledgeKind.OBSERVATION,
        KnowledgeKind.ASSESSMENT,
    }
)

_ALLOWED_TOP_LEVEL: Final = frozenset(
    {"bundle_format", "bundle_id", "produced_by", "produced_at", "records", "notes", "checksum"}
)

_ALLOWED_RECORD_KEYS: Final = frozenset(
    {
        "source",
        "kind",
        "subject",
        "subject_type",
        "occurred_at",
        "payload",
        "features",
        "correlation_id",
    }
)

#: A bundle is a batch, not a firehose. An unbounded one would let a single file
#: exhaust memory before a single record was validated.
MAX_RECORDS: Final = 5_000


class KnowledgeBundleRejectedError(DomainError):
    """A bundle failed validation and was not imported. Carries the reason."""


def _fail(reason: str) -> NoReturn:
    raise KnowledgeBundleRejectedError(reason)


def canonical_checksum(payload: dict[str, Any]) -> str:
    """The sha256 both sides agree on: canonical JSON minus ``checksum``.

    Sorted keys, compact separators, UTF-8. The lab computes it when writing and
    the Core recomputes it when reading, so a bundle truncated or edited in
    transit cannot be imported. It is an integrity check, not an authenticity
    one — it proves the file is intact, not that it came from anyone in
    particular. The allowlists above are what bound the damage a hostile file
    could do.
    """
    body = {key: value for key, value in payload.items() if key != "checksum"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verification_for(source: KnowledgeSource) -> Verification:
    """The verification level the Core assigns to an external record.

    Always :attr:`Verification.SIMULATED`. The function exists so the rule has a
    name and a single place to change, not because it currently branches — and if
    it ever does branch, the change will be visible in review rather than buried
    in a parser.
    """
    return Verification.SIMULATED


def _finite(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{where}: {number} is not finite")
    return number


def _iso(value: object, where: str) -> datetime:
    if not isinstance(value, str) or not value:
        _fail(f"{where}: expected an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail(f"{where}: {value!r} is not a valid ISO-8601 timestamp")
    # A naive timestamp would be silently reinterpreted as local time by every
    # consumer downstream, which is how a research window quietly shifts by
    # hours. Assume UTC explicitly rather than letting it drift.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _enum[T: (KnowledgeSource, KnowledgeKind, SubjectType)](
    cls: type[T], raw: object, where: str, allowed: frozenset[T] | None = None
) -> T:
    if not isinstance(raw, str):
        _fail(f"{where}: expected a string")
    try:
        value = cls(raw)
    except ValueError:
        _fail(f"{where}: {raw!r} is not a recognised {cls.__name__}")
    if allowed is not None and value not in allowed:
        _fail(
            f"{where}: {raw!r} is not accepted from an external producer "
            f"(allowed: {sorted(item.value for item in allowed)})"
        )
    return value


def _parse_record(raw: object, index: int) -> KnowledgeEnvelope:
    where = f"records[{index}]"
    if not isinstance(raw, dict):
        _fail(f"{where}: expected an object")

    unknown = set(raw) - _ALLOWED_RECORD_KEYS
    if unknown:
        # Unknown keys are rejected rather than ignored: a bundle written by a
        # newer lab must not be half-understood, because the half we skipped
        # might be the half that mattered.
        _fail(f"{where}: unknown keys {sorted(unknown)}")

    source = _enum(KnowledgeSource, raw.get("source"), f"{where}.source", _EXTERNAL_SOURCES)
    kind = _enum(KnowledgeKind, raw.get("kind"), f"{where}.kind", _EXTERNAL_KINDS)
    subject_type = _enum(
        SubjectType, raw.get("subject_type", SubjectType.STRATEGY.value), f"{where}.subject_type"
    )

    subject = raw.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        _fail(f"{where}.subject: must be a non-empty string")

    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        _fail(f"{where}.payload: expected an object")

    features_raw = raw.get("features", {})
    if not isinstance(features_raw, dict):
        _fail(f"{where}.features: expected an object")
    features = {
        str(name): _finite(value, f"{where}.features[{name}]")
        for name, value in features_raw.items()
    }

    correlation_id = raw.get("correlation_id")
    if correlation_id is not None and not isinstance(correlation_id, str):
        _fail(f"{where}.correlation_id: expected a string or null")

    return KnowledgeEnvelope(
        source=source,
        kind=kind,
        # Assigned, never read from the bundle. See the module docstring.
        verification=verification_for(source),
        subject=subject,
        subject_type=subject_type,
        occurred_at=_iso(raw.get("occurred_at"), f"{where}.occurred_at"),
        payload=payload,
        features=features,
        correlation_id=correlation_id,
    )


class KnowledgeBundle:
    """A validated bundle. Constructing one is the only way to trust a file."""

    __slots__ = ("bundle_id", "checksum", "envelopes", "notes", "produced_at", "produced_by")

    def __init__(
        self,
        *,
        bundle_id: str,
        produced_by: str,
        produced_at: datetime,
        envelopes: tuple[KnowledgeEnvelope, ...],
        notes: str,
        checksum: str,
    ) -> None:
        self.bundle_id = bundle_id
        self.produced_by = produced_by
        self.produced_at = produced_at
        self.envelopes = envelopes
        self.notes = notes
        self.checksum = checksum

    def __len__(self) -> int:
        return len(self.envelopes)


def parse_knowledge_bundle(payload: object) -> KnowledgeBundle:
    """Validate a decoded bundle, or raise :class:`KnowledgeBundleRejectedError`.

    Pure and total: no I/O, no registry, no clock, so the entire trust boundary
    is unit-testable without a stack behind it.
    """
    if not isinstance(payload, dict):
        _fail("bundle: expected a JSON object at the top level")
    bundle: dict[str, object] = dict(payload)

    unknown = set(bundle) - _ALLOWED_TOP_LEVEL
    if unknown:
        _fail(f"bundle: unknown keys {sorted(unknown)}")

    if bundle.get("bundle_format") != BUNDLE_FORMAT:
        _fail(f"bundle_format: expected {BUNDLE_FORMAT!r}, got {bundle.get('bundle_format')!r}")

    checksum = bundle.get("checksum")
    if not isinstance(checksum, str) or not checksum:
        _fail("checksum: missing")
    if checksum != canonical_checksum(bundle):
        _fail("checksum: mismatch — the bundle was modified or truncated in transit")

    bundle_id = bundle.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id:
        _fail("bundle_id: missing")

    produced_by = bundle.get("produced_by")
    if not isinstance(produced_by, str) or not produced_by:
        _fail("produced_by: missing")

    notes = bundle.get("notes", "")
    if not isinstance(notes, str):
        _fail("notes: expected a string")

    records = bundle.get("records")
    if not isinstance(records, list) or not records:
        _fail("records: expected a non-empty array")
    if len(records) > MAX_RECORDS:
        _fail(f"records: {len(records)} exceeds the {MAX_RECORDS} per-bundle limit")

    envelopes = tuple(_parse_record(record, index) for index, record in enumerate(records))

    return KnowledgeBundle(
        bundle_id=bundle_id,
        produced_by=produced_by,
        produced_at=_iso(bundle.get("produced_at"), "produced_at"),
        envelopes=envelopes,
        notes=notes,
        checksum=checksum,
    )


__all__ = [
    "BUNDLE_FORMAT",
    "MAX_RECORDS",
    "KnowledgeBundle",
    "KnowledgeBundleRejectedError",
    "canonical_checksum",
    "parse_knowledge_bundle",
    "verification_for",
]
