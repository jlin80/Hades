"""Decision Context Builder — assemble everything the committee needs (I/O here).

Keeps the engine and models pure by doing all the reads itself. For a token it:

    * loads the latest persisted feature vector from the Feature Store and
      normalises it through the Feature Catalog (never touches raw PostgreSQL);
    * reads the wallet-intelligence snapshot straight off the triggering event;
    * looks up the most recent security verdict for the token (best-effort — its
      absence degrades the security features to neutral, never an error);
    * establishes the candidate's **identity** — the cohort keys the Candidate
      Enricher will query the memory with: developer, launchpad, narrative,
      dominant cluster, wallets, liquidity and volatility.

Identity is assembled *here*, from reads this builder already performs, rather
than in the enricher. Two reasons, both structural: the enricher stays pure and
testable with no database, and the identity is derived from exactly the same
snapshot of the world as the features it accompanies — asking again a moment
later is a second chance to get a different answer.

The result is an immutable :class:`DecisionContext`. It builds context; it takes
no decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from hades.contexts.intelligence.domain.events import WalletIntelligenceComputed
from hades.contexts.intelligence.domain.models import IntelligenceSnapshot
from hades.contexts.learning.application.feature_catalog import FeatureNormalizer
from hades.contexts.learning.application.narrative import narrative_of
from hades.contexts.learning.domain.models import CandidateIdentity, DecisionContext
from hades.contexts.learning.domain.ports import FeatureView
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.persistence.database import Database
from hades.shared_kernel.persistence.models.scanner import TokenMetadataRecord
from hades.shared_kernel.persistence.models.security import SecurityAssessmentRecord
from hades.shared_kernel.persistence.models.tokens import Token

_logger = get_logger("committee.context_builder")


@dataclass(frozen=True)
class _SecurityRead:
    """The security verdict as this builder reads it, defaults included.

    A record rather than a dict because ``facts`` (the per-analyzer evidence the
    identity is mined from) joined the read in this phase, and an untyped bag
    that three call sites index by string is how a shape change becomes a
    runtime surprise instead of a type error.
    """

    approved: bool = True
    score: float = 50.0
    sub_scores: dict[str, float] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)


#: How many of a token's wallets travel into the identity. The enricher looks
#: each one up, so this is a bound on work per candidate, not a preference.
_MAX_WALLETS = 12


class DecisionContextBuilder:
    """Assembles the full decision context for a token from all upstream reads."""

    def __init__(
        self,
        feature_view: FeatureView,
        normalizer: FeatureNormalizer,
        database: Database | None = None,
    ) -> None:
        self._features = feature_view
        self._normalizer = normalizer
        self._db = database

    async def build(self, event: WalletIntelligenceComputed) -> DecisionContext | None:
        token = event.token
        feature_set = await self._features.latest(token)
        if feature_set is None:
            return None
        vector = self._normalizer.normalize(feature_set)
        snapshot = event.snapshot
        mint = str(token.mint)
        security = await self._security(mint)
        identity = await self._identity(mint, event, security, vector.raw)

        return DecisionContext(
            token=token,
            at=datetime.now(UTC),
            vector=vector,
            security_approved=security.approved,
            security_score=security.score,
            security_sub_scores=security.sub_scores,
            smart_money_count=snapshot.smart_money_count,
            dumb_money_count=snapshot.dumb_money_count,
            smart_money_pct_supply=snapshot.smart_money_pct_supply,
            avg_wallet_trust=snapshot.avg_trust,
            avg_wallet_risk=snapshot.avg_risk,
            cluster_count=snapshot.cluster_count,
            largest_cluster_pct=snapshot.largest_cluster_pct,
            wallets_observed=snapshot.wallets_observed,
            identity=identity,
        )

    async def _security(self, mint: str) -> _SecurityRead:
        neutral = _SecurityRead()
        if self._db is None:
            return neutral
        try:
            async with self._db.session() as session:
                row = await session.scalar(
                    select(SecurityAssessmentRecord)
                    .where(SecurityAssessmentRecord.mint == mint)
                    .order_by(desc(SecurityAssessmentRecord.analyzed_at))
                    .limit(1)
                )
        except Exception as exc:  # never fail the committee on a DB hiccup
            _logger.warning("security_lookup_failed", mint=mint, error=str(exc))
            return neutral
        if row is None:
            return neutral
        return _SecurityRead(
            approved=bool(row.approved),
            score=float(row.score),
            sub_scores={k: float(v) for k, v in (row.sub_scores or {}).items()},
            facts=dict(row.facts or {}),
        )

    # -- identity -------------------------------------------------------------

    async def _identity(
        self,
        mint: str,
        event: WalletIntelligenceComputed,
        security: _SecurityRead,
        raw: dict[str, float],
    ) -> CandidateIdentity:
        """Establish the cohort keys the memory is indexed by.

        Every field is optional and every source is best-effort: an unknown key
        means one enrichment dimension stays silent, which is the correct
        behaviour. Guessing a developer or a narrative would poison a cohort
        permanently — a wrong label is worse than a missing one, because the
        memory has no way to notice it later.
        """
        facts = security.facts
        developer = _text(_sub(facts, "developer").get("deployer"))
        liquidity = raw.get("basic.liquidity")
        volatility = raw.get("tech.volatility")

        snapshot = event.snapshot
        cluster_id: str | None = None
        if snapshot.clusters:
            # The dominant cluster is the identity that matters: it is the
            # entity whose coordinated behaviour this token would inherit.
            cluster_id = max(snapshot.clusters, key=lambda c: c.pct_supply).cluster_id

        wallets = self._wallets(snapshot, facts)
        launchpad, narrative = await self._metadata(mint)

        return CandidateIdentity(
            mint=mint,
            developer=developer,
            launchpad=launchpad,
            narrative=narrative,
            cluster_id=cluster_id,
            wallets=wallets,
            liquidity_usd=liquidity,
            volatility=volatility,
        )

    @staticmethod
    def _wallets(snapshot: IntelligenceSnapshot, facts: dict[str, Any]) -> tuple[str, ...]:
        """The wallets around this token, deduplicated and bounded."""
        found: list[str] = []
        for cluster in snapshot.clusters:
            for member in cluster.members:
                text = _text(member)
                if text:
                    found.append(text)
        holders = _sub(facts, "holder").get("top_holders")
        if isinstance(holders, list):
            for holder in holders:
                text = _text(holder.get("owner") if isinstance(holder, dict) else holder)
                if text:
                    found.append(text)
        return tuple(dict.fromkeys(found))[:_MAX_WALLETS]

    async def _metadata(self, mint: str) -> tuple[str | None, str | None]:
        """Launchpad (the venue that listed it) and narrative (the story it tells)."""
        if self._db is None:
            return (None, None)
        try:
            async with self._db.session() as session:
                row = await session.scalar(
                    select(TokenMetadataRecord)
                    .join(Token, Token.id == TokenMetadataRecord.token_id)
                    .where(Token.mint == mint)
                    .limit(1)
                )
        except Exception as exc:
            _logger.warning("metadata_lookup_failed", mint=mint, error=str(exc))
            return (None, None)
        if row is None:
            return (None, None)
        sources = row.sources if isinstance(row.sources, list) else []
        launchpad = _text(sources[0]) if sources else None
        return (launchpad, narrative_of(row.name, row.symbol, row.description))


def _sub(facts: dict[str, Any], analyzer: str) -> dict[str, Any]:
    """One analyzer's fact bundle out of the assessment's ``{analyzer: facts}``."""
    value = facts.get(analyzer)
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
