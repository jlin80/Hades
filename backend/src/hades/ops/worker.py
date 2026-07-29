"""Background worker — hosts the Scanner (data-acquisition) subsystem.

In the modular monolith the worker is where domain background loops live. It
hosts the :class:`ScannerRuntime` (RPC Manager, DEX sources, Discovery Engine,
Acquisition Pipeline, Feature Engine, History Builder) and the
:class:`SecurityRuntime` (the conservative rug/scam guardrail that reacts to
``FeaturesComputed`` and screens every measured token), the
:class:`IntelligenceRuntime` (wallet knowledge base) and the
:class:`CommitteeRuntime` (the AI Committee — the explainable brain that reacts to
``WalletIntelligenceComputed`` and emits probabilities, never decisions). Together
they realise the flow ``scanner -> features -> security -> intelligence -> committee``.
The supervised skeleton (liveness, event bus, graceful shutdown) comes from
:class:`ServiceProcess`.
"""

from __future__ import annotations

from hades.bootstrap import Container
from hades.ops.audit_runtime import AuditRuntime
from hades.ops.committee_runtime import CommitteeRuntime
from hades.ops.execution_runtime import ExecutionRuntime
from hades.ops.exploration_runtime import ExplorationRuntime
from hades.ops.intelligence_runtime import IntelligenceRuntime
from hades.ops.knowledge_runtime import KnowledgeRuntime
from hades.ops.performance_runtime import PerformanceRuntime
from hades.ops.research_runtime import ResearchRuntime
from hades.ops.risk_runtime import RiskRuntime
from hades.ops.scanner_runtime import ScannerRuntime
from hades.ops.security_runtime import SecurityRuntime
from hades.ops.service import ServiceProcess
from hades.ops.strategy_runtime import StrategyRuntime


class Worker(ServiceProcess):
    """The general background worker process — hosts the Scanner + Security Engine."""

    role = "worker"

    def __init__(self, container: Container | None = None) -> None:
        super().__init__(container)
        self._audit: AuditRuntime | None = None
        self._performance: PerformanceRuntime | None = None
        self._runtime: ScannerRuntime | None = None
        self._security: SecurityRuntime | None = None
        self._intelligence: IntelligenceRuntime | None = None
        self._committee: CommitteeRuntime | None = None
        self._strategy: StrategyRuntime | None = None
        self._risk: RiskRuntime | None = None
        self._execution: ExecutionRuntime | None = None
        self._research: ResearchRuntime | None = None
        self._knowledge: KnowledgeRuntime | None = None
        self._exploration: ExplorationRuntime | None = None

    async def setup(self) -> None:
        # The Audit trail is a platform concern: subscribe it first so every
        # consequential event that follows is recorded. It runs no loop.
        self._audit = AuditRuntime(self._container)
        self._tasks.extend(await self._audit.start())

        # The Performance Monitor measures throughput from the live event stream
        # and publishes latency/throughput snapshots for the dashboard/API.
        self._performance = PerformanceRuntime(self._container)
        self._tasks.extend(await self._performance.start())

        # Permanent memory is subscribed before any producer starts, so nothing
        # the platform learns in its first seconds is lost. It records what every
        # context observes and — the reason it exists — pairs each decision with
        # its realised outcome, which is what gives the AI Committee ground-truth
        # training samples. It cannot trade: it has no concept of an order, a
        # position or a mode, and an AST test forbids it importing one.
        if self._container.settings.knowledge.enabled:
            self._knowledge = KnowledgeRuntime(self._container)
            self._tasks.extend(await self._knowledge.start())
        else:
            self._log.info("knowledge_disabled")

        if self._container.settings.scanner.enabled:
            self._runtime = ScannerRuntime(self._container)
            self._tasks.extend(await self._runtime.start())
        else:
            self._log.info("scanner_disabled")

        # The Security Engine subscribes to FeaturesComputed; it must be wired
        # before/independently of the Scanner so no measured token is unscreened.
        if self._container.settings.security.enabled:
            self._security = SecurityRuntime(self._container)
            self._tasks.extend(await self._security.start())
        else:
            self._log.info("security_disabled")

        # Wallet Intelligence reacts to SecurityScoreComputed, building the
        # permanent on-chain knowledge base. It only knows — it never trades.
        if self._container.settings.intelligence.enabled:
            self._intelligence = IntelligenceRuntime(self._container)
            self._tasks.extend(await self._intelligence.start())
        else:
            self._log.info("intelligence_disabled")

        # The AI Committee reacts to WalletIntelligenceComputed (the last analytical
        # stage). It emits probabilities + explanations only — it never trades.
        if (
            self._container.settings.learning.enabled
            and self._container.settings.learning.committee_enabled
        ):
            self._committee = CommitteeRuntime(self._container)
            self._tasks.extend(await self._committee.start())
        else:
            self._log.info("committee_disabled")

        # The Strategy Engine reacts to CommitteePredictionGenerated, runs the
        # modular strategy roster and emits per-strategy + fused ensemble signals.
        # It only detects opportunities — it never executes, sizes or approves a
        # trade; the Risk Manager remains the sole decision-maker.
        if self._container.settings.strategy.enabled:
            self._strategy = StrategyRuntime(self._container)
            self._tasks.extend(await self._strategy.start())
        else:
            self._log.info("strategy_disabled")

        # The exploration programme is built before the Risk Manager because the
        # guardian holds it as a collaborator. It is built even when disabled: a
        # disabled programme still answers "why are you off?" and still serves
        # its status endpoint, so an operator can read the evidence census and
        # the budget *before* deciding to switch it on. It spends nothing, runs
        # no loop that touches a token, and cannot approve anything — the Risk
        # Manager may only ask it about a candidate its conviction gates muted.
        self._exploration = ExplorationRuntime(self._container)
        self._tasks.extend(await self._exploration.start())

        # The Risk Manager reacts to CommitteePredictionGenerated (the end of the
        # pipeline) and the Portfolio Manager to the Position stream. The Risk
        # Manager is the only component that may approve a trade; it never
        # executes one.
        if self._container.settings.risk.enabled:
            self._risk = RiskRuntime(self._container, self._exploration.risk_port)
            self._tasks.extend(await self._risk.start())
        else:
            self._log.info("risk_disabled")

        # The Execution Engine reacts to TradeApproved (a Risk-Manager permission)
        # and turns it into a real (live) or simulated (paper) order. It is the
        # ONLY component that knows the mode; it defaults to — and can only be —
        # paper unless the hard live gate and live adapters are both present.
        if self._container.settings.execution.enabled:
            self._execution = ExecutionRuntime(self._container)
            self._tasks.extend(await self._execution.start())
        else:
            self._log.info("execution_disabled")

        # The Research Lab runs entirely offline: it subscribes to FeaturesComputed
        # to drive VIRTUAL shadow strategies and studies copied history. It has no
        # Execution/Risk/Portfolio collaborator, so it can never place an order or
        # enable live trading — it only produces knowledge.
        if self._container.settings.research.lab_enabled:
            self._research = ResearchRuntime(self._container)
            self._tasks.extend(await self._research.start())
        else:
            self._log.info("research_disabled")

        self._log.info(
            "worker_ready",
            audit="running" if self._audit else "off",
            performance="running" if self._performance else "off",
            scanner="running" if self._runtime else "off",
            security="running" if self._security else "off",
            intelligence="running" if self._intelligence else "off",
            committee="running" if self._committee else "off",
            strategy="running" if self._strategy else "off",
            risk="running" if self._risk else "off",
            execution="running" if self._execution else "off",
            research="running" if self._research else "off",
            knowledge="running" if self._knowledge else "off",
            exploration=(
                "granting"
                if self._exploration is not None and self._container.settings.exploration.enabled
                else "off"
            ),
        )

    async def teardown(self) -> None:
        if self._audit is not None:
            await self._audit.stop()
        if self._performance is not None:
            await self._performance.stop()
        if self._runtime is not None:
            await self._runtime.stop()
        if self._security is not None:
            await self._security.stop()
        if self._intelligence is not None:
            await self._intelligence.stop()
        if self._committee is not None:
            await self._committee.stop()
        if self._strategy is not None:
            await self._strategy.stop()
        if self._risk is not None:
            await self._risk.stop()
        if self._execution is not None:
            await self._execution.stop()
        if self._research is not None:
            await self._research.stop()
        if self._knowledge is not None:
            await self._knowledge.stop()
        if self._exploration is not None:
            await self._exploration.stop()
