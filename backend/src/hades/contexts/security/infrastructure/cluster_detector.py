"""Wallet cluster detector — funding-graph clustering of the top holders.

The core insight: wallets funded by the *same* source are almost always one
operator splitting a position across many addresses to look decentralised. This
detector fetches the funders of a token's top holders (bounded by an RPC budget)
and groups holders that share a funder. Clusters that control a large combined
share of supply are the real concentration risk that a naive holder count hides.

Cost is strictly capped: only the top ``max_holders`` are inspected and each
funder lookup is itself bounded inside the reader. When the budget stops the
search short, the report is marked ``data_complete=False`` so the analyzer treats
the gap as doubt. The :class:`NullClusterDetector` disables the feature entirely.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.security.domain.models import (
    ClusterReport,
    HolderBalance,
    WalletCluster,
)
from hades.contexts.security.domain.ports import OnChainReader
from hades.shared_kernel.logging import get_logger

_logger = get_logger("security.cluster")


class FundingGraphClusterDetector:
    """Clusters top holders by their common funding wallet."""

    def __init__(
        self,
        reader: OnChainReader,
        *,
        max_holders: int = 12,
        min_members: int = 3,
        concurrency: int = 4,
    ) -> None:
        self._reader = reader
        self._max_holders = max_holders
        self._min_members = min_members
        self._concurrency = max(1, concurrency)

    async def detect(self, token: TokenRef, holders: list[HolderBalance]) -> ClusterReport:
        """Group the top holders by shared funder, looking them up concurrently.

        Measured on the live deployment before this changed: the Security
        assembler averaged **21.4 seconds** per token against 10 milliseconds for
        all ten analyzers combined, and this loop was almost all of it. Twelve
        funder lookups, each an RPC round-trip, awaited one after another —
        independent queries about different wallets, ordered by nothing.

        The bound is deliberate and not a gather over all twelve. The RPC manager
        rate-limits per provider and this deployment has exactly one, so a burst
        of twelve would be admitted as four and *rate-limit* the rest — which the
        detector reads as reduced coverage, so an impatient version of this would
        buy latency by quietly lowering the quality of the answer. A semaphore
        keeps the request rate close to what the sequential version produced while
        overlapping the waiting.
        """
        sample = sorted(holders, key=lambda h: h.pct, reverse=True)[: self._max_holders]
        funder_to_holders: dict[str, list[HolderBalance]] = defaultdict(list)
        analysed = 0
        complete = len(sample) >= len(holders)

        semaphore = asyncio.Semaphore(self._concurrency)

        async def _funders_for(holder: HolderBalance) -> tuple[HolderBalance, list[str] | None]:
            async with semaphore:
                try:
                    return holder, await self._reader.get_funders(holder.owner)
                except Exception as exc:  # a lookup failure just lowers coverage
                    _logger.warning("funder_lookup_failed", owner=holder.owner, error=str(exc))
                    return holder, None

        # Results are consumed in the sample's order rather than completion order:
        # cluster membership must not depend on which RPC answered first, or two
        # workers would describe the same token differently.
        for holder, funders in await asyncio.gather(*(_funders_for(h) for h in sample)):
            if funders is None:
                complete = False
                continue
            analysed += 1
            for funder in funders:
                funder_to_holders[funder].append(holder)

        clusters: list[WalletCluster] = []
        for funder, members in funder_to_holders.items():
            unique = {m.owner: m for m in members}.values()
            if len(unique) >= self._min_members:
                clusters.append(
                    WalletCluster(
                        members=tuple(m.owner for m in unique),
                        funder=funder,
                        pct_supply=sum(m.pct for m in unique),
                        reason="common_funder",
                    )
                )
        return ClusterReport(
            clusters=tuple(clusters),
            analysed_wallets=analysed,
            data_complete=complete,
        )


class NullClusterDetector:
    """Disabled detector — returns an empty, complete report (feature off)."""

    async def detect(self, token: TokenRef, holders: list[HolderBalance]) -> ClusterReport:
        return ClusterReport(clusters=(), analysed_wallets=0, data_complete=True)
