"""Security Engine — composes the analyzers into one conservative verdict.

The engine orchestrates; it does not itself analyse chains or place trades. Given
a pre-assembled :class:`SecurityInputs` context it:

    1. Announces ``SecurityAnalysisStarted``.
    2. Fails closed on any blacklist hit (token / deployer / pool) — an immediate
       veto, no further analysis needed.
    3. Runs every analyzer (each isolated: one throwing never breaks the rest).
    4. Folds in whitelist reputation (positives; can waive *doubt*, never a
       critical flag).
    5. Scores + explains the result.
    6. Persists the assessment for audit **and research** — rejected tokens are
       stored too, they are the most valuable training data.
    7. Grows the developer reputation and, on a definitive honeypot, the token
       blacklist.
    8. Emits granular risk events plus ``TokenApproved`` / ``TokenRejected`` and
       ``SecurityScoreComputed``.

Its entire output is a recommendation surface for the (later) AI Committee. It
never buys, sells, or sizes anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from hades.contexts.common.domain.value_objects import Score
from hades.contexts.security.application.explainability import build_explanation
from hades.contexts.security.application.lists import (
    BlacklistEngine,
    ListKind,
    WhitelistEngine,
)
from hades.contexts.security.application.metrics import SecurityMetrics
from hades.contexts.security.application.scoring import SecurityScorer
from hades.contexts.security.domain.events import (
    ClusterFound,
    ContractRiskDetected,
    DeveloperRisk,
    LiquidityWarning,
    SecurityAnalysisStarted,
    SecurityScoreComputed,
    TokenApproved,
    TokenRejected,
)
from hades.contexts.security.domain.models import (
    AnalyzerReport,
    Explanation,
    PositiveSignal,
    RiskFlag,
    RiskSeverity,
    SecurityAssessment,
    SecurityDecision,
    SecurityInputs,
)
from hades.contexts.security.domain.ports import (
    DeveloperReputationStore,
    SecurityCheck,
    SecurityRepository,
)
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("security.engine")

# Flags whose codes come from the contract/authority dimension (routed to the
# ContractRiskDetected event).
_CONTRACT_CODES = frozenset(
    {
        "MINT_AUTHORITY_ACTIVE",
        "FREEZE_AUTHORITY_ACTIVE",
        "MINT_ACCOUNT_UNREADABLE",
        "MINT_UNINITIALIZED",
        "UNKNOWN_TOKEN_PROGRAM",
        "TOKEN2022_RISKY_EXTENSION",
        "METADATA_MUTABLE",
    }
)


class SecurityEngine:
    """Runs analyzers, aggregates, decides, persists and announces — never trades."""

    def __init__(
        self,
        *,
        analyzers: Sequence[SecurityCheck],
        scorer: SecurityScorer,
        blacklist: BlacklistEngine,
        whitelist: WhitelistEngine,
        repository: SecurityRepository,
        developer_store: DeveloperReputationStore,
        event_bus: EventBus,
        metrics: SecurityMetrics,
        auto_blacklist_honeypots: bool = True,
    ) -> None:
        self._analyzers = tuple(analyzers)
        self._scorer = scorer
        self._blacklist = blacklist
        self._whitelist = whitelist
        self._repo = repository
        self._dev_store = developer_store
        self._bus = event_bus
        self._metrics = metrics
        self._auto_blacklist = auto_blacklist_honeypots

    async def analyze(self, inputs: SecurityInputs) -> SecurityAssessment:
        started = asyncio.get_running_loop().time()
        correlation = new_id()
        token = inputs.token
        await self._bus.publish(
            SecurityAnalysisStarted(aggregate_id=new_id(), correlation_id=correlation, token=token)
        )
        try:
            assessment = await self._run(inputs, correlation)
        except Exception as exc:  # analysis must never crash the subscriber
            self._metrics.errors.inc()
            _logger.error("security_analysis_failed", mint=str(token.mint), error=str(exc))
            raise
        finally:
            elapsed = asyncio.get_running_loop().time() - started
            self._metrics.analysis_seconds.observe(elapsed)
        return assessment

    # -- orchestration --------------------------------------------------------

    async def _run(self, inputs: SecurityInputs, correlation: str) -> SecurityAssessment:
        started = asyncio.get_running_loop().time()
        token = inputs.token

        blacklist_hit = await self._blacklist_veto(inputs)
        if blacklist_hit is not None:
            assessment = self._blacklisted_assessment(inputs, blacklist_hit)
            await self._finalise(inputs, assessment, correlation, started, reports=[])
            return assessment

        reports = self._analyze_all(inputs)
        trusted = await self._is_trusted(inputs)
        result = self._scorer.score(reports, trusted=trusted)

        positives = list(result.positives)
        if trusted:
            positives.append(
                PositiveSignal(
                    code="WHITELISTED",
                    detail="Token or deployer is on the trusted whitelist.",
                )
            )

        explanation = build_explanation(
            decision=result.decision,
            final_score=result.final.value,
            flags=result.flags,
            positives=positives,
            reject_reasons=result.reject_reasons,
        )
        assessment = SecurityAssessment(
            token=token,
            decision=result.decision,
            score=result.final,
            sub_scores=result.sub_scores,
            flags=result.flags,
            positives=tuple(positives),
            explanation=explanation,
            facts={r.name: dict(r.facts) for r in reports},
            analyzed_at=inputs.now,
            duration_ms=round((asyncio.get_running_loop().time() - started) * 1000.0, 2),
        )
        await self._finalise(inputs, assessment, correlation, started, reports=reports)
        return assessment

    def _analyze_all(self, inputs: SecurityInputs) -> list[AnalyzerReport]:
        started = asyncio.get_running_loop().time()
        try:
            return self._analyze_each(inputs)
        finally:
            # Metered separately from the assembler so "is it the analyzers or the
            # I/O?" is a question the dashboard answers rather than one that needs
            # a reading of the code. The answer has been the I/O by three orders of
            # magnitude, and a number on record is what keeps that from being
            # re-litigated from intuition next time the pipeline falls behind.
            self._metrics.analyzers_seconds.observe(
                asyncio.get_running_loop().time() - started
            )

    def _analyze_each(self, inputs: SecurityInputs) -> list[AnalyzerReport]:
        reports: list[AnalyzerReport] = []
        for analyzer in self._analyzers:
            try:
                reports.append(analyzer.analyze(inputs))
            except Exception as exc:  # one bad analyzer must not sink the verdict
                _logger.warning("analyzer_failed", analyzer=analyzer.name, error=str(exc))
                reports.append(
                    AnalyzerReport(
                        name=analyzer.name,
                        score=Score(value=0.0),
                        flags=(
                            RiskFlag(
                                code="ANALYZER_ERROR",
                                severity=RiskSeverity.HIGH,
                                detail=f"{analyzer.name} analyzer failed: {exc}",
                            ),
                        ),
                        insufficient_data=True,
                    )
                )
        return reports

    # -- blacklist / whitelist ------------------------------------------------

    async def _blacklist_veto(self, inputs: SecurityInputs) -> str | None:
        mint = str(inputs.token.mint)
        if await self._blacklist.is_blacklisted(ListKind.TOKEN, mint):
            return f"Token {mint} is blacklisted."
        if inputs.deployer is not None and await self._blacklist.is_blacklisted(
            ListKind.DEPLOYER, str(inputs.deployer)
        ):
            return f"Deployer {inputs.deployer} is blacklisted."
        if inputs.pool is not None and await self._blacklist.is_blacklisted(
            ListKind.POOL, inputs.pool.address
        ):
            return f"Pool {inputs.pool.address} is blacklisted."
        return None

    async def _is_trusted(self, inputs: SecurityInputs) -> bool:
        if await self._whitelist.is_whitelisted(ListKind.TOKEN, str(inputs.token.mint)):
            return True
        return inputs.deployer is not None and await self._whitelist.is_whitelisted(
            ListKind.DEPLOYER, str(inputs.deployer)
        )

    def _blacklisted_assessment(self, inputs: SecurityInputs, reason: str) -> SecurityAssessment:
        flag = RiskFlag(code="BLACKLISTED", severity=RiskSeverity.CRITICAL, detail=reason)
        return SecurityAssessment(
            token=inputs.token,
            decision=SecurityDecision.REJECTED,
            score=Score(value=0.0),
            sub_scores={},
            flags=(flag,),
            explanation=Explanation(summary=f"REJECTED — {reason}", negatives=(reason,)),
            analyzed_at=inputs.now,
        )

    # -- finalisation: persist, learn, announce, meter ------------------------

    async def _finalise(
        self,
        inputs: SecurityInputs,
        assessment: SecurityAssessment,
        correlation: str,
        started: float,
        *,
        reports: list[AnalyzerReport],
    ) -> None:
        await self._persist(assessment)
        await self._learn(inputs, reports)
        await self._maybe_blacklist_honeypot(inputs, assessment)
        await self._emit_events(inputs, assessment, correlation, reports)
        self._meter(assessment, reports)

    async def _persist(self, assessment: SecurityAssessment) -> None:
        try:
            await self._repo.save(assessment)
        except Exception as exc:  # research storage is best-effort
            _logger.warning("assessment_persist_failed", error=str(exc))

    async def _learn(self, inputs: SecurityInputs, reports: list[AnalyzerReport]) -> None:
        if inputs.deployer is None:
            return
        try:
            await self._dev_store.observe_token(inputs.deployer, mint=str(inputs.token.mint))
        except Exception as exc:
            _logger.warning("developer_observe_failed", error=str(exc))

    async def _maybe_blacklist_honeypot(
        self, inputs: SecurityInputs, assessment: SecurityAssessment
    ) -> None:
        if not self._auto_blacklist:
            return
        if any(f.code in {"CANNOT_SELL", "PUNITIVE_SELL_TAX"} for f in assessment.flags):
            try:
                await self._blacklist.blacklist(
                    ListKind.TOKEN,
                    str(inputs.token.mint),
                    reason="Confirmed honeypot (cannot sell).",
                    source="engine",
                )
            except Exception as exc:
                _logger.warning("auto_blacklist_failed", error=str(exc))

    async def _emit_events(
        self,
        inputs: SecurityInputs,
        assessment: SecurityAssessment,
        correlation: str,
        reports: list[AnalyzerReport],
    ) -> None:
        token = inputs.token
        for f in assessment.flags:
            if f.severity in (RiskSeverity.CRITICAL, RiskSeverity.HIGH):
                if f.code in _CONTRACT_CODES:
                    await self._bus.publish(
                        ContractRiskDetected(
                            aggregate_id=new_id(),
                            correlation_id=correlation,
                            token=token,
                            code=f.code,
                            severity=f.severity.value,
                            detail=f.detail,
                        )
                    )
                elif "LP" in f.code or "LIQUIDITY" in f.code:
                    await self._bus.publish(
                        LiquidityWarning(
                            aggregate_id=new_id(),
                            correlation_id=correlation,
                            token=token,
                            code=f.code,
                            detail=f.detail,
                        )
                    )

        cluster_facts = self._facts(reports, "wallet_cluster")
        if cluster_facts.get("cluster_count", 0):
            await self._bus.publish(
                ClusterFound(
                    aggregate_id=new_id(),
                    correlation_id=correlation,
                    token=token,
                    member_count=_as_int(cluster_facts.get("cluster_count")),
                    pct_supply=_as_float(cluster_facts.get("largest_cluster_pct")),
                )
            )

        if inputs.deployer is not None and any(
            f.code in {"KNOWN_SCAMMER", "SERIAL_RUGGER", "DEPLOYER_PRIOR_RUGS"}
            for f in assessment.flags
        ):
            await self._bus.publish(
                DeveloperRisk(
                    aggregate_id=new_id(),
                    correlation_id=correlation,
                    token=token,
                    deployer=str(inputs.deployer),
                    detail="Deployer carries reputational risk.",
                )
            )

        if assessment.approved:
            await self._bus.publish(
                TokenApproved(
                    aggregate_id=new_id(),
                    correlation_id=correlation,
                    token=token,
                    score=assessment.score.value,
                )
            )
        else:
            reasons = assessment.explanation.negatives if assessment.explanation else ()
            await self._bus.publish(
                TokenRejected(
                    aggregate_id=new_id(),
                    correlation_id=correlation,
                    token=token,
                    score=assessment.score.value,
                    reasons=reasons,
                )
            )

        await self._bus.publish(
            SecurityScoreComputed(
                aggregate_id=new_id(),
                correlation_id=correlation,
                token=token,
                assessment=assessment,
                vetoed=assessment.has_critical_flag,
            )
        )

    def _meter(self, assessment: SecurityAssessment, reports: list[AnalyzerReport]) -> None:
        self._metrics.analyzed.inc()
        self._metrics.security_score.set(assessment.score.value)
        if assessment.approved:
            self._metrics.approved.inc()
        else:
            reason = (
                assessment.explanation.negatives[0][:60]
                if assessment.explanation and assessment.explanation.negatives
                else "unknown"
            )
            self._metrics.rejected.labels(reason=reason).inc()
        if assessment.has_critical_flag:
            self._metrics.vetoes.inc()
        for f in assessment.flags:
            self._metrics.risks.labels(code=f.code, severity=f.severity.value).inc()
        dev = self._facts(reports, "developer")
        if "tokens_created" in dev:
            for r in reports:
                if r.name == "developer":
                    self._metrics.developer_score.set(r.score.value)
        cluster = self._facts(reports, "wallet_cluster")
        clusters = _as_int(cluster.get("cluster_count"))
        if clusters:
            self._metrics.clusters.inc(clusters)

    @staticmethod
    def _facts(reports: list[AnalyzerReport], name: str) -> dict[str, object]:
        for r in reports:
            if r.name == name:
                return dict(r.facts)
        return {}


def _as_int(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0
