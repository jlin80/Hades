"""Derive the ``history.*`` context features from a candidate's enrichment.

The same treatment ``security.*`` and ``intel.*`` already get: upstream evidence
that does not come from the Feature Engine is turned into pre-normalised [0, 1]
values and injected into the vector, so it travels with the prediction, appears
in explanations, and is available to any model the Training Engine later fits on
these vectors.

Two deliberate restrictions:

* **Nothing is emitted for an uninformative prior.** An absent key is honest
  ("we have no history on this developer"); a key at 0.5 would be a fabricated
  measurement that a future training run would happily learn from.
* **No default specialist reads these keys.** Injecting a feature is not the same
  as wiring it into a model: the twelve specialists' weight sets are unchanged,
  so this addition cannot move today's probabilities through a side door. The
  historical prior reaches the fusion through exactly one documented path — the
  meta-model's bounded prior — and everything here is evidence for the record
  and material for future training.
"""

from __future__ import annotations

from hades.contexts.learning.application.mathx import clamp
from hades.contexts.learning.domain.models import CandidateEnrichment, EvidenceBasis

#: Realised ROI at which the average-return feature saturates in either
#: direction (±100%): meme-coin outcomes have no upper bound worth encoding.
_ROI_SATURATION = 1.0


def history_feature_values(enrichment: CandidateEnrichment | None) -> dict[str, float]:
    """Compute the injected ``history.*`` features for one enriched candidate."""
    if enrichment is None or not enrichment.evidence_available:
        return {}

    values: dict[str, float] = {
        # The fused prior, mapped from its bounded logit range into [0, 1] with
        # 0.5 as "history says nothing either way".
        "history.prior": clamp(0.5 + enrichment.prior_log_odds / 4.0),
        "history.support": clamp(enrichment.sample_support),
        "history.dimensions": clamp(len(enrichment.informative_dimensions) / 11.0),
    }
    for prior in enrichment.priors:
        if not prior.is_informative:
            continue
        name = prior.dimension.value
        values[f"history.{name}.rate"] = clamp(prior.positive_rate)
        values[f"history.{name}.strength"] = clamp(prior.strength)
        if prior.basis is EvidenceBasis.OUTCOMES:
            values[f"history.{name}.roi"] = clamp(0.5 + prior.avg_roi / (2.0 * _ROI_SATURATION))
    return values


__all__ = ["history_feature_values"]
