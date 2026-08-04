"""Retry Engine — bounded exponential backoff for transient execution failures.

Never retries infinitely: attempts are capped, the delay grows exponentially
(with jitter to avoid thundering-herd retries against an RPC), and only errors
flagged retryable are retried. On exhaustion the caller cancels the order. The
engine is transport-agnostic — the live executor uses it to re-send/re-confirm a
transaction and to rotate RPC on failure.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.retry")


class RetryableError(Exception):
    """Raise to signal a failure the :class:`RetryEngine` may retry."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    backoff_multiplier: float = 2.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Delay *before* ``attempt`` (1-indexed): attempt 1 has no delay."""
        if attempt <= 1:
            return 0.0
        raw = self.base_delay_seconds * (self.backoff_multiplier ** (attempt - 2))
        capped = min(raw, self.max_delay_seconds)
        if self.jitter:
            capped *= 0.5 + random.random() * 0.5  # jitter, not crypto
        return capped


class RetryEngine:
    """Runs an async operation under a :class:`RetryPolicy`."""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self._policy = policy or RetryPolicy()

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    async def run(
        self,
        operation: Callable[[int], Awaitable[object]],
        *,
        on_retry: Callable[[int, Exception], Awaitable[None]] | None = None,
    ) -> object:
        """Invoke ``operation(attempt)`` until it succeeds or attempts run out.

        ``operation`` receives the 1-indexed attempt number. Only
        :class:`RetryableError` is retried; any other exception propagates
        immediately (a non-transient failure must not be masked by retries).
        """
        last: Exception | None = None
        for attempt in range(1, self._policy.max_attempts + 1):
            delay = self._policy.delay_for(attempt)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await operation(attempt)
            except RetryableError as exc:
                last = exc
                _logger.warning(
                    "execution_retry",
                    attempt=attempt,
                    max_attempts=self._policy.max_attempts,
                    error=str(exc),
                )
                if on_retry is not None:
                    await on_retry(attempt, exc)
                continue
        raise RetryableError(f"operation failed after {self._policy.max_attempts} attempts: {last}")
