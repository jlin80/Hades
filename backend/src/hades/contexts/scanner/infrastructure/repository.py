"""Discovery repository adapters — PostgreSQL + in-memory.

Writes the facts the Scanner learns: token identities, full metadata, observed
pools (into the token's attributes) and data-quality anomalies. Uses idempotent
upserts (``ON CONFLICT``) so re-processing a token is safe. An in-memory
implementation backs the tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hades.contexts.scanner.domain.models import RawPool, TokenMetadata
from hades.contexts.scanner.domain.ports import RawTokenCandidate
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models import (
    DataAnomaly,
    Token,
    TokenMetadataRecord,
)

_logger = get_logger("scanner.repository")


class DiscoveryRepository:
    """PostgreSQL-backed implementation of the Scanner's persistence port."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def upsert_token(self, candidate: RawTokenCandidate) -> None:
        mint = str(candidate.token.mint)
        now = datetime.now(UTC)
        stmt = (
            pg_insert(Token)
            .values(
                mint=mint,
                symbol=candidate.token.symbol,
                name=candidate.token.name,
                source=candidate.source,
                first_seen_at=candidate.created_at or now,
            )
            .on_conflict_do_update(
                index_elements=[Token.mint],
                set_={"symbol": candidate.token.symbol, "name": candidate.token.name},
            )
        )
        async with self._db.session() as session:
            await session.execute(stmt)

    async def save_metadata(self, metadata: TokenMetadata) -> None:
        async with self._db.session() as session:
            token_id = await self._token_id(session, str(metadata.mint))
            if token_id is None:
                _logger.warning("metadata_for_unknown_token", mint=str(metadata.mint))
                return
            values = {
                "token_id": token_id,
                "name": metadata.name,
                "symbol": metadata.symbol,
                "decimals": metadata.decimals,
                "total_supply": metadata.total_supply,
                "logo_uri": metadata.logo_uri,
                "website": metadata.website,
                "twitter": metadata.twitter,
                "telegram": metadata.telegram,
                "discord": metadata.discord,
                "description": metadata.description,
                "program": metadata.program,
                "update_authority": metadata.update_authority,
                "mint_authority": metadata.mint_authority,
                "freeze_authority": metadata.freeze_authority,
                "is_mutable": metadata.is_mutable,
                "created_on_chain_at": metadata.created_at,
                "sources": list(metadata.sources),
                "extra": metadata.extra,
                "collected_at": datetime.now(UTC),
            }
            update = {k: v for k, v in values.items() if k != "token_id"}
            stmt = (
                pg_insert(TokenMetadataRecord)
                .values(**values)
                .on_conflict_do_update(index_elements=[TokenMetadataRecord.token_id], set_=update)
            )
            await session.execute(stmt)

    async def save_pool(self, pool: RawPool) -> None:
        async with self._db.session() as session:
            token = await self._token(session, str(pool.base_mint))
            if token is None:
                return
            pools = dict(token.attributes)
            pools["last_pool"] = {
                "address": pool.address,
                "dex": pool.dex,
                "liquidity_usd": float(pool.liquidity.amount),
            }
            token.attributes = pools

    async def record_anomaly(self, *, subject: str, kind: str, field: str, detail: str) -> None:
        """Record one sighting of a problem, collapsing repeats onto one row.

        Re-seeing the same ``(subject, kind, field)`` bumps ``occurrences`` and
        the last-seen timestamp rather than appending a row — a token rescanned
        a thousand times is one anomaly seen a thousand times, not a thousand
        anomalies. ``detail`` is refreshed because it can carry a changing
        measurement (an outlier's z-score) and the latest reading is the useful
        one.
        """
        now = datetime.now(UTC)
        stmt = (
            pg_insert(DataAnomaly)
            .values(
                subject=subject,
                kind=kind,
                field=field,
                detail=detail,
                occurrences=1,
                first_detected_at=now,
                detected_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_data_anomalies_problem",
                set_={
                    "detail": detail,
                    "occurrences": DataAnomaly.__table__.c.occurrences + 1,
                    "detected_at": now,
                },
            )
        )
        async with self._db.session() as session:
            await session.execute(stmt)

    async def _token(self, session: AsyncSession, mint: str) -> Token | None:
        result = await session.execute(select(Token).where(Token.mint == mint))
        return result.scalar_one_or_none()

    async def _token_id(self, session: AsyncSession, mint: str) -> uuid.UUID | None:
        result = await session.execute(select(Token.id).where(Token.mint == mint))
        return result.scalar_one_or_none()


class InMemoryDiscoveryRepository:
    """In-process store for tests — records everything it was asked to save."""

    def __init__(self) -> None:
        self.tokens: dict[str, RawTokenCandidate] = {}
        self.metadata: dict[str, TokenMetadata] = {}
        self.pools: list[RawPool] = []
        self.anomalies: list[dict[str, Any]] = []

    async def upsert_token(self, candidate: RawTokenCandidate) -> None:
        self.tokens[str(candidate.token.mint)] = candidate

    async def save_metadata(self, metadata: TokenMetadata) -> None:
        self.metadata[str(metadata.mint)] = metadata

    async def save_pool(self, pool: RawPool) -> None:
        self.pools.append(pool)

    async def record_anomaly(self, *, subject: str, kind: str, field: str, detail: str) -> None:
        # Mirrors the Postgres upsert: one row per distinct problem, counted.
        for existing in self.anomalies:
            if (existing["subject"], existing["kind"], existing["field"]) == (subject, kind, field):
                existing["detail"] = detail
                existing["occurrences"] = int(existing["occurrences"]) + 1
                return
        self.anomalies.append(
            {"subject": subject, "kind": kind, "field": field, "detail": detail, "occurrences": 1}
        )
