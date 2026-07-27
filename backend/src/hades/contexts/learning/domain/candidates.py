"""The Research Lab candidate bundle — the one contract the Core accepts from outside.

Hades Research Lab is an **independent repository and an independent stack**. It
researches on copies of history and produces *knowledge*; it has no permission to
write into the Core, and the Core has no runtime dependency on it. The two are
joined by exactly one thing: a **declarative, self-describing JSON bundle** that
the lab writes to disk and a human carries into the Core's inbox.

Why a JSON bundle and not a pickled estimator:

* The registry stores **transparent weight sets** — ``sigmoid(bias + Σ wᵢ·xᵢ)`` over
  named features. A bundle is diffable, auditable and reviewable *before* it is
  imported. Explainability is a golden rule; an opaque blob would break it.
* Unpickling is code execution. The Core must never execute an artifact that
  originated outside it. JSON cannot execute.
* The Core image is deliberately pure-Python (no numpy/sklearn at runtime). A
  bundle carries numbers, so it needs nothing new.

Parsing is **fail-closed**: this module raises :class:`CandidateRejectedError` on the
first thing it cannot fully vouch for and never returns a partially-trusted
object. A rejected bundle changes nothing — the registry is untouched.

Nothing here promotes, decides or executes. A parsed bundle becomes a
``TRAINED`` :class:`~hades.contexts.learning.domain.models.ModelCard` candidate and
stops there; promotion remains the human-gated step it has always been.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Final, NoReturn

from hades.contexts.learning.domain.models import (
    META_MODEL_NAME,
    CommitteeMember,
    ModelMetrics,
    ModelWeights,
)
from hades.shared_kernel.errors.exceptions import DomainError

#: The only bundle format this Core understands. A bundle declaring anything else
#: is rejected rather than best-effort parsed — version skew must be loud.
BUNDLE_FORMAT: Final = "hades.candidate/v1"

#: The only candidate kind wired today. Strategy-parameter and feature candidates
#: are deliberately *not* accepted yet: they have no human-gated destination in the
#: Core, and accepting what we cannot gate would be worse than rejecting it.
BUNDLE_KIND: Final = "committee_model"

#: Names the registry recognises: the twelve specialists plus the meta-model.
VALID_MODEL_NAMES: Final = frozenset({m.value for m in CommitteeMember} | {META_MODEL_NAME})

#: The meta-model's heads. A meta bundle must carry exactly these.
META_HEADS: Final = ("roi_positive", "hit_tp", "hit_sl")

#: Keys allowed at the top level of a bundle. Unknown keys are rejected so a bundle
#: written by a newer lab is never silently half-understood.
_ALLOWED_KEYS: Final = frozenset(
    {
        "bundle_format",
        "kind",
        "candidate_id",
        "produced_by",
        "produced_at",
        "name",
        "model_kind",
        "dataset_id",
        "feature_names",
        "weights",
        "heads",
        "metrics",
        "trained_on_samples",
        "notes",
        "evidence",
        "checksum",
    }
)

#: Metric fields the Core stores. Anything else the lab reports is evidence, not a
#: registry metric, and is kept in ``notes``/``evidence`` rather than coerced.
_METRIC_FIELDS: Final = frozenset(ModelMetrics.model_fields)


class CandidateRejectedError(DomainError):
    """A bundle failed validation and was not imported. Carries the reason."""


# ``NoReturn`` is load-bearing: it lets the type-checker narrow the decoded JSON
# after each guard, so every value reaching a constructor is provably the right
# type rather than an ``Any`` waved through with an ignore.
def _fail(reason: str) -> NoReturn:
    raise CandidateRejectedError(reason)


def canonical_checksum(payload: dict[str, Any]) -> str:
    """Return the sha256 both sides agree on: canonical JSON minus ``checksum``.

    Sorted keys, compact separators, UTF-8. The lab computes it when writing and
    the Core recomputes it when reading, so a bundle that was truncated or edited
    in transit cannot be imported.
    """
    body = {k: v for k, v in payload.items() if k != "checksum"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finite(value: object, where: str) -> float:
    """Coerce to a finite float or reject. NaN/inf would poison every prediction."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{where}: {number} is not finite")
    return number


def _parse_weights(raw: object, where: str) -> ModelWeights:
    if not isinstance(raw, dict):
        _fail(f"{where}: expected an object")
    weights: dict[str, object] = dict(raw)
    unknown = set(weights) - {"bias", "coefficients"}
    if unknown:
        _fail(f"{where}: unknown keys {sorted(unknown)}")
    coefficients_raw = weights.get("coefficients")
    if not isinstance(coefficients_raw, dict) or not coefficients_raw:
        _fail(f"{where}.coefficients: expected a non-empty object")
    coefficients: dict[str, float] = {}
    for feature, value in coefficients_raw.items():
        if not isinstance(feature, str) or not feature:
            _fail(f"{where}.coefficients: feature names must be non-empty strings")
        coefficients[feature] = _finite(value, f"{where}.coefficients[{feature}]")
    return ModelWeights(
        bias=_finite(weights.get("bias", 0.0), f"{where}.bias"), coefficients=coefficients
    )


def _parse_metrics(raw: object) -> ModelMetrics:
    if raw is None:
        return ModelMetrics()
    if not isinstance(raw, dict):
        _fail("metrics: expected an object")
    # Only known metric fields are lifted into the card; unknown ones are the
    # lab's own vocabulary and must not be forced into the Core's.
    values: dict[str, float] = {}
    for field, value in raw.items():
        if field in _METRIC_FIELDS and value is not None:
            values[field] = _finite(value, f"metrics.{field}")
    samples = values.pop("samples", 0.0)
    return ModelMetrics(samples=int(samples), **values)


class CandidateBundle:
    """A validated candidate. Constructing one is the only way to trust a bundle."""

    __slots__ = (
        "candidate_id",
        "checksum",
        "dataset_id",
        "evidence",
        "feature_names",
        "heads",
        "metrics",
        "name",
        "notes",
        "produced_at",
        "produced_by",
        "trained_on_samples",
        "weights",
    )

    def __init__(
        self,
        *,
        candidate_id: str,
        produced_by: str,
        produced_at: str,
        name: str,
        dataset_id: str | None,
        feature_names: tuple[str, ...],
        weights: ModelWeights,
        heads: dict[str, ModelWeights],
        metrics: ModelMetrics,
        trained_on_samples: int,
        notes: str,
        evidence: dict[str, Any],
        checksum: str,
    ) -> None:
        self.candidate_id = candidate_id
        self.produced_by = produced_by
        self.produced_at = produced_at
        self.name = name
        self.dataset_id = dataset_id
        self.feature_names = feature_names
        self.weights = weights
        self.heads = heads
        self.metrics = metrics
        self.trained_on_samples = trained_on_samples
        self.notes = notes
        self.evidence = evidence
        self.checksum = checksum

    @property
    def is_meta(self) -> bool:
        return self.name == META_MODEL_NAME


def parse_candidate_bundle(payload: object) -> CandidateBundle:
    """Validate a decoded bundle and return it, or raise :class:`CandidateRejectedError`.

    Pure and total: it touches no I/O, no registry and no clock, so the whole
    trust boundary is unit-testable without a stack behind it.
    """
    if not isinstance(payload, dict):
        _fail("bundle: expected a JSON object at the top level")
    bundle: dict[str, object] = dict(payload)

    unknown = set(bundle) - _ALLOWED_KEYS
    if unknown:
        _fail(f"bundle: unknown keys {sorted(unknown)}")

    if bundle.get("bundle_format") != BUNDLE_FORMAT:
        _fail(f"bundle_format: expected {BUNDLE_FORMAT!r}, got {bundle.get('bundle_format')!r}")
    if bundle.get("kind") != BUNDLE_KIND:
        _fail(f"kind: only {BUNDLE_KIND!r} candidates can be imported, got {bundle.get('kind')!r}")

    checksum = bundle.get("checksum")
    if not isinstance(checksum, str) or not checksum:
        _fail("checksum: missing")
    expected = canonical_checksum(bundle)
    if checksum != expected:
        _fail("checksum: mismatch — the bundle was modified or truncated in transit")

    name = bundle.get("name")
    if not isinstance(name, str) or name not in VALID_MODEL_NAMES:
        _fail(f"name: {name!r} is not a committee member or the meta-model")

    model_kind = bundle.get("model_kind", "logistic")
    if model_kind != "logistic":
        _fail(f"model_kind: only 'logistic' weight sets are accepted, got {model_kind!r}")

    weights = _parse_weights(bundle.get("weights"), "weights")

    heads: dict[str, ModelWeights] = {}
    raw_heads = bundle.get("heads") or {}
    if not isinstance(raw_heads, dict):
        _fail("heads: expected an object")
    for head, raw in raw_heads.items():
        if not isinstance(head, str):
            _fail("heads: head names must be strings")
        heads[head] = _parse_weights(raw, f"heads[{head}]")

    is_meta = name == META_MODEL_NAME
    if is_meta and tuple(sorted(heads)) != tuple(sorted(META_HEADS)):
        _fail(f"heads: the meta-model must carry exactly {sorted(META_HEADS)}, got {sorted(heads)}")
    if not is_meta and heads:
        _fail("heads: only the meta-model may declare heads")

    declared = bundle.get("feature_names")
    if declared is None:
        feature_names = weights.feature_names
    else:
        if not isinstance(declared, list) or not all(isinstance(f, str) for f in declared):
            _fail("feature_names: expected a list of strings")
        feature_names = tuple(declared)
        # A card whose declared features disagree with its coefficients would be
        # unexplainable — the very thing this contract exists to prevent.
        if set(feature_names) != set(weights.coefficients):
            _fail("feature_names: does not match the weight coefficients")

    samples = bundle.get("trained_on_samples", 0)
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 0:
        _fail("trained_on_samples: expected a non-negative integer")

    evidence = bundle.get("evidence") or {}
    if not isinstance(evidence, dict):
        _fail("evidence: expected an object")

    candidate_id = bundle.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        _fail("candidate_id: missing")

    dataset_id = bundle.get("dataset_id")
    if not isinstance(dataset_id, str):
        dataset_id = None

    notes = bundle.get("notes", "")
    if not isinstance(notes, str):
        _fail("notes: expected a string")

    produced_by = bundle.get("produced_by", "")
    produced_at = bundle.get("produced_at", "")
    if not isinstance(produced_by, str) or not isinstance(produced_at, str):
        _fail("produced_by / produced_at: expected strings")

    return CandidateBundle(
        candidate_id=candidate_id,
        produced_by=produced_by,
        produced_at=produced_at,
        name=name,
        dataset_id=dataset_id,
        feature_names=feature_names,
        weights=weights,
        heads=heads,
        metrics=_parse_metrics(bundle.get("metrics")),
        trained_on_samples=samples,
        notes=notes,
        evidence=dict(evidence),
        checksum=checksum,
    )
