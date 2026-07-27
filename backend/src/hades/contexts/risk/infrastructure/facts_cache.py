"""Risk facts cache — the real upstream evidence behind a risk candidate.

The :class:`RiskContextBuilder` was designed to join the committee's
probabilities to hard facts from upstream, but no :class:`RiskFactsPort` was ever
implemented, so the builder always fell back to ``NullRiskFactsPort``. The
consequences were not neutral:

* ``security_approved`` defaulted to ``True`` — a token the Security Engine had
  *rejected* reached the Risk Manager looking approved.
* ``security_score`` fell back to the committee's security specialist, whose
  probability is 0.5 (a score of exactly 50) whenever that specialist abstains
  for want of features — below the 60 the policy demands, so low-coverage tokens
  were vetoed on a number that described the committee, not the token.
* ``liquidity_usd`` was always 0, which the liquidity policy reads as "unknown"
  and skips entirely.
* ``developer`` / ``cluster`` were always ``None``, leaving the Exposure and
  Correlation engines with no keys to aggregate concentration on.

This adapter closes that gap without adding any I/O: the facts already travel on
the bus. It listens to the two events that carry them and remembers the latest
per mint, so by the time the committee finishes a token — the very next stage —
the facts are there to be joined.

The cache is deliberately bounded in both size and age. A token the pipeline
dropped mid-flight must not pin memory forever, and a stale verdict must not be
joined to a fresh prediction: expiry degrades to "unknown", which every policy
already handles conservatively.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from hades.contexts.intelligence.domain.events import WalletIntelligenceComputed
from hades.contexts.security.domain.events import SecurityScoreComputed
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("risk.facts")

FactValue = float | bool | str | None


class EventDrivenRiskFacts:
    """Remembers upstream security/wallet facts per mint, from the event stream."""

    def __init__(self, *, max_entries: int = 5_000, ttl_seconds: float = 900.0) -> None:
        self._max = max(1, max_entries)
        self._ttl = max(0.0, ttl_seconds)
        self._facts: OrderedDict[str, tuple[dict[str, FactValue], float]] = OrderedDict()

    def register(self, event_bus: EventBus) -> None:
        event_bus.subscribe(SecurityScoreComputed.__name__, self.on_security)
        event_bus.subscribe(WalletIntelligenceComputed.__name__, self.on_intelligence)

    @property
    def tracked(self) -> int:
        return len(self._facts)

    # -- the RiskFactsPort ----------------------------------------------------

    async def facts_for(self, mint: str) -> dict[str, FactValue]:
        entry = self._facts.get(mint)
        if entry is None:
            return {}
        facts, stored_at = entry
        if self._ttl and (time.monotonic() - stored_at) > self._ttl:
            self._facts.pop(mint, None)
            return {}
        return dict(facts)

    # -- subscriptions --------------------------------------------------------

    async def on_security(self, event: DomainEvent) -> None:
        if not isinstance(event, SecurityScoreComputed):
            return
        try:
            self._merge(str(event.token.mint), _security_facts(event))
        except Exception as exc:  # one malformed assessment must not break the bus
            _logger.warning(
                "risk_facts_security_failed", mint=str(event.token.mint), error=str(exc)
            )

    async def on_intelligence(self, event: DomainEvent) -> None:
        if not isinstance(event, WalletIntelligenceComputed):
            return
        try:
            self._merge(str(event.token.mint), _intelligence_facts(event))
        except Exception as exc:
            _logger.warning(
                "risk_facts_intelligence_failed", mint=str(event.token.mint), error=str(exc)
            )

    # -- internals ------------------------------------------------------------

    def _merge(self, mint: str, new_facts: dict[str, FactValue]) -> None:
        if not new_facts:
            return
        existing = self._facts.pop(mint, None)
        merged: dict[str, FactValue] = dict(existing[0]) if existing else {}
        merged.update(new_facts)
        self._facts[mint] = (merged, time.monotonic())
        while len(self._facts) > self._max:
            self._facts.popitem(last=False)  # evict the oldest


def _security_facts(event: SecurityScoreComputed) -> dict[str, FactValue]:
    assessment = event.assessment
    facts: dict[str, FactValue] = {
        # A veto is a hard block; treat it as un-approved regardless of decision.
        "security_approved": bool(assessment.approved and not event.vetoed),
        "security_score": float(assessment.score.value),
    }

    developer_score = assessment.sub_scores.get("developer")
    if isinstance(developer_score, int | float):
        facts["developer_score"] = float(developer_score)

    analyzer_facts = assessment.facts
    liquidity = _sub_facts(analyzer_facts, "liquidity")
    usd = liquidity.get("liquidity_usd")
    if isinstance(usd, int | float):
        facts["liquidity_usd"] = float(usd)

    developer = _sub_facts(analyzer_facts, "developer")
    deployer = developer.get("deployer")
    if isinstance(deployer, str) and deployer:
        facts["developer"] = deployer
    return facts


def _intelligence_facts(event: WalletIntelligenceComputed) -> dict[str, FactValue]:
    snapshot = event.snapshot
    facts: dict[str, FactValue] = {"wallet_risk": float(snapshot.avg_risk)}
    # The dominant cluster is the correlation key: two tokens held by the same
    # coordinated entity are the same bet, and the Correlation engine caps that.
    if snapshot.clusters:
        largest = max(snapshot.clusters, key=lambda c: c.pct_supply)
        facts["cluster"] = largest.cluster_id
    return facts


def _sub_facts(facts: dict[str, Any], analyzer: str) -> dict[str, Any]:
    value = facts.get(analyzer)
    return value if isinstance(value, dict) else {}
