"""Persistence adapters for the portfolio time series - in-memory + PostgreSQL.

The Portfolio Manager writes its history here: a full snapshot on each recompute
(``portfolio_history``), the raw equity samples behind the chart
(``equity_curve``) and a realised/unrealised PnL log (``pnl_history``). Writes
are append-only and best-effort - a history hiccup never blocks the manager from
keeping live state. The in-memory adapter backs tests and single-process runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from hades.contexts.portfolio.domain.models import PersistedPortfolio, PortfolioState
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models.portfolio import (
    EquityCurve,
    PnLHistory,
    PortfolioHistory,
    PortfolioStateRecord,
)


def _dec(value: float) -> Decimal:
    return Decimal(str(round(value, 18)))


class InMemoryPortfolioHistoryStore:
    """Bounded in-process history - keeps recent snapshots/equity/PnL for tests."""

    def __init__(self) -> None:
        self.snapshots: list[PortfolioState] = []
        self.equity: list[float] = []
        self.pnl: list[tuple[str, float]] = []

    async def record_snapshot(self, state: PortfolioState, *, mode: str) -> None:
        self.snapshots.append(state)

    async def record_equity(self, equity_usd: float, *, mode: str) -> None:
        self.equity.append(equity_usd)

    async def record_pnl(
        self, amount_usd: float, *, kind: str, mode: str, mint: str | None = None
    ) -> None:
        self.pnl.append((kind, amount_usd))


class InMemoryPortfolioStateStore:
    """Keeps the latest book in process — tests and single-process runs.

    It does not survive a restart, which is exactly what it claims: without a
    database the platform is honest about starting from the opening balance
    rather than pretending otherwise.
    """

    def __init__(self) -> None:
        self._states: dict[str, PersistedPortfolio] = {}

    async def load(self, *, mode: str) -> PersistedPortfolio | None:
        return self._states.get(mode)

    async def save(self, state: PersistedPortfolio, *, mode: str) -> None:
        self._states[mode] = state


class PostgresPortfolioStateStore:
    """Upserts and loads the single durable book row per trading mode."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def load(self, *, mode: str) -> PersistedPortfolio | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(PortfolioStateRecord).where(PortfolioStateRecord.mode == mode)
            )
        if row is None or not row.state:
            return None
        return PersistedPortfolio.model_validate(row.state)

    async def save(self, state: PersistedPortfolio, *, mode: str) -> None:
        payload = state.model_dump(mode="json")
        async with self._db.session() as session:
            row = await session.scalar(
                select(PortfolioStateRecord).where(PortfolioStateRecord.mode == mode)
            )
            if row is None:
                session.add(
                    PortfolioStateRecord(
                        mode=mode,
                        cash_usd=_dec(state.cash_usd),
                        realized_pnl_usd=_dec(state.realized_pnl_usd),
                        peak_equity_usd=_dec(state.peak_equity_usd),
                        open_positions=len(state.positions),
                        state=payload,
                    )
                )
            else:
                row.cash_usd = _dec(state.cash_usd)
                row.realized_pnl_usd = _dec(state.realized_pnl_usd)
                row.peak_equity_usd = _dec(state.peak_equity_usd)
                row.open_positions = len(state.positions)
                row.state = payload


class PostgresPortfolioHistoryStore:
    """Durable, append-only portfolio history in PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def record_snapshot(self, state: PortfolioState, *, mode: str) -> None:
        async with self._db.session() as session:
            session.add(
                PortfolioHistory(
                    mode=mode,
                    snapshot_at=state.at,
                    total_equity_usd=_dec(state.equity_usd),
                    cash_usd=_dec(state.cash_usd),
                    positions_value_usd=_dec(state.invested_usd),
                    open_positions=state.open_positions,
                    exposure_pct=_dec(state.exposure_pct),
                    realized_pnl_usd=_dec(state.realized_pnl_usd),
                    unrealized_pnl_usd=_dec(state.unrealized_pnl_usd),
                    details={"roi_pct": state.roi_pct, "drawdown_pct": state.drawdown_pct},
                )
            )

    async def record_equity(self, equity_usd: float, *, mode: str) -> None:
        async with self._db.session() as session:
            session.add(
                EquityCurve(mode=mode, timestamp=datetime.now(UTC), equity_usd=_dec(equity_usd))
            )

    async def record_pnl(
        self, amount_usd: float, *, kind: str, mode: str, mint: str | None = None
    ) -> None:
        async with self._db.session() as session:
            session.add(
                PnLHistory(
                    mode=mode,
                    kind=kind,
                    amount_usd=_dec(amount_usd),
                    timestamp=datetime.now(UTC),
                )
            )
