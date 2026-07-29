"""The Feature Store contract — normalise, version and document every feature.

The spec is emphatic: models are **never** fed raw PostgreSQL values. They read
from a Feature Store where every variable is normalised, versioned, documented
and auditable. This module is that contract for the AI Committee:

* :class:`FeatureCatalog` holds the curated :class:`FeatureSpec` set — each with a
  name, description, version, unit, origin, and the members that use it.
* :class:`FeatureNormalizer` turns a raw :class:`FeatureSet` (from the Feature
  Engine) into a model-ready :class:`NormalizedVector`, applying each feature's
  documented normalisation and recording coverage (missing inputs become a
  neutral 0 and lower confidence, never an error).

The normalisation parameters are sensible, documented priors for Solana meme
coins; the Training Engine may later refit them, producing a new catalog
version. Nothing here decides anything — it only shapes inputs.
"""

from __future__ import annotations

import math

from hades.contexts.features.domain.models import FeatureSet
from hades.contexts.learning.application.mathx import clamp
from hades.contexts.learning.domain.models import (
    CommitteeMember,
    FeatureSpec,
    NormalizationMethod,
    NormalizedVector,
)

_M = CommitteeMember


def _spec(
    name: str,
    description: str,
    *,
    unit: str,
    method: NormalizationMethod,
    used_by: tuple[CommitteeMember, ...],
    center: float = 0.0,
    scale: float = 1.0,
    lo: float = 0.0,
    hi: float = 1.0,
    origin: str = "feature_engine",
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        description=description,
        unit=unit,
        method=method,
        used_by=tuple(m.value for m in used_by),
        center=center,
        scale=scale,
        lo=lo,
        hi=hi,
        origin=origin,
    )


# The curated Feature Store schema (v1). Keys match the Feature Engine's
# ``{namespace}.{key}`` output exactly. Documented priors, not fitted values.
_CATALOG_V1: tuple[FeatureSpec, ...] = (
    # --- liquidity / pool ---------------------------------------------------
    _spec(
        "basic.liquidity",
        "USD liquidity available in the token's main pool.",
        unit="usd",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.LIQUIDITY, _M.RISK, _M.MARKET_REGIME),
        lo=math.log1p(1_000.0),
        hi=math.log1p(500_000.0),
    ),
    _spec(
        "pool.depth_usd",
        "Executable depth near mid price.",
        unit="usd",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.LIQUIDITY, _M.MICROSTRUCTURE),
        lo=math.log1p(500.0),
        hi=math.log1p(200_000.0),
    ),
    _spec(
        "basic.spread_bps",
        "Bid/ask spread in basis points (lower is healthier).",
        unit="bps",
        method=NormalizationMethod.CLIP,
        used_by=(_M.LIQUIDITY, _M.MICROSTRUCTURE),
        lo=0.0,
        hi=400.0,
    ),
    _spec(
        "basic.slippage_bps",
        "Estimated slippage for a reference size, in bps.",
        unit="bps",
        method=NormalizationMethod.CLIP,
        used_by=(_M.LIQUIDITY, _M.MICROSTRUCTURE),
        lo=0.0,
        hi=800.0,
    ),
    _spec(
        "pool.liquidity_change_pct",
        "Recent change in pool liquidity (%).",
        unit="pct",
        method=NormalizationMethod.ZSCORE,
        used_by=(_M.LIQUIDITY, _M.RISK),
        center=0.0,
        scale=25.0,
    ),
    _spec(
        "pool.lp_locked",
        "Whether LP tokens are locked (0/1).",
        unit="bool",
        method=NormalizationMethod.BOOL,
        used_by=(_M.RISK, _M.SECURITY),
    ),
    _spec(
        "basic.volume_to_liquidity",
        "Volume / liquidity — turnover intensity.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.LIQUIDITY, _M.MICROSTRUCTURE, _M.MOMENTUM),
        lo=0.0,
        hi=10.0,
    ),
    # --- momentum / technical ----------------------------------------------
    _spec(
        "tech.rsi_14",
        "14-period RSI (0-100).",
        unit="index",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.MOMENTUM, _M.TIMING),
        lo=0.0,
        hi=100.0,
    ),
    _spec(
        "tech.macd",
        "MACD line (trend momentum).",
        unit="price",
        method=NormalizationMethod.ZSCORE,
        used_by=(_M.MOMENTUM,),
        center=0.0,
        scale=1.0,
    ),
    _spec(
        "tech.price_slope",
        "Slope of recent price series.",
        unit="price/step",
        method=NormalizationMethod.ZSCORE,
        used_by=(_M.MOMENTUM, _M.MARKET_REGIME),
        center=0.0,
        scale=1.0,
    ),
    _spec(
        "tech.volume_slope",
        "Slope of recent volume series.",
        unit="usd/step",
        method=NormalizationMethod.ZSCORE,
        used_by=(_M.MOMENTUM, _M.MICROSTRUCTURE),
        center=0.0,
        scale=1.0,
    ),
    _spec(
        "tech.trend_alignment",
        "Agreement of micro/macro trend (-1..1).",
        unit="ratio",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.MOMENTUM, _M.MARKET_REGIME),
        lo=-1.0,
        hi=1.0,
    ),
    _spec(
        "basic.buyer_seller_ratio",
        "Buyers / sellers over the window.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.MOMENTUM, _M.BEHAVIOR),
        lo=0.0,
        hi=4.0,
    ),
    _spec(
        "basic.buy_sell_volume_ratio",
        "Buy volume / sell volume.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.MOMENTUM, _M.MICROSTRUCTURE),
        lo=0.0,
        hi=4.0,
    ),
    # --- volatility ---------------------------------------------------------
    _spec(
        "tech.volatility",
        "Realised volatility of recent returns.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.VOLATILITY, _M.RISK, _M.MARKET_REGIME),
        lo=0.0,
        hi=0.5,
    ),
    _spec(
        "tech.atr_14",
        "14-period Average True Range.",
        unit="price",
        method=NormalizationMethod.ZSCORE,
        used_by=(_M.VOLATILITY,),
        center=0.0,
        scale=1.0,
    ),
    _spec(
        "tech.volatility_compression",
        "Volatility compression (coil) signal.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.VOLATILITY, _M.TIMING),
        lo=0.0,
        hi=1.0,
    ),
    _spec(
        "tech.volatility_explosion",
        "Volatility expansion (breakout) signal.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.VOLATILITY, _M.MOMENTUM),
        lo=0.0,
        hi=1.0,
    ),
    # --- holders / distribution --------------------------------------------
    _spec(
        "holders.count",
        "Number of holders.",
        unit="count",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.HOLDER_DISTRIBUTION, _M.LIQUIDITY),
        lo=math.log1p(10.0),
        hi=math.log1p(5_000.0),
    ),
    _spec(
        "holders.top10_share_pct",
        "Supply held by the top-10 holders (%). High = concentrated.",
        unit="pct",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.HOLDER_DISTRIBUTION, _M.RISK),
        lo=0.0,
        hi=100.0,
    ),
    _spec(
        "holders.gini",
        "Gini of holder distribution (0 even, 1 concentrated).",
        unit="index",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.HOLDER_DISTRIBUTION, _M.RISK),
        lo=0.0,
        hi=1.0,
    ),
    _spec(
        "holders.holders_growth_pct",
        "Holder growth over the window (%).",
        unit="pct",
        method=NormalizationMethod.ZSCORE,
        used_by=(_M.HOLDER_DISTRIBUTION, _M.MOMENTUM),
        center=0.0,
        scale=20.0,
    ),
    _spec(
        "holders.recurring_ratio",
        "Share of holders that are recurring wallets.",
        unit="ratio",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.HOLDER_DISTRIBUTION, _M.BEHAVIOR),
        lo=0.0,
        hi=1.0,
    ),
    # --- timing / temporal --------------------------------------------------
    _spec(
        "basic.age_minutes",
        "Token age in minutes.",
        unit="min",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.TIMING, _M.RISK),
        lo=math.log1p(1.0),
        hi=math.log1p(1_440.0),
    ),
    _spec(
        "time.utc_hour",
        "UTC hour of day (0-23).",
        unit="hour",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.TIMING,),
        lo=0.0,
        hi=23.0,
    ),
    _spec(
        "basic.trades_5m",
        "Trade count in the last 5 minutes.",
        unit="count",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.TIMING, _M.MICROSTRUCTURE, _M.MOMENTUM),
        lo=0.0,
        hi=math.log1p(500.0),
    ),
    _spec(
        "basic.trades_1h",
        "Trade count in the last hour.",
        unit="count",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.TIMING, _M.MICROSTRUCTURE),
        lo=0.0,
        hi=math.log1p(5_000.0),
    ),
    # --- market cap / regime ------------------------------------------------
    _spec(
        "basic.market_cap",
        "Token market capitalisation (USD).",
        unit="usd",
        method=NormalizationMethod.LOG_MINMAX,
        used_by=(_M.MARKET_REGIME, _M.RISK),
        lo=math.log1p(5_000.0),
        hi=math.log1p(10_000_000.0),
    ),
    _spec(
        "regime.macro_trend",
        "Macro-trend direction (-1..1).",
        unit="ratio",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.MARKET_REGIME, _M.MOMENTUM),
        lo=-1.0,
        hi=1.0,
    ),
    _spec(
        "regime.micro_trend",
        "Micro-trend direction (-1..1).",
        unit="ratio",
        method=NormalizationMethod.MINMAX,
        used_by=(_M.MARKET_REGIME, _M.TIMING),
        lo=-1.0,
        hi=1.0,
    ),
    _spec(
        "basic.liquidity_to_mcap",
        "Liquidity / market-cap — how backed the cap is.",
        unit="ratio",
        method=NormalizationMethod.CLIP,
        used_by=(_M.LIQUIDITY, _M.RISK, _M.MARKET_REGIME),
        lo=0.0,
        hi=1.0,
    ),
)


class FeatureCatalog:
    """The versioned, documented registry of every feature the committee uses."""

    def __init__(self, specs: tuple[FeatureSpec, ...] = _CATALOG_V1, *, version: int = 1) -> None:
        self._specs = {s.name: s for s in specs}
        self.version = version

    def all(self) -> tuple[FeatureSpec, ...]:
        return tuple(self._specs.values())

    def get(self, name: str) -> FeatureSpec | None:
        return self._specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs.keys())

    def used_by(self, member: CommitteeMember) -> tuple[str, ...]:
        return tuple(n for n, s in self._specs.items() if member.value in s.used_by)


class FeatureNormalizer:
    """Turns a raw :class:`FeatureSet` into a model-ready :class:`NormalizedVector`."""

    def __init__(self, catalog: FeatureCatalog) -> None:
        self._catalog = catalog

    def normalize_values(self, raw_values: dict[str, float]) -> dict[str, float]:
        """Normalise a bare ``{name: value}`` map through the same catalog.

        The Candidate Enricher needs this: a lesson stores the *raw* vector that
        was frozen at decision time, and comparing it to a live candidate has to
        happen in the same normalised space the models see. Sharing this method
        with :meth:`normalize` is what guarantees "the same space" stays true
        when a spec changes.
        """
        out: dict[str, float] = {}
        for spec in self._catalog.all():
            if spec.name not in raw_values:
                continue
            out[spec.name] = self._apply(spec, raw_values[spec.name])
        return out

    def normalize(self, features: FeatureSet) -> NormalizedVector:
        values: dict[str, float] = {}
        raw: dict[str, float] = {}
        present: list[str] = []
        for spec in self._catalog.all():
            if spec.name not in features.values:
                continue
            raw_value = features.values[spec.name]
            raw[spec.name] = raw_value
            values[spec.name] = self._apply(spec, raw_value)
            present.append(spec.name)
        expected = max(1, len(self._catalog.names()))
        coverage = clamp(len(present) / expected)
        return NormalizedVector(
            token=features.token,
            at=features.computed_at,
            schema_version=self._catalog.version,
            values=values,
            raw=raw,
            coverage=coverage,
            present=tuple(present),
        )

    @staticmethod
    def _apply(spec: FeatureSpec, x: float) -> float:
        method = spec.method
        if method is NormalizationMethod.IDENTITY:
            return x
        if method is NormalizationMethod.BOOL:
            return 1.0 if x >= 0.5 else 0.0
        if method is NormalizationMethod.ZSCORE:
            scale = spec.scale if abs(spec.scale) > 1e-9 else 1.0
            return clamp((x - spec.center) / scale, -spec.clip_sigma, spec.clip_sigma)
        if method is NormalizationMethod.MINMAX:
            span = spec.hi - spec.lo
            return clamp((x - spec.lo) / span) if abs(span) > 1e-9 else 0.5
        if method is NormalizationMethod.CLIP:
            span = spec.hi - spec.lo
            return clamp((x - spec.lo) / span) if abs(span) > 1e-9 else 0.5
        if method is NormalizationMethod.LOG_MINMAX:
            lx = math.log1p(max(0.0, x))
            span = spec.hi - spec.lo
            return clamp((lx - spec.lo) / span) if abs(span) > 1e-9 else 0.5
        return x
