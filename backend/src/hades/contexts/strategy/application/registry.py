"""StrategyRegistry - the hot-swappable catalogue of loaded strategies.

Holds every strategy instance keyed by name and tracks its operational status and
current dynamic weight. Adding a strategy is a registration, not an architecture
change. The registry also owns the *failsafe*: when a strategy raises, it is muted
(status -> ``ERROR``) after a small run of failures - never removed - so one broken
plugin can never stop the engine. The registry is pure/synchronous state; the
engine performs the async event emission around it.
"""

from __future__ import annotations

from collections.abc import Iterable

from hades.contexts.strategy.application.base import BaseStrategy
from hades.contexts.strategy.domain.models import StrategyStatus, StrategyWeight


class StrategyRegistry:
    """In-memory catalogue of strategies plus their status, weight and errors."""

    def __init__(self, *, error_threshold: int = 3) -> None:
        self._strategies: dict[str, BaseStrategy] = {}
        self._weights: dict[str, StrategyWeight] = {}
        self._errors: dict[str, int] = {}
        self._error_threshold = max(1, error_threshold)

    # -- registration ---------------------------------------------------------

    def register(self, strategy: BaseStrategy) -> None:
        self._strategies[strategy.name] = strategy
        self._errors.setdefault(strategy.name, 0)
        self._weights.setdefault(strategy.name, StrategyWeight(strategy=strategy.name, weight=1.0))

    def register_many(self, strategies: Iterable[BaseStrategy]) -> None:
        for strategy in strategies:
            self.register(strategy)

    def remove(self, name: str) -> None:
        self._strategies.pop(name, None)
        self._weights.pop(name, None)
        self._errors.pop(name, None)

    # -- lookup ---------------------------------------------------------------

    def get(self, name: str) -> BaseStrategy | None:
        return self._strategies.get(name)

    def all(self) -> tuple[BaseStrategy, ...]:
        """Every registered strategy, ordered by priority then name (stable)."""
        return tuple(
            sorted(
                self._strategies.values(),
                key=lambda s: (s.metadata.priority, s.name),
            )
        )

    def active(self) -> tuple[BaseStrategy, ...]:
        """Strategies eligible to evaluate a token (ENABLED, not muted)."""
        return tuple(s for s in self.all() if s.metadata.status is StrategyStatus.ENABLED)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.all())

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, name: object) -> bool:
        return name in self._strategies

    # -- status control -------------------------------------------------------

    def set_status(self, name: str, status: StrategyStatus) -> None:
        strategy = self._strategies.get(name)
        if strategy is None:
            return
        strategy.override_metadata(status=status)
        if status is StrategyStatus.ENABLED:
            self._errors[name] = 0

    def enable(self, name: str) -> None:
        self.set_status(name, StrategyStatus.ENABLED)

    def disable(self, name: str) -> None:
        self.set_status(name, StrategyStatus.DISABLED)

    def record_error(self, name: str) -> bool:
        """Count a failure; mute the strategy past the threshold. Returns muted?."""
        if name not in self._strategies:
            return False
        self._errors[name] = self._errors.get(name, 0) + 1
        if self._errors[name] >= self._error_threshold:
            self.set_status(name, StrategyStatus.ERROR)
            return True
        return False

    def error_count(self, name: str) -> int:
        return self._errors.get(name, 0)

    # -- weights --------------------------------------------------------------

    def weight_of(self, name: str) -> StrategyWeight:
        return self._weights.get(name, StrategyWeight(strategy=name, weight=1.0))

    def set_weight(self, weight: StrategyWeight) -> StrategyWeight:
        previous = self._weights.get(weight.strategy)
        self._weights[weight.strategy] = weight
        return previous or StrategyWeight(strategy=weight.strategy, weight=1.0)

    def weights(self) -> dict[str, StrategyWeight]:
        return dict(self._weights)
