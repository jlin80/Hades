"""Ports declared by Exploration — the two things it cannot know by itself.

Both are read-shaped and narrow, and neither can be used to act. That is not an
accident of scope: it is the reason this context can be wired into the decision
path at all. There is no port here through which exploration could reach an
executor, a wallet or a portfolio, so no future refactor can turn one into that
by widening it.

:class:`EvidencePort` is the memory's read side. The implementation lives at
Exploration's own infrastructure edge and touches only Knowledge's *domain* —
the sanctioned shape of cross-context reading on this platform, and the same one
the AI Committee's enricher uses. The arrow points Exploration → Knowledge, and
Knowledge (whose isolation test is an allowlist) still imports nothing, so there
is no cycle.

:class:`ExplorationLedgerStore` is the budget's accounting. It is append-only for
the same reason every other ledger here is: spend must be reconstructable after
the fact, and a total held in a process resets on restart — which would silently
re-authorise the day's budget on every deploy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from hades.contexts.exploration.domain.models import EvidenceStatus, ExplorationRecord


@runtime_checkable
class EvidencePort(Protocol):
    """How much ground truth the platform holds, and in which cohorts.

    Implementations must degrade rather than raise: an unreachable memory yields
    an :class:`EvidenceStatus` with ``available=False``, which the policy reads as
    "do not grant". Fail-closed is the correct direction here — exploration is
    optional spending, so not knowing whether it is still needed is a reason to
    stop, never a reason to continue.
    """

    async def status(self, *, cohort_dimensions: Sequence[str]) -> EvidenceStatus: ...


@runtime_checkable
class ExplorationLedgerStore(Protocol):
    """Append-only record of every exploration trade actually charged."""

    async def append(self, record: ExplorationRecord) -> None: ...

    async def spent_since(self, since: datetime) -> tuple[float, int]:
        """``(usd, trade_count)`` granted at or after ``since``."""
        ...

    async def spent_total(self) -> tuple[float, int]:
        """``(usd, trade_count)`` over the programme's whole lifetime."""
        ...

    async def recent(self, *, limit: int = 50) -> list[ExplorationRecord]:
        """Newest-first, for the status endpoint and after-the-fact audit."""
        ...


__all__ = ["EvidencePort", "ExplorationLedgerStore"]
