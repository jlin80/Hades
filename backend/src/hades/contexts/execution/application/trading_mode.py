"""Trading-mode service — the guarded paper↔live switch.

Switching to live is the most dangerous action on the platform, so it can never
happen implicitly. This service enforces the full protocol the operator sees on
the dashboard:

1. the switch **cannot enable live directly** — it must pass verification;
2. readiness is verified (hard env gate, wallet, RPC, risk config);
3. an explicit confirmation is required;
4. the change is persisted (DB authority), audited and recorded as a risk event;
5. a :class:`TradingModeChanged` event is emitted and a Discord alert requested.

The effective ``is_live`` predicate always ANDs the persisted mode with the
hard env gate ``HADES_LIVE_TRADING_ENABLED`` — the DB can never bypass it, so the
posture is never inconsistent with :attr:`Settings.is_live` semantics.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from hades.contexts.execution.domain.events import TradingModeChanged
from hades.contexts.execution.domain.ports import ProductionChecklistPort
from hades.contexts.execution.infrastructure.trading_mode_repository import (
    TradingModeRepository,
)
from hades.contexts.notification.application.publisher import NotificationPublisher
from hades.contexts.notification.domain.ports import Severity
from hades.shared_kernel.config import Settings
from hades.shared_kernel.config.settings import TradingMode
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.errors import PermissionDeniedError, ValidationError
from hades.shared_kernel.events import EventBus
from hades.shared_kernel.logging import describe, get_logger

_logger = get_logger("execution")

#: Answers "can this platform actually place a live order right now?". The
#: Execution Engine builds its live executor only when the hard gate *and* every
#: live adapter (signer, quote provider, RPC) are present, so this is the single
#: fact that separates real trading from a convincing simulation of it.
LiveExecutorProbe = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class ReadinessReport:
    checks: list[ReadinessCheck]

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks if c.required)


@dataclass(frozen=True)
class ModeStatus:
    mode: str
    is_live: bool
    live_gate_enabled: bool


class TradingModeService:
    """Reads and safely changes the effective trading mode."""

    def __init__(
        self,
        settings: Settings,
        *,
        event_bus: EventBus,
        notifier: NotificationPublisher,
        repository: TradingModeRepository | None = None,
        checklist: ProductionChecklistPort | None = None,
        live_executor_probe: LiveExecutorProbe | None = None,
    ) -> None:
        self._settings = settings
        self._bus = event_bus
        self._notifier = notifier
        self._repo = repository
        self._live_executor_probe = live_executor_probe
        self._checklist = checklist

    async def current(self) -> ModeStatus:
        mode = await self._effective_mode()
        return ModeStatus(
            mode=mode,
            is_live=(mode == TradingMode.LIVE.value and self._settings.live_trading_enabled),
            live_gate_enabled=self._settings.live_trading_enabled,
        )

    async def _effective_mode(self) -> str:
        if self._repo is not None:
            try:
                persisted = await self._repo.get_mode()
                if persisted:
                    return persisted
            except Exception as exc:  # fall back to env on DB trouble
                _logger.warning("mode_read_failed", error=str(exc))
        return self._settings.trading_mode.value

    async def verify_live_readiness(self) -> ReadinessReport:
        """Run every live-readiness check and return a full report."""
        checks: list[ReadinessCheck] = []

        checks.append(
            ReadinessCheck(
                name="live_gate",
                ok=self._settings.live_trading_enabled,
                detail="HADES_LIVE_TRADING_ENABLED must be true",
            )
        )
        checks.append(
            ReadinessCheck(
                name="wallet",
                ok=self._settings.wallet.is_configured,
                detail="a wallet public key must be configured",
            )
        )
        risk = self._settings.risk
        risk_ok = risk.max_position_size_usd > 0 and risk.max_concurrent_positions > 0
        checks.append(
            ReadinessCheck(
                name="risk_manager",
                ok=risk_ok,
                detail="risk limits must be positive",
            )
        )
        checks.append(
            ReadinessCheck(
                name="kill_switch",
                ok=risk.kill_switch_enabled,
                detail="kill switch should be enabled",
                required=False,
            )
        )
        checks.append(await self._check_live_executor())
        checks.append(await self._check_rpc())

        # Phase 10: fold in the platform Production Checklist (infra/subsystems/
        # Emergency Mode). Any required item that fails blocks the switch to live.
        if self._checklist is not None:
            try:
                for name, ok, detail, required in await self._checklist.readiness():
                    checks.append(
                        ReadinessCheck(name=name, ok=ok, detail=detail, required=required)
                    )
            except Exception as exc:  # a checklist failure must fail closed
                checks.append(
                    ReadinessCheck(
                        name="production_checklist",
                        ok=False,
                        detail=f"checklist evaluation failed: {exc}",
                    )
                )
        return ReadinessReport(checks=checks)

    async def _check_live_executor(self) -> ReadinessCheck:
        """Refuse LIVE unless a live executor demonstrably exists.

        ``ExecutionEngine._executor_for`` falls back to the paper executor for
        any mode it has no executor registered for. That is the right failure
        mode for capital — no real order can escape — but on its own it is a
        trap: every other readiness check can pass, the switch succeeds, the
        dashboard reads LIVE, and every fill is simulated. An operator would
        then be reading paper results as real ones, which is worse than being
        unable to go live at all.

        Fail closed. If we cannot confirm a live executor is present, the answer
        is no.
        """
        if self._live_executor_probe is None:
            return ReadinessCheck(
                name="live_executor",
                ok=False,
                detail="live executor state unknown — cannot confirm real orders would be placed",
            )
        try:
            available = await self._live_executor_probe()
        except Exception as exc:  # an unanswerable probe is a failed probe
            return ReadinessCheck(
                name="live_executor",
                ok=False,
                detail=f"live executor state unreadable: {describe(exc)}",
            )
        return ReadinessCheck(
            name="live_executor",
            ok=available,
            detail=(
                "live executor ready"
                if available
                else "no live executor — orders would silently execute as paper"
            ),
        )

    async def _check_rpc(self) -> ReadinessCheck:
        url = self._settings.solana.rpc_http_url
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.timeouts.rpc_seconds) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            return ReadinessCheck(name="rpc", ok=False, detail=f"RPC unreachable: {exc}")
        return ReadinessCheck(name="rpc", ok=True, detail="RPC healthy")

    async def request_switch(
        self, target: TradingMode, *, actor: str, confirm: bool, source_ip: str | None = None
    ) -> ModeStatus:
        """Change the trading mode after enforcing the full safety protocol."""
        current = await self._effective_mode()
        if target.value == current:
            return await self.current()

        if target is TradingMode.LIVE:
            if not confirm:
                raise ValidationError("explicit confirmation is required to enable live trading")
            report = await self.verify_live_readiness()
            if not report.ready:
                failed = [c.name for c in report.checks if c.required and not c.ok]
                raise PermissionDeniedError(
                    "live-trading readiness verification failed",
                    context={"failed_checks": failed},
                )

        if self._repo is not None:
            await self._repo.set_mode(
                target.value, previous_mode=current, actor=actor, source_ip=source_ip
            )

        status = ModeStatus(
            mode=target.value,
            is_live=(target is TradingMode.LIVE and self._settings.live_trading_enabled),
            live_gate_enabled=self._settings.live_trading_enabled,
        )

        await self._bus.publish(
            TradingModeChanged(
                aggregate_id=new_id(),
                previous_mode=current,
                new_mode=target.value,
                is_live=status.is_live,
                actor=actor,
            )
        )
        severity = Severity.CRITICAL if target is TradingMode.LIVE else Severity.WARNING
        await self._notifier.notify(
            title=f"Trading mode → {target.value.upper()}",
            body=f"Changed by {actor}. Effective live: {status.is_live}.",
            severity=severity,
            tags={"topic": "risk", "mode": target.value},
        )
        _logger.warning(
            "trading_mode_changed", previous=current, new=target.value, is_live=status.is_live
        )
        return status
