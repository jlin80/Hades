"""Volatility targeting: an extra reducing layer that cannot escape the caps.

The properties, ordered by what getting them wrong would cost:

1. **Off by default.** Enabling it changes every size the platform computes.
2. **The hard caps still bind.** The layer is applied before them, so nothing it
   does can lift a position above ``max_position_size_usd`` or above cash.
3. **Reduce-only by default.** The engine's standing rule is that doubt shrinks
   size and never amplifies it past the base.
4. **Unmeasured volatility means no adjustment.** A token with no price history
   reports 0.0, and a naive inverse would read that as zero risk and size to the
   ceiling — the one case where this layer could do real damage.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hades.contexts.common.domain.value_objects import TokenMint, TokenRef
from hades.contexts.risk.application.sizing import PositionSizingEngine
from hades.contexts.risk.domain.models import (
    RiskCandidate,
    SizingConfig,
    VolatilitySizingConfig,
)

_MINT = "So11111111111111111111111111111111111111112"


def _candidate(*, volatility_pct: float = 0.0) -> RiskCandidate:
    """A strong candidate, so sizing is driven by the layer under test."""
    return RiskCandidate(
        token=TokenRef(mint=TokenMint(address=_MINT), symbol="WSOL"),
        at=datetime(2026, 8, 4, tzinfo=UTC),
        prob_roi_positive=0.9,
        prob_hit_tp=0.8,
        prob_hit_sl=0.2,
        confidence=1.0,
        regime="bull",
        security_score=95.0,
        wallet_risk_score=5.0,
        liquidity_score=95.0,
        volatility_pct=volatility_pct,
    )


def _config(**vol: object) -> SizingConfig:
    return SizingConfig(
        risk_per_trade_pct=2.0,
        max_position_size_usd=100.0,
        min_position_size_usd=0.05,
        stop_loss_pct=20.0,
        volatility=VolatilitySizingConfig(**vol),  # type: ignore[arg-type]
    )


def _size(config: SizingConfig, candidate: RiskCandidate, *, available: float = 10_000.0) -> float:
    decision = PositionSizingEngine(config).size(
        candidate, equity_usd=10_000.0, available_usd=available
    )
    assert decision.approved, decision.detail
    assert decision.sizing is not None
    return float(decision.sizing.notional.amount)


# -- off by default ------------------------------------------------------------


def test_volatility_sizing_is_off_by_default() -> None:
    assert SizingConfig().volatility.enabled is False
    assert VolatilitySizingConfig().scale_for(80.0) == 1.0


def test_disabled_layer_changes_nothing() -> None:
    calm = _size(_config(), _candidate(volatility_pct=5.0))
    wild = _size(_config(), _candidate(volatility_pct=80.0))
    assert calm == wild


# -- the scale factor ----------------------------------------------------------


def test_high_volatility_reduces_the_scale() -> None:
    cfg = VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0, min_scale=0.1)
    # 15 target / 30 realised = 0.5
    assert cfg.scale_for(30.0) == 0.5


def test_the_scale_is_clamped_to_the_floor() -> None:
    cfg = VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0, min_scale=0.25)
    # 15 / 300 = 0.05, below the floor.
    assert cfg.scale_for(300.0) == 0.25


def test_reduce_only_by_default_a_calm_token_is_not_sized_up() -> None:
    cfg = VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0)
    assert cfg.max_scale == 1.0
    # 15 / 5 = 3.0 raw, clamped to 1.0.
    assert cfg.scale_for(5.0) == 1.0


def test_scaling_up_is_possible_but_bounded_when_explicitly_configured() -> None:
    cfg = VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0, max_scale=1.5)
    assert cfg.scale_for(5.0) == 1.5  # 3.0 raw, clamped to the configured ceiling


# -- unmeasured volatility is inert --------------------------------------------


def test_zero_volatility_is_unmeasured_not_calm() -> None:
    """A naive inverse would divide by zero or scale to the ceiling here."""
    cfg = VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0, max_scale=3.0)
    assert cfg.scale_for(0.0) == 1.0


def test_volatility_below_the_measurability_floor_is_inert() -> None:
    cfg = VolatilitySizingConfig(
        enabled=True, target_volatility_pct=15.0, max_scale=3.0, min_measurable_volatility_pct=1.0
    )
    assert cfg.scale_for(0.5) == 1.0


def test_a_negative_reading_is_inert_not_inverted() -> None:
    cfg = VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0, max_scale=3.0)
    assert cfg.scale_for(-10.0) == 1.0


# -- the hard caps still bind --------------------------------------------------


def test_the_per_trade_cap_binds_after_scaling() -> None:
    """Scaling happens before the caps, so the caps are never lifted."""
    cfg = _config(enabled=True, target_volatility_pct=15.0, max_scale=10.0)
    notional = _size(cfg, _candidate(volatility_pct=1.5))
    assert notional <= cfg.max_position_size_usd


def test_available_cash_binds_after_scaling() -> None:
    cfg = _config(enabled=True, target_volatility_pct=15.0, max_scale=10.0)
    notional = _size(cfg, _candidate(volatility_pct=1.5), available=7.0)
    assert notional <= 7.0


def test_scaling_up_can_never_exceed_the_cap_however_extreme_the_config() -> None:
    cfg = SizingConfig(
        risk_per_trade_pct=2.0,
        max_position_size_usd=50.0,
        min_position_size_usd=0.05,
        stop_loss_pct=20.0,
        volatility=VolatilitySizingConfig(
            enabled=True,
            target_volatility_pct=100.0,
            max_scale=1000.0,
            min_measurable_volatility_pct=0.1,
        ),
    )
    notional = _size(cfg, _candidate(volatility_pct=0.2))
    assert notional == 50.0


# -- it is a layer, not a replacement ------------------------------------------


def test_conviction_still_drives_the_base_size() -> None:
    # The per-trade cap is lifted here on purpose: with it in place both sizes
    # clamp to the ceiling and the test would pass or fail for the wrong reason.
    cfg = _config(enabled=True, target_volatility_pct=15.0).model_copy(
        update={"max_position_size_usd": 1_000_000.0}
    )
    strong = _size(cfg, _candidate(volatility_pct=15.0), available=1_000_000.0)
    weak_candidate = _candidate(volatility_pct=15.0).model_copy(
        update={"prob_roi_positive": 0.3, "confidence": 0.2, "security_score": 40.0}
    )
    weak = _size(cfg, weak_candidate, available=1_000_000.0)
    assert weak < strong, "the volatility layer must not flatten conviction"


def test_the_scale_is_recorded_in_the_audit_factors() -> None:
    cfg = _config(enabled=True, target_volatility_pct=15.0, min_scale=0.1)
    decision = PositionSizingEngine(cfg).size(
        _candidate(volatility_pct=30.0), equity_usd=10_000.0, available_usd=10_000.0
    )
    assert decision.factors["volatility_scale"] == 0.5
    assert decision.factors["volatility_pct"] == 30.0


def test_halving_the_scale_halves_the_size_when_no_cap_binds() -> None:
    """The arithmetic itself, isolated from the caps by a small equity."""
    base = SizingConfig(
        risk_per_trade_pct=2.0,
        max_position_size_usd=1_000_000.0,
        min_position_size_usd=0.05,
        stop_loss_pct=20.0,
    )
    scaled = base.model_copy(
        update={
            "volatility": VolatilitySizingConfig(enabled=True, target_volatility_pct=15.0),
        }
    )
    unscaled_notional = _size(base, _candidate(volatility_pct=30.0), available=1_000_000.0)
    scaled_notional = _size(scaled, _candidate(volatility_pct=30.0), available=1_000_000.0)
    assert abs(scaled_notional - unscaled_notional * 0.5) < 0.01
