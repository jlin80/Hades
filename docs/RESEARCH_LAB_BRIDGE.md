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

- `tests/test_research_candidate_contract.py` parses fixtures committed under
  `tests/fixtures/research_lab/`. ⚠️ For the **candidate** bundle those fixtures are
  hand-written to the contract — no lab-side candidate exporter exists — so they
  pin Core's reader against itself, not against a real writer. The **knowledge**
  bundle's fixture *is* generated by the lab's exporter and committed in both
  repositories, which is the guard actually working (see below).
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

## The knowledge bridge (Phase 2, 2026-07-28) — the direction that now works

There are **two** bridges, and they carry different things. This section is the one
that is live end-to-end.

| | Candidate bridge (`hades.candidate/v1`) | **Knowledge bridge (`hades.knowledge/v1`)** |
|---|---|---|
| Carries | a trained model the Core may promote | **findings**: backtests, walk-forward, Monte Carlo, replays, experiments |
| Lands in | the model registry, as a TRAINED candidate | **permanent memory**, as observations |
| Lab-side writer | none (see the section below) | **`hades_research.knowledge_export`** ✅ |
| Status | still one-sided | **working; fixture-tested in both repos** |

### What crosses, and what the lab is not trusted to say

The lab writes a checksummed JSON bundle into a directory **it owns**; an operator or a
volume mount places it in the Core's inbox; `POST /api/v1/knowledge/import` sweeps it. The
two repositories share no library, no schema and no network call, and with nobody sweeping,
a bundle on disk does nothing.

Three structural limits bound what a file can do, and they matter because Knowledge feeds the
AI Committee's training ledger:

1. **A bundle cannot declare its verification.** The field does not exist in the format —
   declaring it is a rejection, not an ignored key. The Core derives the level from the
   record's `source`, and every source an external producer may claim maps to `simulated`.
2. **A bundle cannot claim a platform source.** `paper_trading`, `executed_trade`, `scanner`,
   `security`, `committee` are refused by an allowlist, so a file cannot pose as the platform
   observing itself.
3. **A bundle cannot express a *lesson*.** Lessons are the only thing the committee trains
   on, and they are minted exclusively by the Decision Journal settling a trade the platform
   actually took.

Together: the worst a hostile or buggy bundle achieves is inserting clearly-labelled
simulated observations. It cannot inject a simulation into the ledger the brain learns from.

> Why this matters concretely: `Verification.REALISED` means a trade that opened, ran and
> paid out. If a backtest could stamp itself realised, the committee would be trained on the
> lab's own assumptions, and nothing downstream would notice — the rows would look exactly
> like real ones.

### Keeping the two sides honest

`tests/fixtures/research_lab/knowledge_bundle_v1.json` is **generated by the lab's actual
exporter** and committed byte-identically in both repositories. Each side's suite parses it.
Drift on either side fails a build. (Contrast the candidate bridge below, whose fixtures were
hand-written while its docs claimed otherwise — that is precisely why this one is generated.)

### Running an import

```bash
curl -X POST http://127.0.0.1:8000/api/v1/knowledge/import
```

Processed files are moved to `accepted/` or `rejected/` under the inbox, timestamped, with
the rejection reason logged. Rejections are **kept, not deleted** — the file that failed is
usually the one you need to look at.

## ⚠️ The *candidate* bridge has never carried a single bundle (2026-07-28)

This section is about the **model** bridge (`hades.candidate/v1`), not the knowledge
bridge above. The contract Core implements is real and tested, but **no candidate
bundle has ever crossed it, and the lab as built today cannot produce one.** The gap
is not a missing exporter; it is three independent incompatibilities:

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

The `meta_model.json` / `specialist_liquidity.json` fixtures are **hand-written to
the contract**, not generated by a lab exporter, because no *candidate* exporter
exists. Treat that guard as untested against reality until one ships. (The
knowledge bundle's fixture, by contrast, is generated — see above.)

Closing the bridge requires a product decision recorded in
[`ARCHITECTURE_AUDIT_2026-07-28.md`](ARCHITECTURE_AUDIT_2026-07-28.md) §7.4. The
recommendation there is **O1**: the lab restricts its *deployable* output to logistic
weight sets over Core's `FeatureCatalog`, and its tree models stay upstream as
hypothesis generators (feature ranking, interaction discovery) rather than as
deployable artifacts. That is the only option preserving Core's explainability rule.
