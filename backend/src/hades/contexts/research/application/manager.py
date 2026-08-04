"""Research Manager — coordinates the whole lab without ever touching production.

The single entry point the runtime and API drive. It owns the engines and the
append-only stores, runs experiments/backtests/optimisations/comparisons, records
everything in the Knowledge Base, and publishes the lab's domain events and the
(sparse) Discord-worthy notifications. It orchestrates knowledge; it never
executes, sizes, or enables a trade — there is no Execution/Risk/Portfolio
collaborator anywhere in its graph.

Design rule: the lab must never block the main system. Every operation here is
pure-compute + append-only I/O, run on the worker's own tasks, and any failure is
swallowed and logged rather than propagated.
"""

from __future__ import annotations

from collections.abc import Sequence

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.research.application.backtest_engine import BacktestingEngine
from hades.contexts.research.application.comparators import ModelComparator, StrategyComparator
from hades.contexts.research.application.experiment_engine import ExperimentEngine
from hades.contexts.research.application.feature_discovery import FeatureDiscoveryEngine
from hades.contexts.research.application.knowledge_base import KnowledgeBase
from hades.contexts.research.application.metrics import multi_objective_score
from hades.contexts.research.application.monte_carlo import MonteCarloEngine
from hades.contexts.research.application.optimizer import ParameterOptimizer
from hades.contexts.research.application.promotion import PromotionEngine
from hades.contexts.research.application.replay_engine import ReplayEngine, ReplayResult
from hades.contexts.research.application.reports import ReportGenerator
from hades.contexts.research.application.strategies import evaluate
from hades.contexts.research.application.validation import ValidationEngine, ValidationReport
from hades.contexts.research.application.walk_forward import WalkForwardEngine
from hades.contexts.research.domain.events import (
    BacktestCompleted,
    CandidateProposed,
    ExperimentFinished,
    ExperimentStarted,
    FeatureProposed,
    ModelCompared,
    MonteCarloCompleted,
    PromotionRejected,
    ReplayCompleted,
    ResearchReportGenerated,
    ResearchStrategyPromoted,
    StrategyCompared,
    WalkForwardCompleted,
)
from hades.contexts.research.domain.models import (
    BacktestResult,
    CandidateKind,
    CandidateStrategy,
    ExperimentResult,
    ExperimentSpec,
    ModelComparison,
    MonteCarloResult,
    PerformanceMetrics,
    PromotionCriteria,
    PromotionDecision,
    ResearchReport,
    ShadowStrategy,
    StrategyComparison,
    ValidationStage,
    WalkForwardResult,
)
from hades.contexts.research.domain.ports import (
    BacktestStore,
    CandidateStore,
    ExperimentStore,
    HistoricalDataReader,
    HistoricalSample,
    PromotionStore,
    ReportStore,
)
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger

_logger = get_logger("research.manager")


class ResearchUnavailableError(RuntimeError):
    """A study was requested that this manager has no collaborator to run."""


class ResearchManager:
    """Coordinates experiments, studies, comparisons, promotion and reporting."""

    def __init__(
        self,
        *,
        event_bus: EventBus,
        notifier: NotificationPublisher,
        knowledge: KnowledgeBase,
        experiment_store: ExperimentStore,
        backtest_store: BacktestStore,
        candidate_store: CandidateStore,
        promotion_store: PromotionStore,
        report_store: ReportStore,
        historical_reader: HistoricalDataReader | None = None,
        criteria: PromotionCriteria | None = None,
    ) -> None:
        self._bus = event_bus
        self._notifier = notifier
        self._kb = knowledge
        self._experiments = experiment_store
        self._backtests = backtest_store
        self._candidates = candidate_store
        self._promotions = promotion_store
        self._reports = report_store
        self._reader = historical_reader

        self._bt = BacktestingEngine()
        self._wf = WalkForwardEngine(self._bt)
        self._mc = MonteCarloEngine()
        self._opt = ParameterOptimizer(self._bt)
        self._fd = FeatureDiscoveryEngine()
        self._experiment_engine = ExperimentEngine(
            backtester=self._bt,
            walk_forward=self._wf,
            monte_carlo=self._mc,
            optimizer=self._opt,
            feature_discovery=self._fd,
        )
        self._validation = ValidationEngine(
            backtester=self._bt, walk_forward=self._wf, monte_carlo=self._mc
        )
        self._promotion = PromotionEngine(criteria)
        self._comparator = StrategyComparator()
        self._model_comparator = ModelComparator()
        self._reporter = ReportGenerator()
        # Built only when a reader is available: a replay is a read of copied
        # history, so without one there is nothing to replay and the method says
        # so rather than failing obscurely deeper down.
        self._replay = ReplayEngine(historical_reader) if historical_reader is not None else None

    # -- experiments ----------------------------------------------------------

    async def run_experiment(
        self, spec: ExperimentSpec, samples: Sequence[HistoricalSample]
    ) -> ExperimentResult:
        await self._publish(
            ExperimentStarted(
                aggregate_id=new_id(),
                experiment_id=str(spec.experiment_id),
                name=spec.name,
                kind=spec.kind.value,
                hypothesis=spec.hypothesis,
            )
        )
        result = self._experiment_engine.run(spec, samples)
        await self._experiments.save(result)
        await self._kb.record_experiment(result)
        await self._publish(
            ExperimentFinished(
                aggregate_id=new_id(),
                experiment_id=str(result.experiment_id),
                name=result.name,
                kind=result.kind.value,
                status=result.status.value,
                metrics=result.metrics,
                conclusion=result.conclusion,
            )
        )
        return result

    async def run_backtest(
        self,
        *,
        candidate: CandidateStrategy,
        samples: Sequence[HistoricalSample],
    ) -> BacktestResult:
        result = self._bt.run(strategy=candidate.name, genome=candidate.genome, samples=samples)
        await self._backtests.save(result)
        await self._publish(
            BacktestCompleted(aggregate_id=new_id(), strategy=candidate.name, result=result)
        )
        await self._notify(
            title="Backtest completed",
            body=(
                f"{candidate.name}: return {result.metrics.total_return:.1%}, "
                f"PF {result.metrics.profit_factor:.2f}, DD {result.metrics.max_drawdown:.1%}"
            ),
        )
        return result

    async def run_walk_forward(
        self, *, candidate: CandidateStrategy, samples: Sequence[HistoricalSample], folds: int = 4
    ) -> WalkForwardResult:
        result = self._wf.run(
            strategy=candidate.name, genome=candidate.genome, samples=samples, folds=folds
        )
        await self._publish(
            WalkForwardCompleted(aggregate_id=new_id(), strategy=candidate.name, result=result)
        )
        return result

    async def run_replay(
        self,
        *,
        from_iso: str,
        to_iso: str,
        shadows: Sequence[ShadowStrategy] = (),
        limit: int = 100_000,
    ) -> ReplayResult:
        """Replay a historical window through virtual shadow strategies.

        The Replay Engine has existed since Phase 9 and was never wired to
        anything: no caller, and ``ReplayCompleted`` was registered on the bus but
        never published. A study nobody can run produces no knowledge, which is
        precisely the failure mode the architecture audit catalogued. It is
        reachable now, and its completion is a fact on the bus like every other
        finished study.

        It reads copied history through a read-only reader and drives only shadow
        runners, so it cannot place an order or mutate production state.
        """
        if self._replay is None:
            raise ResearchUnavailableError(
                "replay requires a historical reader; none was wired into this manager"
            )
        result = await self._replay.replay(
            from_iso=from_iso, to_iso=to_iso, shadows=shadows, limit=limit
        )
        await self._publish(
            ReplayCompleted(
                aggregate_id=new_id(),
                from_iso=result.from_iso,
                to_iso=result.to_iso,
                events_replayed=result.events_replayed,
            )
        )
        return result

    async def run_monte_carlo(
        self,
        *,
        candidate: CandidateStrategy,
        samples: Sequence[HistoricalSample],
        simulations: int = 1000,
    ) -> MonteCarloResult:
        trades = evaluate(candidate.genome, samples)
        result = self._mc.run(strategy=candidate.name, trades=trades, simulations=simulations)
        await self._publish(
            MonteCarloCompleted(aggregate_id=new_id(), strategy=candidate.name, result=result)
        )
        return result

    # -- feature discovery ----------------------------------------------------

    async def discover_features(self, samples: Sequence[HistoricalSample]) -> None:
        for proposal in self._fd.analyze(samples):
            await self._publish(FeatureProposed(aggregate_id=new_id(), proposal=proposal))
        # Proposals are advisory only — nothing is added or removed automatically.

    # -- comparisons ----------------------------------------------------------

    async def compare_strategies(
        self, scorecards: dict[str, PerformanceMetrics], *, objective: str = "expectancy"
    ) -> StrategyComparison:
        comparison = self._comparator.compare(scorecards, objective=objective)
        await self._kb.record_comparison(comparison)
        await self._publish(StrategyCompared(aggregate_id=new_id(), comparison=comparison))
        return comparison

    async def compare_models(
        self,
        *,
        production: dict[str, float],
        candidate: dict[str, float],
        shadow: dict[str, float] | None = None,
    ) -> ModelComparison:
        comparison = self._model_comparator.compare(
            production=production, candidate=candidate, shadow=shadow
        )
        await self._publish(ModelCompared(aggregate_id=new_id(), comparison=comparison))
        return comparison

    # -- validation + promotion (fail-closed, human-gated) --------------------

    def validate(
        self,
        candidate: CandidateStrategy,
        *,
        train: Sequence[HistoricalSample],
        validation: Sequence[HistoricalSample],
        forward: Sequence[HistoricalSample],
    ) -> ValidationReport:
        return self._validation.run(candidate, train=train, validation=validation, forward=forward)

    async def evaluate_promotion(
        self,
        candidate: CandidateStrategy,
        *,
        metrics: PerformanceMetrics | None = None,
        kind: CandidateKind = CandidateKind.STRATEGY,
        paper_positive: bool = False,
        shadow_positive: bool = False,
        stage: ValidationStage = ValidationStage.SHADOW,
        manual_approve: bool = False,
    ) -> PromotionDecision:
        """Evaluate a candidate against the promotion bar.

        Returns a decision only. When APPROVED *and* manually approved by a human,
        a :class:`ResearchStrategyPromoted` governance event is emitted — which still does
        not deploy anything, place an order, or enable live trading. Otherwise a
        :class:`PromotionRejected` event records why the bar was not cleared.
        """
        decision = self._promotion.evaluate(
            candidate,
            metrics=metrics,
            kind=kind,
            paper_positive=paper_positive,
            shadow_positive=shadow_positive,
            stage=stage,
            manual_approve=manual_approve,
        )
        await self._promotions.save(decision)
        await self._kb.record_promotion(decision)
        if decision.promotable:
            await self._publish(ResearchStrategyPromoted(aggregate_id=new_id(), decision=decision))
            await self._notify(
                title="Strategy promotion recommended",
                body=(
                    f"{decision.candidate_name} met every criterion and was human-approved. "
                    "Deployment remains a manual, out-of-lab action."
                ),
                severity=Severity.WARNING,
            )
        else:
            await self._publish(PromotionRejected(aggregate_id=new_id(), decision=decision))
        return decision

    async def propose_candidate(
        self,
        candidate: CandidateStrategy,
        *,
        summary: str,
        kind: CandidateKind = CandidateKind.STRATEGY,
    ) -> None:
        await self._candidates.save(candidate)
        await self._publish(
            CandidateProposed(
                aggregate_id=new_id(),
                candidate_id=str(candidate.candidate_id),
                kind=kind.value,
                summary=summary,
            )
        )
        await self._notify(title="New candidate strategy", body=f"{candidate.name}: {summary}")

    # -- reporting ------------------------------------------------------------

    async def generate_report(
        self,
        *,
        period: str = "weekly",
        comparison: StrategyComparison | None = None,
    ) -> ResearchReport:
        experiments = await self._experiments.recent(limit=100)
        promotions = await self._promotions.recent(limit=50)
        report = self._reporter.generate(
            period=period,
            experiments=experiments,
            comparison=comparison,
            promotions=promotions,
        )
        await self._reports.save(report)
        await self._publish(ResearchReportGenerated(aggregate_id=new_id(), report=report))
        if period == "weekly":
            await self._notify(title="Weekly research report", body=report.summary)
        return report

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def score(metrics: PerformanceMetrics) -> float:
        return multi_objective_score(metrics)

    async def _publish(self, event: object) -> None:
        try:
            await self._bus.publish(event)  # type: ignore[arg-type]
        except Exception as exc:  # the lab must never disrupt the bus
            _logger.warning("research_publish_failed", error=str(exc))

    async def _notify(self, *, title: str, body: str, severity: Severity = Severity.INFO) -> None:
        try:
            await self._notifier.notify(
                title=title, body=body, severity=severity, tags={"context": "research"}
            )
        except Exception as exc:  # notifications are best-effort
            _logger.debug("research_notify_failed", error=str(exc))
