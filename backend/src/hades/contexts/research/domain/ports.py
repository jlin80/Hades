"""Ports (abstractions) the Research Lab depends on.

Two truths are encoded here. First, the lab's only inbound data dependency is a
**read-only** historical reader — it consumes copies of past events and market
snapshots and can never write to, or command, any production context. Second,
everything the lab produces is persisted through append-only stores so the whole
research funnel stays auditable forever.

Notably absent: any Execution, Risk or Portfolio port. The lab cannot import one,
so it structurally cannot place an order or enable live trading.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from hades.contexts.research.domain.models import (
    BacktestResult,
    CandidateStrategy,
    ExperimentResult,
    Hypothesis,
    KnowledgeEntry,
    PromotionDecision,
    ResearchReport,
    ShadowStrategy,
)

# --- inbound (read-only) -----------------------------------------------------


class HistoricalSample(dict[str, Any]):
    """A single point-in-time snapshot: a token's feature vector plus a realised
    forward return label. It is a plain mapping so any historical source (event
    store, feature table, replay) can produce it without a hard type dependency."""


@runtime_checkable
class HistoricalDataReader(Protocol):
    """Read-only access to historical data for backtesting and replay.

    Intentionally read-only: this is the lab's only data dependency, which is why
    the lab can never affect production state.
    """

    async def load_samples(
        self, *, from_iso: str | None = None, to_iso: str | None = None, limit: int = 100_000
    ) -> Sequence[HistoricalSample]: ...

    async def count(self) -> int: ...


class HistoricalDecisionRow(dict[str, Any]):
    """One stored decision, as the *production* decision path would see it.

    Deliberately not a :class:`HistoricalSample`: that type is projected onto the
    lab's own normalised channels, which discards the production feature names the
    Committee actually reads. Replaying the real committee needs the raw vector,
    the timestamp the decision was made at, and a label that is ``None`` unless it
    was genuinely realised.
    """


@runtime_checkable
class DecisionHistoryReader(Protocol):
    """Read-only access to stored decisions for :mod:`decision_backtest`.

    Read-only for the same reason as :class:`HistoricalDataReader`, and separate
    from it because the shapes differ, not because the source does.
    """

    async def load_decisions(
        self, *, from_iso: str | None = None, to_iso: str | None = None, limit: int = 100_000
    ) -> Sequence[HistoricalDecisionRow]: ...


@runtime_checkable
class ExperimentRunner(Protocol):
    """Executes a defined experiment against historical (copied) data."""

    async def run(self, spec: Any) -> ExperimentResult: ...


# --- outbound (append-only persistence) --------------------------------------


@runtime_checkable
class ExperimentStore(Protocol):
    async def save(self, result: ExperimentResult) -> None: ...
    async def recent(self, limit: int = 50) -> tuple[ExperimentResult, ...]: ...


@runtime_checkable
class BacktestStore(Protocol):
    async def save(self, result: BacktestResult) -> None: ...
    async def recent(self, limit: int = 50) -> tuple[BacktestResult, ...]: ...


@runtime_checkable
class CandidateStore(Protocol):
    async def save(self, candidate: CandidateStrategy) -> None: ...
    async def get(self, candidate_id: str) -> CandidateStrategy | None: ...
    async def all(self) -> tuple[CandidateStrategy, ...]: ...


@runtime_checkable
class ShadowStore(Protocol):
    async def save(self, shadow: ShadowStrategy) -> None: ...
    async def all(self) -> tuple[ShadowStrategy, ...]: ...


@runtime_checkable
class PromotionStore(Protocol):
    async def save(self, decision: PromotionDecision) -> None: ...
    async def recent(self, limit: int = 50) -> tuple[PromotionDecision, ...]: ...


@runtime_checkable
class KnowledgeStore(Protocol):
    """Append-only Knowledge Base — history is never deleted."""

    async def append(self, entry: KnowledgeEntry) -> None: ...
    async def append_hypothesis(self, hypothesis: Hypothesis) -> None: ...
    async def recent(self, limit: int = 100) -> tuple[KnowledgeEntry, ...]: ...
    async def hypotheses(self, limit: int = 100) -> tuple[Hypothesis, ...]: ...


@runtime_checkable
class ReportStore(Protocol):
    async def save(self, report: ResearchReport) -> None: ...
    async def recent(self, limit: int = 50) -> tuple[ResearchReport, ...]: ...
