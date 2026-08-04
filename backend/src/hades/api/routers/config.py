"""Config endpoint — the platform's **non-secret** configuration posture.

Exposes tunables and toggles the dashboard displays. It deliberately never
returns secrets (passwords, webhook URLs, private keys, API keys) — only their
presence is indicated where useful.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from hades.api.dependencies import get_config_manager, get_container
from hades.api.security import Principal, get_principal
from hades.bootstrap import Container
from hades.ops.config_manager import ConfigManager

router = APIRouter(prefix="/api/v1", tags=["config"])


@router.get("/config", summary="Non-secret configuration posture")
async def config(container: Container = Depends(get_container)) -> dict[str, object]:
    s = container.settings
    return {
        "core": {
            "environment": s.env,
            "instance_id": s.instance_id,
            "timezone": s.timezone,
            "log_level": s.log_level,
            "log_format": s.log_format,
        },
        "trading": {
            "mode": s.trading_mode,
            "is_live": s.is_live,
            "live_gate_enabled": s.live_trading_enabled,
        },
        "risk": {
            "max_concurrent_positions": s.risk.max_concurrent_positions,
            "max_portfolio_exposure_pct": s.risk.max_portfolio_exposure_pct,
            "max_position_size_usd": s.risk.max_position_size_usd,
            "daily_max_loss_usd": s.risk.daily_max_loss_usd,
            "kill_switch_enabled": s.risk.kill_switch_enabled,
        },
        "scanner": {
            "enabled": s.scanner.enabled,
            "poll_interval_seconds": s.scanner.poll_interval_seconds,
            "sources": s.scanner.sources.split(","),
            "min_liquidity_usd": s.scanner.min_liquidity_usd,
        },
        # The thresholds that actually gate a token, from the engines that
        # enforce them. `ScoringSettings` carries near-identically named fields
        # (SCORING_MIN_SECURITY_SCORE and friends) that nothing reads — the
        # scoring context is not wired into any runtime — and this endpoint used
        # to publish those. Both default to 60, so the two agreed until an
        # operator changed one; then the screen would confirm a setting that had
        # no effect. Report what decides.
        "security": {
            "min_security_score": s.security.min_security_score,
            "doubt_buffer": s.security.doubt_buffer,
            "honeypot_enabled": s.security.honeypot_enabled,
        },
        "signal_gates": {
            "min_prob_roi_positive": s.risk.min_prob_roi_positive,
            "min_confidence": s.risk.min_confidence,
            "min_developer_score": s.risk.min_developer_score,
            "min_liquidity_usd": s.risk.min_liquidity_usd,
        },
        "infrastructure": {
            "event_bus_transport": s.event_bus.transport,
            "clickhouse_enabled": s.clickhouse.enabled,
            "metrics_enabled": s.observability.metrics_enabled,
            "backups_enabled": s.backup.enabled,
            "scheduler_enabled": s.scheduler.enabled,
            "watchdog_enabled": s.watchdog.enabled,
        },
        "notifications": {
            "discord_enabled": s.notification.discord_enabled,
            "discord_webhook_configured": bool(s.notification.discord_webhook_url),
            "min_severity": s.notification.min_severity,
        },
        "wallet": {"configured": s.wallet.is_configured},
    }


@router.get("/config/export", summary="Full non-secret configuration export")
async def config_export(manager: ConfigManager = Depends(get_config_manager)) -> dict[str, object]:
    """The complete non-secret posture (settings + runtime overrides). Secrets
    are redacted before serialization — this never returns a credential."""
    return await manager.export()


@router.get("/config/snapshots", summary="List config snapshot versions")
async def config_snapshots(
    manager: ConfigManager = Depends(get_config_manager),
) -> dict[str, object]:
    versions = await manager.list_versions()
    return {"count": len(versions), "versions": versions}


@router.get("/config/snapshots/{version}", summary="Get one config snapshot")
async def config_snapshot(
    version: int, manager: ConfigManager = Depends(get_config_manager)
) -> dict[str, object]:
    snapshot = await manager.get_version(version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"config snapshot {version} not found")
    return snapshot


@router.post("/config/snapshots", summary="Create a config snapshot version")
async def create_config_snapshot(
    note: str | None = Body(default=None, embed=True),
    manager: ConfigManager = Depends(get_config_manager),
    principal: Principal = Depends(get_principal),
) -> dict[str, object]:
    return await manager.snapshot(actor=principal.identity, note=note)


@router.get("/config/diff", summary="Diff two config snapshot versions")
async def config_diff(
    a: int = Query(...),
    b: int = Query(...),
    manager: ConfigManager = Depends(get_config_manager),
) -> dict[str, object]:
    try:
        return await manager.diff_versions(a, b)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/config/drift", summary="Detect drift from the latest snapshot")
async def config_drift(
    manager: ConfigManager = Depends(get_config_manager),
) -> dict[str, object]:
    return await manager.detect_drift()


@router.post("/config/import", summary="Import a config snapshot as a new version")
async def config_import(
    payload: dict[str, Any] = Body(...),
    manager: ConfigManager = Depends(get_config_manager),
    principal: Principal = Depends(get_principal),
) -> dict[str, object]:
    """Adopt a snapshot as a new recorded version and apply allow-listed runtime
    overrides. Never mutates env or a guarded switch; reserved keys are refused."""
    try:
        return await manager.import_config(payload, actor=principal.identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
