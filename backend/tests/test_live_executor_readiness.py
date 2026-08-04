"""Live readiness must not pass when no live executor exists.

``ExecutionEngine._executor_for`` falls back to the paper executor for any mode
it has no executor registered for. For capital that is the right failure mode —
no real order can escape — but on its own it is a trap. The live adapters
(signer, quote provider, RPC gateway) are unimplemented, so every other check
could pass, the switch to LIVE would succeed, the dashboard would read LIVE, and
every fill would be simulated. An operator reading paper results as real ones is
worse off than one who simply cannot go live.

The probe therefore fails closed: anything short of a confirmed live executor is
a blocked switch.
"""

from __future__ import annotations

import asyncio

import pytest

from hades.contexts.execution.application.trading_mode import TradingModeService
from hades.shared_kernel.config import Settings
from hades.shared_kernel.events import InMemoryEventBus


class _Notifier:
    async def notify(self, **_: object) -> None:
        return None


def _service(probe: object = None) -> TradingModeService:
    return TradingModeService(
        Settings(),
        event_bus=InMemoryEventBus(),
        notifier=_Notifier(),  # type: ignore[arg-type]
        live_executor_probe=probe,  # type: ignore[arg-type]
    )


def _check(service: TradingModeService) -> object:
    report = asyncio.run(service.verify_live_readiness())
    found = [c for c in report.checks if c.name == "live_executor"]
    assert found, "the live_executor check must always be reported"
    return found[0]


def test_an_absent_probe_blocks_live() -> None:
    """No probe means no evidence, and no evidence means no."""
    check = _check(_service(None))

    assert check.ok is False  # type: ignore[attr-defined]
    assert check.required is True  # type: ignore[attr-defined]


def test_a_probe_reporting_no_executor_blocks_live() -> None:
    async def probe() -> bool:
        return False

    check = _check(_service(probe))

    assert check.ok is False  # type: ignore[attr-defined]
    # The detail has to name the consequence: silence here is what misleads.
    assert "paper" in check.detail  # type: ignore[attr-defined]


def test_a_failing_probe_blocks_live_rather_than_being_ignored() -> None:
    """An unreachable Worker must not be read as permission to trade live."""

    async def probe() -> bool:
        raise RuntimeError("redis down")

    check = _check(_service(probe))

    assert check.ok is False  # type: ignore[attr-defined]
    assert "RuntimeError" in check.detail  # type: ignore[attr-defined]


def test_a_confirmed_executor_passes_the_check() -> None:
    async def probe() -> bool:
        return True

    check = _check(_service(probe))

    assert check.ok is True  # type: ignore[attr-defined]


def test_the_whole_report_is_not_ready_without_a_live_executor() -> None:
    """The check is required, so it must sink the report on its own."""

    async def probe() -> bool:
        return False

    report = asyncio.run(_service(probe).verify_live_readiness())

    assert report.ready is False


@pytest.mark.parametrize("available", [True, False])
def test_the_check_is_always_present(available: bool) -> None:
    """It must never be silently omitted — absence would restore the trap."""

    async def probe() -> bool:
        return available

    report = asyncio.run(_service(probe).verify_live_readiness())

    assert any(c.name == "live_executor" for c in report.checks)
