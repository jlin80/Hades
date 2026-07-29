"""Exploration ledger adapters — Postgres, and an in-memory twin.

Both satisfy :class:`ExplorationLedgerStore` and both are exercised by the same
tests, which is what keeps the in-memory version honest rather than a fixture
that drifted from its Postgres counterpart years ago.

Spend is **aggregated from rows**, never accumulated. The alternative — a running
total held by the service — resets on restart, and a budget that quietly
re-authorises itself on every deploy is the kind of bug that only shows up as an
overspend weeks later, with nothing in the logs to explain it.

The Postgres aggregate coalesces to zero: an empty ledger must read as "nothing
spent", not as ``None`` propagating into arithmetic that would then compare a
budget against nothing at all.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from hades.contexts.exploration.domain.models import ExplorationRecord
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models import ExplorationGrantRecord


class PostgresExplorationLedger:
    """Append-only ``exploration_grants`` adapter."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def append(self, record: ExplorationRecord) -> None:
        async with self._db.session() as session:
            session.add(
                ExplorationGrantRecord(
                    subject=record.subject,
                    granted_at=record.granted_at,
                    notional_usd=record.notional_usd,
                    cohort_key=record.cohort_key,
                    reason=record.reason[:256],
                    correlation_id=record.correlation_id,
                )
            )

    async def spent_since(self, since: datetime) -> tuple[float, int]:
        stmt = select(
            func.coalesce(func.sum(ExplorationGrantRecord.notional_usd), 0.0),
            func.count(),
        ).where(ExplorationGrantRecord.granted_at >= since)
        async with self._db.session() as session:
            row = (await session.execute(stmt)).one()
        return float(row[0] or 0.0), int(row[1] or 0)

    async def spent_total(self) -> tuple[float, int]:
        stmt = select(
            func.coalesce(func.sum(ExplorationGrantRecord.notional_usd), 0.0),
            func.count(),
        )
        async with self._db.session() as session:
            row = (await session.execute(stmt)).one()
        return float(row[0] or 0.0), int(row[1] or 0)

    async def recent(self, *, limit: int = 50) -> list[ExplorationRecord]:
        stmt = (
            select(ExplorationGrantRecord)
            .order_by(ExplorationGrantRecord.granted_at.desc())
            .limit(limit)
        )
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            ExplorationRecord(
                subject=row.subject,
                granted_at=row.granted_at,
                notional_usd=row.notional_usd,
                cohort_key=row.cohort_key,
                reason=row.reason,
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]


class InMemoryExplorationLedger:
    """In-memory ledger for tests and single-process dev.

    Note what it does *not* have: a ``clear`` or a ``remove``. The port is
    append-only, and an in-memory twin that offered a way to erase spend would be
    a way to write a test that proves a property the real system does not have.
    """

    def __init__(self) -> None:
        self._rows: list[ExplorationRecord] = []

    @property
    def rows(self) -> list[ExplorationRecord]:
        return list(self._rows)

    async def append(self, record: ExplorationRecord) -> None:
        self._rows.append(record)

    async def spent_since(self, since: datetime) -> tuple[float, int]:
        rows = [row for row in self._rows if row.granted_at >= since]
        return round(sum(row.notional_usd for row in rows), 6), len(rows)

    async def spent_total(self) -> tuple[float, int]:
        return round(sum(row.notional_usd for row in self._rows), 6), len(self._rows)

    async def recent(self, *, limit: int = 50) -> list[ExplorationRecord]:
        rows = sorted(self._rows, key=lambda row: row.granted_at, reverse=True)
        return rows[:limit]


__all__ = ["InMemoryExplorationLedger", "PostgresExplorationLedger"]
