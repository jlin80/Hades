"""Read-only historical data sources for the Research Lab.

The lab's *only* inbound dependency. Two adapters:

* :class:`InMemoryHistoricalReader` — a fixed list of samples for tests/dev.
* :class:`OutcomeHistoricalReader` — projects the AI Committee's append-only
  labelled outcome ledger (feature vector + realised ROI) into research samples,
  reusing production's own recorded history as the lab's dataset. It reads a copy;
  it never writes, and it cannot reach any production context.
* :class:`InMemoryDecisionHistoryReader` / :class:`OutcomeDecisionHistoryReader` —
  the same ledger, *un*projected, for the decision replay (§3.2), which needs the
  production feature names rather than the lab's normalised channels.

A "sample" is a plain mapping: normalised feature channels plus a
``forward_return`` label. Nothing here can affect live state.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select

from hades.contexts.research.application.subscriber import CHANNEL_ALIASES
from hades.contexts.research.domain.models import clamp01
from hades.contexts.research.domain.ports import HistoricalDecisionRow, HistoricalSample
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models.learning import CommitteeOutcomeRecord


class InMemoryHistoricalReader:
    """A fixed sample list — the read-only source used in tests and dev."""

    def __init__(self, samples: Sequence[HistoricalSample]) -> None:
        self._samples = list(samples)

    async def load_samples(
        self, *, from_iso: str | None = None, to_iso: str | None = None, limit: int = 100_000
    ) -> Sequence[HistoricalSample]:
        return self._samples[:limit]

    async def count(self) -> int:
        return len(self._samples)


def _project(features: dict[str, float], realized_roi: float, mint: str) -> HistoricalSample:
    """Map a committee outcome's raw features onto the lab's channels + label."""
    sample: HistoricalSample = HistoricalSample({"mint": mint, "forward_return": realized_roi})
    for channel, aliases in CHANNEL_ALIASES.items():
        for name in aliases:
            if name in features:
                sample[channel] = clamp01(float(features[name]))
                break
    return sample


class OutcomeHistoricalReader:
    """Projects the committee's labelled outcome ledger into research samples.

    Read-only: it only ``SELECT``s ``committee_outcomes``. This is how the lab
    trains and backtests on real history without ever touching production state.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def load_samples(
        self, *, from_iso: str | None = None, to_iso: str | None = None, limit: int = 100_000
    ) -> Sequence[HistoricalSample]:
        stmt = select(CommitteeOutcomeRecord).order_by(CommitteeOutcomeRecord.at)
        if from_iso is not None:
            stmt = stmt.where(CommitteeOutcomeRecord.at >= from_iso)
        if to_iso is not None:
            stmt = stmt.where(CommitteeOutcomeRecord.at <= to_iso)
        stmt = stmt.limit(limit)
        async with self._db.session() as session:
            rows = (await session.scalars(stmt)).all()
        return [
            _project(dict(r.features or {}), float(r.realized_roi or 0.0), r.mint) for r in rows
        ]

    async def count(self) -> int:
        async with self._db.session() as session:
            total = await session.scalar(select(func.count()).select_from(CommitteeOutcomeRecord))
        return int(total or 0)


class InMemoryDecisionHistoryReader:
    """A fixed list of decision rows — the source used in tests and dev."""

    def __init__(self, rows: Sequence[HistoricalDecisionRow]) -> None:
        self._rows = list(rows)

    async def load_decisions(
        self, *, from_iso: str | None = None, to_iso: str | None = None, limit: int = 100_000
    ) -> Sequence[HistoricalDecisionRow]:
        return self._rows[:limit]


class OutcomeDecisionHistoryReader:
    """The platform's own captured history, unprojected, for the decision replay.

    This is the answer to checklist §3.2's "define the source": Hades' own
    ``committee_outcomes`` ledger, not a purchased dataset. It costs nothing, it
    is exactly the distribution the bot actually saw, and it needs no decision
    from the operator about spending.

    The one thing it does *not* do is invent labels. ``label_roi_positive`` on a
    rejected row is the column default, not a measurement — nothing was executed,
    so no return was realised. Those rows are handed over with a ``None`` label so
    the backtest counts them as unlabelled instead of reading 1,771 defaults as a
    unanimous verdict. Read-only: it only ``SELECT``s.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def load_decisions(
        self, *, from_iso: str | None = None, to_iso: str | None = None, limit: int = 100_000
    ) -> Sequence[HistoricalDecisionRow]:
        stmt = select(CommitteeOutcomeRecord).order_by(CommitteeOutcomeRecord.at)
        if from_iso is not None:
            stmt = stmt.where(CommitteeOutcomeRecord.at >= from_iso)
        if to_iso is not None:
            stmt = stmt.where(CommitteeOutcomeRecord.at <= to_iso)
        stmt = stmt.limit(limit)
        async with self._db.session() as session:
            records = (await session.scalars(stmt)).all()
        return [project_decision(record) for record in records]


def project_decision(record: CommitteeOutcomeRecord) -> HistoricalDecisionRow:
    executed = bool(record.was_executed) and not bool(record.was_rejected)
    row: HistoricalDecisionRow = HistoricalDecisionRow(
        {
            "mint": record.mint,
            "at": record.at,
            "features": dict(record.features or {}),
            "label_roi_positive": bool(record.label_roi_positive) if executed else None,
            "realized_roi": float(record.realized_roi) if executed else None,
            "was_executed": executed,
        }
    )
    return row
