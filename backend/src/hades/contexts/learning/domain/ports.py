"""Ports defined by the AI Committee (the Learning context).

These are the seams between the pure engine/models and the outside world:
building a decision context (I/O), persisting predictions, and the model
registry (append-only, versioned). Every port is a ``Protocol`` so the
application layer depends on the contract, never on a concrete adapter, and the
composition root wires the Postgres or in-memory implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.intelligence.domain.events import WalletIntelligenceComputed
from hades.contexts.learning.domain.models import (
    CommitteePrediction,
    Dataset,
    DecisionContext,
    DriftReport,
    EnrichedCandidate,
    FeatureImportance,
    HistoricalLesson,
    HistoricalRecord,
    ModelCard,
    TrainingSample,
)

# --- serving side ------------------------------------------------------------


@runtime_checkable
class FeatureView(Protocol):
    """Reads the latest persisted feature vector for a token (training/serving parity).

    The committee never queries PostgreSQL directly; it goes through the Feature
    Store, which returns the same vectors training saw.
    """

    async def latest(self, token: TokenRef) -> FeatureSet | None: ...


@runtime_checkable
class DecisionContextBuilder(Protocol):
    """Assembles the full :class:`DecisionContext` for a token (does the I/O).

    Joins the normalised feature vector (from the Feature Store), the wallet
    snapshot carried on the triggering event, and the latest security verdict.
    """

    async def build(self, event: WalletIntelligenceComputed) -> DecisionContext | None: ...


@runtime_checkable
class CandidateHistoryPort(Protocol):
    """Reads what the platform already knows, for enrichment.

    This is a **narrow read port the Learning context declares itself** — the
    sanctioned way to depend on another context's knowledge without importing
    its application or infrastructure. It returns Learning's own value objects,
    so the committee never learns the memory's storage vocabulary and the
    enricher can be exercised with a list of lessons and no database.

    Both methods are read-only and best-effort by contract: an adapter that
    cannot reach the memory returns nothing. Enrichment degrades to "consulted,
    found nothing" — never to an exception that would stop a token being judged,
    and never to a fabricated prior.
    """

    async def lessons(self, *, limit: int = 5_000) -> tuple[HistoricalLesson, ...]:
        """Settled decisions, newest last. The only ground truth there is."""
        ...

    async def observations(self, subject: str, *, limit: int = 50) -> tuple[HistoricalRecord, ...]:
        """What the memory holds about one subject (a mint, a wallet, a dev)."""
        ...


@runtime_checkable
class CandidateEnricher(Protocol):
    """Turns a bare decision context into an :class:`EnrichedCandidate`.

    Declared as a port so the committee's handler depends on the contract and
    the composition root supplies the implementation — and so the requirement
    "no candidate reaches the committee unenriched" is expressible as a type,
    not as a convention someone has to remember.
    """

    async def enrich(self, context: DecisionContext) -> EnrichedCandidate: ...


@runtime_checkable
class PredictionStore(Protocol):
    """Persists committee predictions (append-only, auditable) and serves them."""

    async def save(self, prediction: CommitteePrediction) -> None: ...

    async def recent(self, limit: int = 50) -> tuple[CommitteePrediction, ...]: ...

    async def latest_for(self, token: TokenRef) -> CommitteePrediction | None: ...


# --- registry & training side ------------------------------------------------


@runtime_checkable
class ModelRegistry(Protocol):
    """The append-only, versioned home of every model. Never overwrites.

    Registering a model stores a new immutable ``(name, version)``. Promotion is
    an explicit, human/policy-gated state transition — the registry enforces that
    at most one version of a name is ``PROMOTED`` at a time.
    """

    async def register(self, card: ModelCard) -> None: ...

    async def get(self, model_id: str) -> ModelCard | None: ...

    async def active(self, name: str) -> ModelCard | None: ...

    async def versions(self, name: str) -> tuple[ModelCard, ...]: ...

    async def list_active(self) -> tuple[ModelCard, ...]: ...

    async def next_version(self, name: str) -> str: ...

    async def promote(self, model_id: str) -> ModelCard | None: ...

    async def set_status(self, model_id: str, status: str) -> None: ...


@runtime_checkable
class DatasetStore(Protocol):
    """Persists assembled datasets so a model's training data is reproducible."""

    async def save(self, dataset: Dataset) -> None: ...

    async def get(self, dataset_id: str) -> Dataset | None: ...


@runtime_checkable
class DriftStore(Protocol):
    """Records drift reports so degradation is visible over time."""

    async def save(self, report: DriftReport) -> None: ...

    async def recent(self, limit: int = 50) -> tuple[DriftReport, ...]: ...


@runtime_checkable
class FeatureImportanceStore(Protocol):
    """Records the continuously-computed feature-importance snapshots."""

    async def save(self, importance: FeatureImportance) -> None: ...

    async def latest(self, model_id: str) -> FeatureImportance | None: ...


@runtime_checkable
class OutcomeStore(Protocol):
    """The append-only ledger of labelled outcomes the brain learns from.

    Holds *both* executed trades (with realised ROI/TP/SL labels) and rejected
    opportunities (labelled from what they would have done, or as negatives), so
    training never sees only executed trades or only gains. The Dataset Builder
    reads from here.
    """

    async def append(self, samples: Sequence[TrainingSample]) -> None: ...

    async def load(
        self, *, since_iso: str | None = None, limit: int = 100_000
    ) -> tuple[TrainingSample, ...]: ...

    async def count(self) -> int: ...
