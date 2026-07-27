"""Ports defined by the Portfolio context."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hades.contexts.portfolio.domain.models import (
    PersistedPortfolio,
    PortfolioState,
    Position,
)
from hades.shared_kernel.domain.identifiers import EntityId


@runtime_checkable
class PositionRepository(Protocol):
    """Loads and persists ``Position`` aggregates (event-sourced)."""

    async def get(self, position_id: EntityId) -> Position: ...

    async def save(self, position: Position) -> None: ...

    async def list_open(self) -> list[Position]: ...


@runtime_checkable
class PortfolioHistoryStore(Protocol):
    """Persists the portfolio time series - snapshots, equity curve and PnL log.

    Write-only from the Portfolio Manager's perspective (the dashboard reads the
    tables directly). Every write is best-effort: a history hiccup must never
    stop the manager from maintaining live state.
    """

    async def record_snapshot(self, state: PortfolioState, *, mode: str) -> None: ...

    async def record_equity(self, equity_usd: float, *, mode: str) -> None: ...

    async def record_pnl(
        self, amount_usd: float, *, kind: str, mode: str, mint: str | None = None
    ) -> None: ...


@runtime_checkable
class PortfolioStateStore(Protocol):
    """Persists and restores the live book so it survives a restart.

    Distinct from :class:`PortfolioHistoryStore`, which is an append-only time
    series for charts: this holds the single latest *state*, and it is read back
    on startup. Keyed by trading mode so a paper book and a live book can never
    be confused for one another.
    """

    async def load(self, *, mode: str) -> PersistedPortfolio | None: ...

    async def save(self, state: PersistedPortfolio, *, mode: str) -> None: ...
