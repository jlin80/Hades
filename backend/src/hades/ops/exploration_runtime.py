"""Exploration runtime — wires the cold-start programme and publishes its state.

Process-level composition (the ops layer may import application + infrastructure).
It builds the :class:`ExplorationService` from configuration, points it at
permanent memory for the evidence census and at the ledger for the budget, and
hands the Risk Manager an adapter so the guardian can *ask* it about a candidate
the conviction gates muted.

Three things this module is careful about.

**It is constructed even when disabled.** A disabled programme still answers
``consider`` (with a decline) and still serves ``/exploration/status``, so an
operator can read what it *would* do — the evidence census, the budget, the
reason it is off — before switching it on. A component that only exists when
enabled cannot be inspected before the decision to enable it, which is the wrong
way round for something that spends money.

**The two consequential events become operator alerts here, not in the context.**
Exploration itself imports nothing but the shared kernel; turning
``ExplorationBudgetExhausted`` and ``ExplorationCompleted`` into a notification
is wiring, and wiring lives in ops. ``ExplorationCompleted`` is the one worth
waking up for — it is the platform announcing it has finished buying evidence and
switched the programme off by itself.

**It owns no loop that could trade.** The only task it starts publishes a status
snapshot to Redis for the dashboard. Nothing here reacts to a token, and the
service is reached exclusively through the Risk Manager's port.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

from hades.bootstrap import Container
from hades.contexts.exploration.application.factory import exploration_config_from_settings
from hades.contexts.exploration.application.metrics import ExplorationMetrics
from hades.contexts.exploration.application.service import ExplorationService
from hades.contexts.exploration.domain.events import (
    ExplorationBudgetExhausted,
    ExplorationCompleted,
)
from hades.contexts.exploration.domain.ports import EvidencePort, ExplorationLedgerStore
from hades.contexts.exploration.infrastructure.evidence import KnowledgeEvidence
from hades.contexts.exploration.infrastructure.stores import (
    InMemoryExplorationLedger,
    PostgresExplorationLedger,
)
from hades.contexts.knowledge.domain.ports import LessonStore
from hades.contexts.knowledge.infrastructure.stores import (
    InMemoryLessonStore,
    PostgresLessonStore,
)
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.risk.infrastructure.exploration import ExplorationAdapter
from hades.shared_kernel.cache import CacheService
from hades.shared_kernel.domain.events import DomainEvent
from hades.shared_kernel.logging import get_logger

_logger = get_logger("exploration.runtime")

EXPLORATION_STATUS_NAMESPACE: Final = "exploration"
STATUS_KEY: Final = "status"
_STATUS_TTL_SECONDS: Final = 30


class ExplorationRuntime:
    """Owns the wired exploration programme and its lifecycle."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._stop = asyncio.Event()
        self._metrics = ExplorationMetrics(container.metrics)
        self._cache = CacheService(container.redis, namespace=EXPLORATION_STATUS_NAMESPACE)

        config = exploration_config_from_settings(container.settings)
        self._lessons: LessonStore = self._build_lesson_store()
        self._ledger: ExplorationLedgerStore = self._build_ledger()
        self._evidence: EvidencePort = KnowledgeEvidence(self._lessons, config.target)
        self._service = ExplorationService(
            config,
            self._evidence,
            self._ledger,
            event_bus=container.event_bus,
            metrics=self._metrics,
        )
        self._register()

    # -- construction ---------------------------------------------------------

    def _build_lesson_store(self) -> LessonStore:
        if self._c.database is not None:
            return PostgresLessonStore(self._c.database)
        return InMemoryLessonStore()

    def _build_ledger(self) -> ExplorationLedgerStore:
        if self._c.database is not None:
            return PostgresExplorationLedger(self._c.database)
        return InMemoryExplorationLedger()

    def _register(self) -> None:
        bus = self._c.event_bus
        bus.subscribe(ExplorationCompleted.__name__, self._on_completed)
        bus.subscribe(ExplorationBudgetExhausted.__name__, self._on_budget_exhausted)

    # -- operator alerts ------------------------------------------------------

    async def _on_completed(self, event: DomainEvent) -> None:
        if not isinstance(event, ExplorationCompleted):
            return
        await self._notify(
            title="Exploration complete — the platform has its evidence",
            body=(
                f"{event.lessons} settled lessons ({event.positive} positive, "
                f"{event.negative} negative). Exploration switched itself off; "
                "the AI Committee can now be validated against ground truth."
            ),
            severity=Severity.INFO,
            dedup_key="exploration-complete",
        )

    async def _on_budget_exhausted(self, event: DomainEvent) -> None:
        if not isinstance(event, ExplorationBudgetExhausted):
            return
        await self._notify(
            title=f"Exploration budget exhausted ({event.window})",
            body=(
                f"${event.spent_usd:.2f} of ${event.limit_usd:.2f} spent. "
                + (
                    "This ceiling does not reset — the programme has spent its "
                    "whole allowance without reaching sufficiency."
                    if event.window == "total"
                    else "It resets at the start of the next window."
                )
            ),
            # The lifetime ceiling is the one that needs a human: it means the
            # programme ended for the wrong reason. The others clear themselves.
            severity=Severity.WARNING if event.window == "total" else Severity.INFO,
            dedup_key=f"exploration-budget-{event.window}",
        )

    async def _notify(self, *, title: str, body: str, severity: Severity, dedup_key: str) -> None:
        try:
            await self._c.notification.notify(
                title=title, body=body, severity=severity, channel="alerts", dedup_key=dedup_key
            )
        except Exception as exc:  # an alert must never break the subscriber
            _logger.warning("exploration_notify_failed", error=str(exc))

    # -- status ---------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        status = await self._service.status()
        return {
            "enabled": status.enabled,
            "active": status.active,
            "inactive_reason": status.inactive_reason,
            "evidence": {
                "available": status.evidence.available,
                "lessons": status.evidence.lessons,
                "positive": status.evidence.positive,
                "negative": status.evidence.negative,
                "sufficient": status.evidence.sufficient,
                "summary": status.evidence.summary,
                "target_lessons": status.evidence.target.min_lessons,
                "target_per_class": status.evidence.target.min_per_class,
                "cohort_target": status.evidence.target.cohort_target,
                "cohorts_known": len(status.evidence.cohorts),
            },
            "budget": status.budget.model_dump(mode="json"),
            "spend": status.spend.model_dump(mode="json"),
            "remaining": status.spend.remaining(status.budget),
            "max_trades_ever": status.budget.max_trades_ever,
            "granted_total": status.granted_total,
            "declined_total": status.declined_total,
            "declines_by_reason": status.declines_by_reason,
            # Progress and its two alarms. `budget_exhausts_first` is the one to
            # read first: it says the programme cannot finish on the runway it
            # has left, which is knowable long before the last dollar goes.
            "progress": status.progress.model_dump(mode="json"),
            "updated_at": time.time(),
        }

    async def _publish_status_loop(self) -> None:
        interval = self._c.settings.exploration.status_interval_seconds
        while not self._stop.is_set():
            try:
                await self._cache.set(
                    STATUS_KEY, await self.snapshot(), ttl_seconds=_STATUS_TTL_SECONDS
                )
            except Exception as exc:  # status publishing is best-effort
                _logger.warning("exploration_status_publish_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> list[asyncio.Task[None]]:
        config = self._service.config
        _logger.info(
            "exploration_runtime_started",
            enabled=config.enabled,
            per_trade_usd=config.budget.per_trade_usd,
            total_budget_usd=config.budget.total_usd,
            max_trades_ever=config.budget.max_trades_ever,
            target_lessons=config.target.min_lessons,
        )
        return [asyncio.create_task(self._publish_status_loop(), name="exploration-status")]

    async def stop(self) -> None:
        self._stop.set()
        _logger.info("exploration_runtime_stopped")

    # -- accessors ------------------------------------------------------------

    @property
    def service(self) -> ExplorationService:
        return self._service

    @property
    def risk_port(self) -> ExplorationAdapter:
        """The adapter the Risk Manager holds. Read-only, ask-only."""
        return ExplorationAdapter(self._service)


__all__ = ["EXPLORATION_STATUS_NAMESPACE", "STATUS_KEY", "ExplorationRuntime"]
