# The Research Lab bridge

How the **external** Hades Research Lab (its own repository, its own stack) gets a
finding into Hades Core — and why it is built the way it is.

> Not to be confused with `contexts/research`, the Phase 9 *internal* research
> context that runs inside this process. That one is structurally isolated from
> execution and verified by an AST test. This document is about the **separate
> lab repository**, which is not part of this process at all.

## The shape of it

```
┌──────────────────────┐   candidate.json    ┌──────────────┐   human   ┌──────────┐
│  Hades Research Lab  │ ──────────────────▶ │  Core inbox  │ ────────▶ │ Registry │
│  (separate repo)     │   operator carries  │  (a folder)  │  import   │ TRAINED  │
└──────────────────────┘                     └──────────────┘           └──────────┘
                                                                              │
                                                            existing human-gated ladder
                                                                              ▼
                                                              validate → shadow → PROMOTED
```

The entire interface is **one JSON file in a directory**. There is no shared
library, no shared database, no HTTP call, and neither repository imports the
other. The lab cannot reach into Core, and Core has no runtime dependency on the
lab — if the lab disappears tomorrow, nothing here changes.

## What crosses

A **candidate bundle**: `hades.candidate/v1`, a declarative description of a
transparent logistic model.

```json
{
  "bundle_format": "hades.candidate/v1",
  "kind": "committee_model",
  "candidate_id": "specialist-liquidity-a1b2c3",
  "produced_by": "hades-research-lab",
  "produced_at": "2026-07-25T00:00:00+00:00",
  "name": "liquidity",
  "model_kind": "logistic",
  "dataset_id": "ds-2026-07",
  "feature_names": ["liquidity_usd", "lp_locked_pct"],
  "weights": { "bias": -0.42, "coefficients": { "liquidity_usd": 1.21, "lp_locked_pct": 0.68 } },
  "heads": {},
  "metrics": { "samples": 1200, "auc": 0.72, "brier": 0.18 },
  "trained_on_samples": 1200,
  "notes": "walk-forward validated, 5 folds",
  "evidence": { "walkforward_folds": 5 },
  "checksum": "…sha256…"
}
```

`name` must be one of the twelve committee members or `meta`; a `meta` bundle must
carry exactly the three fusion heads (`roi_positive`, `hit_tp`, `hit_sl`), and no
other bundle may carry heads at all.

### Why weights and not a pickled model

Three independent reasons, each sufficient on its own:

1. **Explainability is a golden rule.** The registry stores models as transparent
   weight sets — `sigmoid(bias + Σ wᵢ·xᵢ)` over named features. A bundle is
   diffable and reviewable *before* import. An opaque blob would not be.
2. **Unpickling is code execution.** Core must never execute an artifact produced
   in another repository. JSON cannot execute.
3. **The Core image is pure Python at runtime** — no numpy, no sklearn. Numbers
   need nothing new; a fitted estimator would drag in the whole ML stack.

A model that is not expressible as a weight set therefore **cannot** be exported.
That is the contract working, not an obstacle to route around: it stays in the lab
as a research finding until someone distils it into something Core can explain.

## What the import does — and does not do

`CandidateImportService` sweeps the inbox and, for each bundle:

- validates it against the contract, **fail-closed** — the first thing it cannot
  vouch for is a rejection with a reason, never a best-effort parse;
- registers the survivor as a `TRAINED` `ModelCard` with a fresh version, through
  the same `ModelRegistryService` a local training run uses, so it emits the same
  `ModelTrained` event and lands in the same append-only history;
- moves the file to `accepted/` or `rejected/` — **nothing is ever deleted**, so
  the import history is reconstructable from the filesystem alone.

It does **not** validate the model, run it in shadow, or promote it. The status is
hardcoded to `TRAINED` rather than read from the bundle: a candidate's own opinion
of its readiness is evidence, never authority. Promotion remains exactly what it
was — `POST /api/v1/committee/models/{model_id}/promote`, a deliberate human act.

Provenance is written into the card's `notes` (`research-lab-candidate:<id>`), so a
reviewer can always tell an imported model from a locally-trained one.

### Rejection is per-file

One malformed bundle is rejected and set aside; it never aborts the sweep and
never half-registers. A bad file cannot block the good ones.

## Running an import

Configure the inbox (defaults to `/app/research/inbox`):

```bash
RESEARCH_CANDIDATE_INBOX=/app/research/inbox
```

Drop the bundles in, then:

```bash
curl -X POST http://localhost:8000/api/v1/committee/candidates/import
```

The response lists what was accepted (with the new `model_id`/`version`) and what
was rejected (with the reason). `"promoted": false` is always in the payload — the
endpoint registers candidates and nothing more.

Review a candidate before promoting it: `GET /api/v1/committee/models` shows the
registry, and the weights are plain JSON you can read.

## Keeping the two sides honest

Because the repositories never import each other, contract drift is the real risk.
Two things guard it:

- `tests/test_research_candidate_contract.py` parses **fixtures generated by the
  lab's actual exporter** (committed under `tests/fixtures/research_lab/`). If the
  lab's writer and Core's reader diverge, this fails.
- the `checksum` is computed identically on both sides (canonical JSON: sorted
  keys, compact separators, UTF-8, `checksum` excluded from its own input), so a
  bundle edited or truncated in transit cannot be imported.

If a fixture test fails, reconcile the contract on **both** sides and regenerate
the fixtures. Never loosen the parser to make a malformed bundle pass.

## Bumping the format

`BUNDLE_FORMAT` is bumped in both repositories in the same change, never
independently. An unrecognised format is a loud rejection by design — silent
partial understanding of a newer bundle would be the dangerous outcome.

## Not yet accepted

Only `kind: "committee_model"` is wired. Strategy-parameter and feature candidates
are deliberately rejected: they have no human-gated destination in Core today, and
accepting what we cannot gate would be worse than rejecting it.

## ⚠️ The bridge has never carried a single bundle (2026-07-28)

This document describes the contract Core *implements and enforces*. It is real and
tested. What it does not say — and must — is that **no bundle has ever crossed it,
and the lab as built today cannot produce one.** The gap is not a missing exporter;
it is three independent incompatibilities:

| | Core accepts | `HadesResearchLab` produces |
|---|---|---|
| **Format** | JSON, `hades.candidate/v1`, checksummed | a directory of `model.pkl` + `preprocessor.pkl` + `manifest.json` (`ml/packaging.py`) |
| **Model family** | `model_kind: "logistic"` only — `sigmoid(bias + Σwᵢxᵢ)` | XGBoost, LightGBM, CatBoost, RandomForest, GradientBoosting, LogisticRegression (`ml/models.py`) |
| **Feature space** | Core's `FeatureCatalog` (`basic.*`, `tech.*`, `holders.*`, `pool.*`, `regime.*`, `time.*`) | the lab's own `feature_store`, fed by the lab's own collector into the lab's own Postgres |

Core's refusal of pickles is correct and must not be relaxed (*"unpickling is code
execution"*). The consequence is that **the lab's tree models can never cross this
bridge in any form** without abandoning Core's explainability rule. Only its
logistic models are expressible at all — and even those index features Core does not
produce.

The `tests/fixtures/research_lab/` fixtures referenced above are **hand-written to
the contract**, not generated by a lab exporter, because no lab exporter exists.
The claim in "Keeping the two sides honest" that they come from *"the lab's actual
exporter"* is aspirational; treat that guard as untested against reality until an
exporter ships.

Closing the bridge requires a product decision recorded in
[`ARCHITECTURE_AUDIT_2026-07-28.md`](ARCHITECTURE_AUDIT_2026-07-28.md) §7.4. The
recommendation there is **O1**: the lab restricts its *deployable* output to logistic
weight sets over Core's `FeatureCatalog`, and its tree models stay upstream as
hypothesis generators (feature ranking, interaction discovery) rather than as
deployable artifacts. That is the only option preserving Core's explainability rule.
