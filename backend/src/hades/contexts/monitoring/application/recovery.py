"""Auto-Recovery — try to heal a failing dependency before giving up.

When the Watchdog sees a component turn unhealthy it no longer *only* alerts and
wait for Docker to restart something. It asks the :class:`RecoveryOrchestrator`
to run the bounded recovery actions that apply to that component (reconnect
Redis, reconnect Postgres, flush a corrupt queue, re-elect an RPC endpoint,
reload configuration…). Each component gets a capped number of attempts; a
success resets the counter and notifies recovery; exhausting the attempts
**escalates to Emergency Mode** — the platform stops taking new risk and shouts.

Design choices:

- **Actions are injected**, so the composition (watchdog vs. worker) decides
  which recoveries are even possible in that process — the watchdog can reconnect
  its own Redis/Postgres and flush queues; an RPC failover action is only wired
  where an RPC Manager actually lives.
- Nothing here trades. Emergency Mode is expressed by *publishing a risk-control
  command* the Risk Manager already knows how to obey — monitoring never reaches
  into risk directly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.contexts.risk.domain.events import ACTION_ENTER_EMERGENCY, RiskControlCommandIssued
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import get_logger
from hades.shared_kernel.observability import MetricsRegistry

_logger = get_logger("monitoring.recovery")


@runtime_checkable
class RecoveryAction(Protocol):
    """One bounded attempt to heal a class of failure."""

    @property
    def name(self) -> str: ...

    def handles(self, component: str) -> bool:
        """True if this action can attempt recovery for ``component``."""
        ...

    async def attempt(self) -> bool:
        """Try once. Return True on success. Must not raise (implementations
        catch their own errors and return False)."""
        ...


@runtime_checkable
class EmergencyFlagStore(Protocol):
    """Persists whether the platform is in Emergency Mode (survives restarts)."""

    async def is_active(self) -> bool: ...

    async def set_active(self, active: bool, *, reason: str, actor: str) -> None: ...


class EmergencyMode:
    """Activates/lifts Emergency Mode via a risk-control command + Discord alert."""

    def __init__(
        self,
        *,
        event_bus: EventBus,
        notifier: NotificationPublisher,
        flags: EmergencyFlagStore,
    ) -> None:
        self._bus = event_bus
        self._notifier = notifier
        self._flags = flags

    async def is_active(self) -> bool:
        return await self._flags.is_active()

    async def activate(self, reason: str, *, actor: str = "watchdog") -> bool:
        """Enter Emergency Mode. Idempotent — a no-op if already active."""
        if await self._flags.is_active():
            return False
        await self._flags.set_active(True, reason=reason, actor=actor)
        await self._bus.publish(
            RiskControlCommandIssued(
                aggregate_id=new_id(), action=ACTION_ENTER_EMERGENCY, reason=reason
            )
        )
        await self._notifier.notify(
            title="EMERGENCY MODE ACTIVATED",
            body=reason,
            severity=Severity.CRITICAL,
            tags={"category": "CRITICAL", "topic": "risk", "container": actor},
        )
        _logger.critical("emergency_mode_activated", reason=reason, actor=actor)
        return True


class RecoveryOrchestrator:
    """Runs recovery actions per failing component; escalates on exhaustion."""

    def __init__(
        self,
        actions: list[RecoveryAction],
        *,
        emergency: EmergencyMode,
        notifier: NotificationPublisher,
        metrics: MetricsRegistry | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._actions = actions
        self._emergency = emergency
        self._notifier = notifier
        self._max_attempts = max_attempts
        self._attempts: dict[str, int] = {}
        self._counter = (
            metrics.counter(
                "hades_recovery_attempts_total",
                "Auto-recovery attempts",
                ("component", "outcome"),
            )
            if metrics is not None
            else None
        )

    async def recover(self, component: str, detail: str = "") -> bool:
        """Attempt to recover ``component``. Escalate to Emergency on exhaustion.

        Escalation requires attempts to have been *made and failed*. Two cases
        that are not that, and must not escalate:

        * **No action handles this component.** This process cannot reach it —
          ``api``, ``dashboard``, ``resources`` and ``rpc`` have no action wired in
          the Watchdog, which can only reconnect its own Redis/Postgres and reload
          config. Counting "there was nothing to try" as a failed attempt drove the
          counter up every cycle and, after three cycles, tripped Emergency Mode
          over a component the Watchdog was never able to heal — halting risk-taking
          for a reason no recovery could ever clear. The component being down is
          still alerted on by the health monitor; that path is unchanged.
        * **Already exhausted.** Emergency Mode is idempotent, but re-running the
          actions and re-logging a WARNING on every cycle produced counters like
          ``attempt=49 max_attempts=3``, which reads as nonsense to an operator and
          buries real events. Once exhausted we stay quiet until :meth:`reset`.
        """
        actions = [a for a in self._actions if a.handles(component)]
        if not actions:
            _logger.debug("component_no_recovery_action", component=component)
            return False

        if self._attempts.get(component, 0) >= self._max_attempts:
            _logger.debug("component_recovery_exhausted", component=component)
            return False

        self._attempts[component] = self._attempts.get(component, 0) + 1
        attempt_no = self._attempts[component]

        for action in actions:
            ok = await self._run(action)
            if ok:
                self._attempts[component] = 0
                self._count(component, "recovered")
                _logger.info("component_recovered", component=component, action=action.name)
                await self._notify_recovered(component, action.name)
                return True

        self._count(component, "failed")
        _logger.warning(
            "component_recovery_failed",
            component=component,
            attempt=attempt_no,
            max_attempts=self._max_attempts,
        )
        if attempt_no >= self._max_attempts:
            await self._emergency.activate(
                f"'{component}' unrecoverable after {attempt_no} attempts: {detail}".strip()
            )
        return False

    def reset(self, component: str) -> None:
        """Clear the attempt counter (a component came back healthy on its own)."""
        self._attempts.pop(component, None)

    async def _run(self, action: RecoveryAction) -> bool:
        try:
            return await action.attempt()
        except Exception as exc:  # an action must never raise into the loop
            _logger.warning("recovery_action_raised", action=action.name, error=str(exc))
            return False

    def _count(self, component: str, outcome: str) -> None:
        if self._counter is not None:
            self._counter.labels(component=component, outcome=outcome).inc()

    async def _notify_recovered(self, component: str, action: str) -> None:
        try:
            await self._notifier.notify(
                title=f"Recovered: {component}",
                body=f"Auto-recovery succeeded via '{action}'.",
                severity=Severity.INFO,
                tags={"category": "SUCCESS", "topic": "health", "component": component},
            )
        except Exception as exc:  # notification is best-effort
            _logger.warning("recovery_notify_failed", error=str(exc))
