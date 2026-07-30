# Hades — Production-Readiness & Project-Closing Report

**Date:** 2026-07-23 · **Version:** `0.10.0` · **Phase:** 11 — Production Hardening, Stage 2
**Prepared as:** the final closing report of the Hades build, written to the bar of a
professional quantitative fund evaluating the platform before entrusting it with capital.

> **One-line verdict.** Hades is an architecturally excellent, safety-first, fully
> explainable trading platform that is **production-ready for continuous PAPER operation
> today**, and is **correctly, structurally incapable of trading real funds** until a
> short, well-understood set of durability and authentication items is closed and validated
> against a live stack. Nothing was inflated in this report; every claim is traceable to
> source or a reproduced command.

Companion documents: the living reference [`../hades.md`](../hades.md), the architecture maps
[`architecture.md`](architecture.md), and the adversarial audit [`TECHNICAL_AUDIT.md`](TECHNICAL_AUDIT.md).

---

## 1. General status

| | |
|---|---|
| Overall posture | **READY (paper) · NOT-LIVE by construction** |
| Backend health | **733 tests passing**, `mypy --strict` clean (456 files), `ruff` clean (0 findings), suite runs **warnings-as-errors** |
| Trading mode | Paper only; live hard-gated (env gate × 2 + readiness checklist + explicit confirm + authenticated operator) and unbuildable (no live adapters) |
| Deployment | Turnkey: `git clone → configure .env → docker compose up -d` (schema auto-migrated) |
| Codebase | 20 bounded contexts · 61 tables / 11 migrations · React dashboard · Docker/Compose · Prometheus/Grafana |
| Money-safety invariants | **Verified at source** (single trade authoriser, fail-closed everywhere, research structurally isolated, wallet layer never touches keys) |

## 2. Final architecture

- **Modular monolith of bounded contexts** (Clean Architecture + DDD), each split
  `domain / application / infrastructure`, communicating **only through domain events**.
- **CQRS** command/query buses; **event-driven** via an `EventBus` port with in-memory and
  **Redis Streams** transports; **ports & adapters** for every store and external service.
- **One-way decision pipeline** — Scanner → Features → Security → Wallet Intelligence → AI
  Committee → Risk → Execution → Portfolio. The AI layer *quantifies*; the **Risk Manager
  alone decides**; the **Execution Engine alone knows the mode**. (The `scoring` context and
  the Strategy Engine are **not** in this path — see `ARCHITECTURE_AUDIT_2026-07-28.md` §8.
  As of 2026-07-29 the Strategy Engine *is* connected as a veto and an exit requester when
  `STRATEGY_GATE_RISK=true`; it still never approves or sizes anything.)
- **Research Lab is offline and structurally isolated** (AST-enforced) — it produces
  knowledge, never orders.
- **Knowledge is the permanent memory** and closes the learning loop: it records from every
  producer and pairs each decision with its realised outcome. It is **structurally unable to
  act** — an AST test asserts it imports no other bounded context at all.
- **The Candidate Enricher is the loop's read half.** Every candidate is enriched from that
  memory before the committee sees it, and the committee accepts no other input type, so no
  token is ever judged from scratch.

See [`architecture.md`](architecture.md) for the flow, service, dependency and event maps.

## 3. Components implemented & functional coverage

| Area | Implemented | Coverage vs. intended scope |
|---|---|---|
| Scanner (RPC manager, DEX adapters, discovery, metadata, features, quality, pipeline, history) | ✅ | Full (paper); load-behaviour not yet measured |
| Feature Store (versioned, cached) | ✅ | Full |
| Security Engine (10 analyzers, critical-flag veto, explainable) | ✅ | Full |
| Wallet Intelligence (permanent on-chain KB, clustering, reputation) | ✅ | Full |
| Candidate Enricher (11 dimensions of history, mandatory, bounded, neutral when empty) | ✅ | Full |
| AI Committee (12 logistic specialists → meta, registry, shadow, drift, explainability) | ✅ | Full (advisory) |
| Scoring (probabilities + confidence + composite; never a decision) | ✅ | Full |
| Strategy Engine (15 plugins, weighted ensemble, dynamic weights, shadow lifecycle) | ✅ | **Connected** — may veto entries and request exits when `gate_risk` is on (off by default) |
| Risk Manager (sizing, exposure, drawdown, correlation, kill switch, breaker, emergency) | ✅ | Full; invariants verified |
| Execution Engine — paper | ✅ | Full; realized PnL now net of both fees |
| Execution Engine — live adapters (signer/quote/RPC) | ⛔ Not built (by design) | Deferred — pre-LIVE |
| Portfolio (positions, trailing, capital, PnL) | ✅ | Full (in-memory open map today) |
| Research Lab (backtest/WF/MC/optimizer/shadow/gauntlet/promotion) | ✅ | Full; isolation verified |
| Notification Service (event-driven Discord, uniform embeds) | ✅ | Full |
| Watchdog / health / auto-recovery / emergency mode | ✅ | Full (design); chaos not yet validated |
| Scheduler / backups / config-as-asset / audit | ✅ | Full |
| API (REST + WS) + Dashboard (read-only control center) | ✅ | Full; WS auth pending (H5) |
| Durable event store / execution ledger / open-position persistence | ⚠️ In-memory | Deferred — pre-LIVE (H1/H2/H3) |

## 4. Test coverage

- **733 backend tests**, all green, run **warnings-as-errors**; `mypy --strict` clean across
  all 456 source files; `ruff` clean.
- Coverage is **behavioural and invariant-focused**, which matters more than a line-count
  percentage for a safety-critical system: money-safety invariants, fail-closed paths, the
  research-isolation AST check, the paper/live seam, schema integrity, the realized-PnL fee
  accounting, the go-LIVE authentication guard, and now the exploration programme's four
  safety properties (it waives only conviction; it ends by itself; it cannot overspend across
  a restart; it stays reproducible by hand).
- **Not yet executed** (require a live Docker stack; no numbers are fabricated): load/stress
  testing, resilience/chaos testing, and CPU/RAM/latency profiling.

## 5. Objective component scores

Scores are 0–10, deliberately un-inflated. "Paper" is what matters today; the "→ LIVE"
column flags what must improve before real funds.

| Component | Score (paper) | Rationale | → LIVE gap |
|---|:---:|---|---|
| **Architecture** | 9.5 | Clean contexts, ports/adapters, CQRS, event-driven; no god-objects; no circular deps | Persist the event store (H1) to be truly event-sourced |
| **Security (app)** | 8.0 | No hardcoded/loggable secrets; mounted keypair; per-tx cap; constant-time key check; go-LIVE needs auth | API auth off by default (H4); WS auth missing (H5) |
| **Performance** | 7.0 | Sound design (bounded stats, event-derived metrics, Redis transport) | Unproven under load; cap in-memory caches; profile hot paths |
| **Scalability** | 8.0 | Redis bus + per-service consumer groups; K8s-shaped topology; plugin strategies/models | In-memory stores bound single-node durability until H1/H2/H3 |
| **Resilience** | 7.0 | Reconnects, timeouts, retries, RPC failover, auto-recovery, emergency mode, `restart: unless-stopped` | In-memory state lost on restart (H1/H2/H3); no PG-degradation strategy (M2); chaos not validated |
| **Observability** | 8.5 | Structured logs, Prometheus metrics, Grafana, audit trail, performance monitor, watchdog | Add error-rate metrics on the broad catches (M3) |
| **Maintainability** | 9.5 | Strict types, lint-clean, warnings-as-errors, living docs, low complexity, high cohesion | — |
| **Research** | 9.0 | Full offline lab; structurally isolated; fail-closed human-gated promotion | Replay needs the durable event store (H1) |
| **AI Committee** | 8.5 | Transparent, explainable, versioned, shadow + drift; only quantifies | Add an explicit train/serve leak assertion to the gauntlet |
| **Trading (paper)** | 8.5 | Faithful paper executor; realized PnL net of both frictions; single-position model documented | Live adapters unbuilt (by design); partial-exit accounting if scope grows |
| **Dashboard** | 8.0 | Complete read-only control center over every context | WS auth (H5); slow-consumer backpressure before public exposure |

**Weighted overall (paper): ≈ 8.4 / 10** — a strong, safe, maintainable platform with a
clear, bounded path to live.

## 6. Per-subsystem closing status

- **Research Lab** — ✅ Complete and **structurally safe**. Offline, works on copies,
  produces knowledge only, promotion is human-gated and fail-closed. Isolation is AST-verified.
- **AI Committee** — ✅ Complete (advisory). Twelve transparent specialists → meta model,
  versioned registry, shadow models, drift monitor, full explainability. Never decides or sizes.
- **Risk Manager** — ✅ Complete; the **sole trade authoriser**, fail-closed, defence layers
  ordered correctly (emergency → breaker → kill switch → token logic). Invariants verified.
- **Execution Engine** — ✅ Paper complete; realized PnL corrected (net of both fees).
  ⚠️ Order/txn ledger and open-position map are in-memory (H2/H3); live adapters intentionally
  unbuilt so the engine is paper-only by construction.
- **Dashboard** — ✅ Complete read-only control center. ⚠️ WebSocket auth pending (H5).
- **Watchdog** — ✅ Complete by design: liveness heartbeats, health checks, bounded
  auto-recovery, escalation to Emergency Mode. ⚠️ Crash/failover behaviour not yet chaos-tested.
- **Documentation** — ✅ `hades.md` (living reference), `README.md` (operations),
  `architecture.md` (maps), `TECHNICAL_AUDIT.md` (adversarial), and this report. Source
  docstrings reconciled with reality (the event-sourcing overstatement corrected at source).

## 7. Closed in this final pass (Phase 11, Stage 2)

| ID | Finding | Resolution |
|---|---|---|
| **M6** | Realized PnL omitted the buy-side fee | Capture entry fee at open; net **both** round-trip fees at close; +test |
| **M4** | Root containers, no cap-drop, `:latest`, admin ports public | `cap_drop: ALL` + `no-new-privileges`; pinned Prometheus/Grafana; admin UIs on `127.0.0.1` |
| **H4 (partial)** | Paper→live switch unauthenticated | Switch **to LIVE** now refuses the implicit `system` principal (403); +test |
| **L1** | 2 `UP046` (+ `RUF012`/`RUF002`/`RUF003`) lint findings | PEP-695 generics; `Field(default_factory=dict)`; Unicode dashes normalized — **0 lint findings** |
| **L3** | Test-suite deprecation warning | Suite now runs **warnings-as-errors** with the one third-party deprecation allow-listed |
| **L4** | `pyproject` version lagged docs | Synced to `0.10.0` |
| **Deploy** | `up -d` needed a manual `make migrate` | One-shot `migrate` service gates all app services — turnkey bring-up |
| **Docs (H1)** | `DomainEvent` docstring overstated durable event-sourcing | Corrected at source to describe the in-memory store + read-model persistence |

## 7a. Closed in Phase 1 — the learning loop (2026-07-28)

The [architecture audit](ARCHITECTURE_AUDIT_2026-07-28.md) found that the platform could not
learn from its own trades, and that the cause was structural rather than a matter of tuning.
Phase 1 introduced the `knowledge` bounded context and closed it.

| Was | Now |
|---|---|
| `KnowledgeFeedback.record_outcome()` had **zero callers** — realised trade results reached no ledger, leaving a **single-class dataset** on which `min_auc = 0.55` could never be met | A closed trade becomes a `Lesson` and lands in `committee_outcomes` at full weight |
| Features would naturally have been read at settlement — **temporal leakage** | Features are **frozen at `TradeApproved`** and the leaking version is unwritable: settling accepts an `Outcome` and nothing else |
| A promotion changed nothing until the worker restarted | `ModelPromoted` reloads the active committee in place |
| `dataset_quality` / `sample_support` were constants presented as measurements | Derived from the dataset; a single-class dataset scores **0.0**, which is the truth |
| `OrderSubmitted`/`OrderFilled`/`OrderFailed` were **never registered** on the bus, so under Redis they were discarded at the process boundary | Registered. Found by the new test that resolves every subscribed event name |

**What this does *not* claim.** Cold start is now *solvable*, not solved: the loop is closed
and proven end-to-end in tests, but the platform must still open and close real trades to
accumulate both classes. Generating those first positives without asking the committee to
decide before it can know is **Phase 2**, and must not be confused with lowering thresholds.

## 7b. Closed in Phase 2 — Research as the knowledge producer (2026-07-28)

The Research Lab — internal *and* the external repository — now feeds permanent memory, and
the connection is nothing but domain events.

| Was | Now |
|---|---|
| The memory recorded experiments but not the lab's **conclusions** — comparisons and promotion decisions went unrecorded | All absorbed, each under its own provenance |
| The **Replay Engine** had no caller anywhere and `ReplayCompleted` was registered but never published | `run_replay()` exists and publishes; a study that could not be run produced no knowledge |
| `contexts/research` and `contexts/strategy` **both defined `StrategyPromoted`**. The bus routes on the class name, so they collided on one key; under Redis a lab promotion was rebuilt as a strategy-engine promotion and audited as one. Nothing raised | Renamed `ResearchStrategyPromoted`; a scan across every `domain/events.py` now fails the build on any collision |
| The external lab had **no way to hand anything over** | `hades.knowledge/v1` — a checksummed JSON bundle, pull-based, fixture-tested in both repositories |

**The trust boundary.** Knowledge feeds the AI Committee's training ledger, so the inbox does
not believe its input. An external bundle cannot declare its verification (the field does not
exist; declaring it is a rejection), cannot claim a platform source such as `paper_trading` or
`executed_trade`, and **cannot express a lesson at all** — lessons are minted only by the
Decision Journal settling a real trade. The worst a hostile file achieves is inserting
clearly-labelled simulated observations.

Isolation is unchanged and still AST-verified: Research imports no `execution`, `risk` or
`portfolio` — and now no `knowledge` either, in both directions.

**Closed 2026-07-29 (option O1):** the candidate bridge now has a lab-side exporter, limited
to transparent logistic weight sets over Core's own `FeatureCatalog`. Tree ensembles are
refused by type and stay in the lab as hypothesis generators, exporting findings as knowledge
instead. The fixtures both suites parse are generated by that exporter.

## 7c. Closed in Phase 3 — the Candidate Enricher (2026-07-28)

Phases 1 and 2 built a memory and filled it. Nothing on the decision path read it.

| Was | Now |
|---|---|
| The committee judged every token as if the platform had never seen a token — the memory was write-only from the brain's point of view | Every candidate is enriched from the Knowledge Engine along **eleven** dimensions before the committee sees it |
| `sample_support`, documented as *"how many similar historical examples exist"*, was a number read from configuration | Measured per candidate from ground-truth cohorts only |
| A verdict could not be audited against what the platform knew at the time | The enrichment is persisted **with** the prediction, and stated in the explanation |
| A settled trade carried no cohort keys, so no future candidate could ever learn from it as a developer / narrative / launchpad / cluster | The committee's identity is remembered by the Knowledge runtime and merged into the decision's tags |

**Why this is not a threshold change.** With an empty memory every prior has zero strength,
the fused nudge is exactly `0.0`, and the fusion is bit-for-bit what it was before the stage
existed — pinned by test. Priors are shrunk toward 0.5 by a pseudo-count, are silent below a
minimum cohort size, and the total influence is capped
(`LEARNING_ENRICHMENT_MAX_PRIOR_LOG_ODDS`). History informs the committee; it cannot overrule
the token in front of it, because what a memory cannot know is what has changed since.

**Failure posture.** An unreachable memory produces a neutral enrichment labelled
*"could not ask"* and the token is still judged. The metrics separate `found` / `empty` /
`unavailable`, because a young platform and a broken one looking identical is the single most
expensive failure mode this codebase has had.

**Still open at the time:** the enricher makes the platform *use* what it has learned; it does
not create knowledge. The first settled trades still had to come from somewhere — that is the
deliberate bootstrap policy, now built as Phase 4 (§7d).

## 7d. Closed in Phase 4 — Exploration Mode (2026-07-29)

The deliberate bootstrap policy the audit asked for, built as a bounded context that is **off
by default** and cannot outlive its purpose.

| Was | Now |
|---|---|
| The cold start could only be broken by lowering `RISK_MIN_PROB_ROI_POSITIVE` — for **all** capital, permanently, on the strength of no evidence | A separate, budgeted path takes deliberately tiny samples while the memory is demonstrably short, and stops |
| No accounting existed for "trades taken to learn" — they would have been indistinguishable from a strategy losing small | An independent append-only ledger, a distinct knowledge source, an `exploration=true` tag on the settled lesson, and a separate approval metric |
| Nothing in the platform could decide it had *enough* evidence | `EvidenceStatus.sufficient` — a stated condition on settled lessons **and both classes** — evaluated on every request, latching the programme off when met |

**What it may waive, and what it may not.** Exactly one named policy, and only from the Risk
Manager's new *conviction* tuple (`min_probability`, `min_confidence`). The safety tuple
(security, developer, wallet, liquidity), the global defence layer (kill switch, circuit
breaker, emergency) and every allocation rule (capital, exposure, correlation, drawdown, rate
limits) apply unchanged. The split lives in one composition function and is asserted by name
in `test_exploration_isolation`, because moving `SecurityPolicy` across is a one-line edit that
would compile, pass every other test, and let the programme buy rug pulls a dollar at a time.

**Money-safety invariants unchanged.** `TradeApproved` is still constructed in exactly one
place; a grant is *eligibility with a dollar ceiling*, expressed in Risk's own vocabulary
through a port Risk declares, and a test asserts the grant type has no field that could
express an approval. The Execution Engine was not modified. Exploration holds no reference to
the guardian and an AST test forbids it importing execution, risk, portfolio, learning or
strategy — necessary, because this is the one context on the platform whose purpose is to
argue for trades the guardian would refuse.

**Budget posture.** Four independent ceilings (per trade, per day, per week, per lifetime),
all derived by aggregating an append-only table rather than from a counter — an in-memory
total would reset on restart and silently re-authorise the day's budget on every deploy. The
size is *fixed*, not conviction-weighted, so the lifetime budget states exactly how many
samples the programme can ever fund. The charge happens on approval, not on grant, so a grant
the allocation rules then veto costs nothing.

**Failure posture, and it is fail-closed in both directions.** An unreadable memory yields
`available=False` and declines (not knowing whether more evidence is needed is a reason to
stop spending, never to continue). An exception anywhere in the programme yields no grant, so
the candidate is rejected exactly as it would have been if exploration did not exist — pinned
by a test, because failing the other way is the one outcome this may never have.

**Explainability.** No bandit, no ε-greedy, no randomness, no model. Selection is the
candidate whose cohort the memory knows least about, ties broken by key name; a test asserts
twenty-five evaluations of the same inputs give the same verdict. Every verdict and every
approval carries the arithmetic behind it, and an exploration approval is never readable as an
ordinary conviction trade that happened to be small.

## 8. Known limitations

1. **Durability is in-memory for three stores** — the event store (H1), the execution
   order/transaction ledger (H2) and the open-position map (H3). Acceptable for paper;
   **blocking for live** (a restart loses this state).
2. **Live adapters are not built** (intentional) — signer, quote provider and RPC gateway
   must be written **and independently audited** before live is even possible.
3. **API auth is off by default** (H4) and **WebSocket endpoints have no auth** (H5) —
   safe on localhost/paper; both must be enforced before public or live exposure.
4. **No Postgres runtime-degradation strategy** (M2) — a mid-run DB outage throws per-write
   rather than pausing new entries gracefully.
5. **Load, resilience and profiling are unproven** — no SLA numbers are claimed; the biggest
   risk to watch is unbounded growth of in-memory caches/stores under sustained load.
6. **Single-position-per-mint, full-close accounting** — correct and documented for the
   current model; partial exits would need cost-basis tracking.
7. **The Strategy Engine is connected but off by default** — `STRATEGY_GATE_RISK=false`
   ships advisory. Turning it on lets the ensemble veto entries and request exits; both
   directions only reduce exposure. Enabling it is a posture change, not a bug fix.
8. **Cold start is now addressed, not yet resolved** — see §7a and §7d. The loop is closed,
   the brain reads the memory, and Exploration Mode exists to buy the first ground-truth
   samples on a bounded budget. But it is **off by default**, and until an operator enables it
   with a budget and the platform actually settles trades on both sides of zero,
   `is_trainable` will keep saying `false` — honestly. A programme that spends its whole
   lifetime budget without reaching sufficiency ends with a warning to the operator; the
   answer to that is a judgement about the platform, not a larger budget applied quietly.

## 9. Recommendations for Hades v2 (pre-LIVE roadmap)

**Must close before enabling LIVE (in priority order):**

1. **Durable execution ledger** — `PostgresOrderStore` + `PostgresTransactionStore` +
   migration `0008`, wired in `execution_runtime`. (H2)
2. **Persist open positions** — move `ExecutionEngine._open` to a repository (or rebuild from
   the portfolio read-model on boot). (H3)
3. **Durable event store** — Postgres-backed `EventStore` + migration; enables true
   event-sourced replay for the Research Lab. (H1)
4. **API auth on by default** in non-dev and enforced on all state-changing routes; **WS
   authentication** at `accept()` (coordinated server + dashboard change). (H4, H5)
5. **Build and independently audit the live adapters** (signer / quote / RPC gateway).

**Then, before trusting the numbers:**

6. **Execute the load & resilience suites** against a real Postgres/Redis/RPC stack; publish
   latency/throughput/RSS SLAs; add LRU/TTL caps to in-memory caches.
7. **Postgres-degradation strategy** — readiness gating + a DB circuit breaker. (M2)
8. **AI train/serve leak assertion** in the validation gauntlet.
9. **Container hardening pass 2** — `read_only` root FS + tmpfs where feasible; image digest
   pinning.
10. **WS slow-consumer backpressure** before exposing the dashboard beyond localhost.

## 10. Gate verdict

> **DO NOT enable LIVE** until every item in §9 (1–5) is closed *and* the load + resilience
> suites (§9.6) have been executed against a real stack. Until then, Hades is a
> production-grade **paper** platform — which is exactly, and safely, what it is today.

Principles honoured throughout: **security over speed, capital over profit, data over
intuition, explainability over black boxes, maintainability over quick fixes, modularity over
monoliths.**
