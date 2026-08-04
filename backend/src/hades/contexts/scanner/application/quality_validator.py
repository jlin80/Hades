"""Quality Validator — corrupt data never enters the store.

A pure, side-effect-free service: given a datum it returns a
:class:`ValidationOutcome` listing every problem it found and whether the datum
is acceptable. Callers (the pipeline) decide what to do with the outcome — record
anomalies, emit events, drop the datum. Keeping it pure makes every rule
trivially unit-testable.

It checks the five families the spec calls out: negative values, duplicates
(via an injected "seen" predicate), extreme outliers, invalid timestamps, and
incomplete information.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from hades.contexts.scanner.application.metadata_collector import CollectedMetadata
from hades.contexts.scanner.domain.models import (
    AnomalyKind,
    QualityIssue,
    TokenMetadata,
    ValidationOutcome,
)
from hades.contexts.scanner.domain.ports import RawTokenCandidate

Now = Callable[[], datetime]

# SPL tokens use 0-18 decimals; anything else is malformed metadata.
_MAX_DECIMALS = 18
# A creation timestamp may drift slightly ahead of us via clock skew, but not far.
_FUTURE_SKEW = timedelta(minutes=5)


class QualityValidator:
    """Validates candidates, metadata and feature vectors before storage."""

    def __init__(
        self,
        *,
        now: Now = lambda: datetime.now(UTC),
        outlier_z: float = 8.0,
    ) -> None:
        self._now = now
        self._outlier_z = outlier_z

    def validate_candidate(
        self, candidate: RawTokenCandidate, *, is_duplicate: bool = False
    ) -> ValidationOutcome:
        issues: list[QualityIssue] = []
        if candidate.liquidity.amount < 0:
            issues.append(
                QualityIssue(
                    kind=AnomalyKind.NEGATIVE_VALUE,
                    field="liquidity",
                    detail=f"negative liquidity {candidate.liquidity.amount}",
                    fatal=True,
                )
            )
        if is_duplicate:
            issues.append(
                QualityIssue(
                    kind=AnomalyKind.DUPLICATE,
                    field="mint",
                    detail="mint already seen",
                    fatal=True,
                )
            )
        if candidate.created_at is not None:
            issues.extend(self._timestamp_issues("created_at", candidate.created_at))
        accepted = not any(i.fatal for i in issues)
        return ValidationOutcome(accepted=accepted, issues=tuple(issues))

    def validate_metadata(
        self, metadata: TokenMetadata, *, uncollected: frozenset[str] = frozenset()
    ) -> ValidationOutcome:
        """Validate merged metadata.

        ``uncollected`` names fields whose every provider failed this round. A
        ``None`` there is an absence of *evidence*, not evidence of absence, so
        it is not reported as incomplete data — otherwise one unreachable RPC
        endpoint manufactures an "incomplete" anomaly for every token it fails
        on, and the anomaly log describes our own outage as the tokens' defect.
        """
        issues: list[QualityIssue] = []
        if metadata.decimals is not None and not (0 <= metadata.decimals <= _MAX_DECIMALS):
            issues.append(
                QualityIssue(
                    kind=AnomalyKind.MALFORMED,
                    field="decimals",
                    detail=f"decimals out of range: {metadata.decimals}",
                    fatal=True,
                )
            )
        if metadata.total_supply is not None and metadata.total_supply < 0:
            issues.append(
                QualityIssue(
                    kind=AnomalyKind.NEGATIVE_VALUE,
                    field="total_supply",
                    detail="negative supply",
                    fatal=True,
                )
            )
        if metadata.created_at is not None:
            issues.extend(self._timestamp_issues("created_at", metadata.created_at))
        # Incompleteness is recorded but never fatal — we store what we have.
        for field_name, present in (
            ("name", bool(metadata.name)),
            ("symbol", bool(metadata.symbol)),
            ("decimals", metadata.decimals is not None),
        ):
            if not present and field_name not in uncollected:
                issues.append(
                    QualityIssue(
                        kind=AnomalyKind.INCOMPLETE,
                        field=field_name,
                        detail=f"missing {field_name}",
                        fatal=False,
                    )
                )
        accepted = not any(i.fatal for i in issues)
        return ValidationOutcome(accepted=accepted, issues=tuple(issues))

    def validate_collected(self, collected: CollectedMetadata) -> ValidationOutcome:
        return self.validate_metadata(collected.metadata, uncollected=collected.unavailable)

    def validate_features(self, values: dict[str, float]) -> ValidationOutcome:
        """Reject NaN/inf; flag extreme z-score outliers within the vector."""
        issues: list[QualityIssue] = []
        finite: dict[str, float] = {}
        for name, value in values.items():
            if not math.isfinite(value):
                issues.append(
                    QualityIssue(
                        kind=AnomalyKind.MALFORMED,
                        field=name,
                        detail=f"non-finite feature value: {value}",
                        fatal=True,
                    )
                )
            else:
                finite[name] = value
        issues.extend(self._outlier_issues(finite))
        accepted = not any(i.fatal for i in issues)
        return ValidationOutcome(accepted=accepted, issues=tuple(issues))

    # -- helpers --------------------------------------------------------------

    def _timestamp_issues(self, field_name: str, ts: datetime) -> list[QualityIssue]:
        if ts.tzinfo is None:
            return [
                QualityIssue(
                    kind=AnomalyKind.INVALID_TIMESTAMP,
                    field=field_name,
                    detail="naive timestamp (no timezone)",
                    fatal=True,
                )
            ]
        if ts > self._now() + _FUTURE_SKEW:
            return [
                QualityIssue(
                    kind=AnomalyKind.INVALID_TIMESTAMP,
                    field=field_name,
                    detail=f"timestamp in the future: {ts.isoformat()}",
                    fatal=True,
                )
            ]
        return []

    def _outlier_issues(self, values: dict[str, float]) -> list[QualityIssue]:
        if len(values) < 3:
            return []
        nums = list(values.values())
        mean = sum(nums) / len(nums)
        variance = sum((n - mean) ** 2 for n in nums) / len(nums)
        std = math.sqrt(variance)
        if std == 0.0:
            return []
        issues: list[QualityIssue] = []
        for name, value in values.items():
            z = abs(value - mean) / std
            if z > self._outlier_z:
                issues.append(
                    QualityIssue(
                        kind=AnomalyKind.OUTLIER,
                        field=name,
                        detail=f"z-score {z:.1f} exceeds {self._outlier_z}",
                        fatal=False,
                    )
                )
        return issues
