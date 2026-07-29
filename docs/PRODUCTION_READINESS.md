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
| Backend health | **629 tests passing**, `mypy --strict` clean (435 files), `ruff` clean (0 findings), suite runs **warnings-as-errors** |
| Trading mode | Paper only; live hard-gated (env gate × 2 + readiness checklist + explicit confirm + authenticated operator) and unbuildable (no live adapters) |
| Deployment | Turnkey: `git clone → configure .env → docker compose up -d` (schema auto-migrated) |
| Codebase | 19 bounded contexts · 60 tables / 10 migrations · React dashboard · Docker/Compose · Prometheus/Grafana |
| Money-safety invariants | **Verified at source** (single trade authoriser, fail-closed everywhere, research structurally isolated, wallet layer never touches keys) |

## 2. Final architecture

- **Modular monolith of bounded contexts** (Clean Architecture + DDD), each split
  `domain / application / infrastructure`, communicating **only through domain events**.
- **CQRS** command/query buses; **event-driven** via an `EventBus` port with in-memory and
  **Redis Streams** transports; **ports & adapters** for every store and external service.
- **One-way decision pipeline** — Scanner → Features → Security → Wallet Intelligence → AI
  Committee → Risk → Execution → Portfolio. The AI layer *quantifies*; the **Risk Manager
  alone decides**; the **Execution Engine alone knows the mode**. (The `scoring` context and
  the Strategy Engine are **not** in this path — see `ARCHITECTURE_AUDIT_2026-07-28.md` §8.)
- **Research Lab is offline and structurally isolated** (AST-enforced) — it produces
  knowledge, never orders.
- **Knowledge is the permanent memory** and closes the learning loop: it records from every
  producer and pairs each decision with its realised outcome. It is **structurally unable to
  act** — an AST test asserts it imports no other bounded context at all.

See [`architecture.md`](architecture.md) for the flow, service, dependency and event maps.

## 3. Components implemented & functional coverage

| Area | Implemented | Coverage vs. intended scope |
|---|---|---|
| Scanner (RPC manager, DEX adapters, discovery, metadata, features, quality, pipeline, history) | ✅ | Full (paper); load-behaviour not yet measured |
| Feature Store (versioned, cached) | ✅ | Full |
| Security Engine (10 analyzers, critical-flag veto, explainable) | ✅ | Full |
| Wallet Intelligence (permanent on-chain KB, clustering, reputation) | ✅ | Full |
| AI Committee (12 logistic specialists → meta, registry, shadow, drift, explainability) | ✅ | Full (advisory) |
| Scoring (probabilities + confidence + composite; never a decision) | ✅ | Full |
| Strategy Engine (15 plugins, weighted ensemble, dynamic weights, shadow lifecycle) | ✅ | Full (advisory; `gate_risk` off) |
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

- **378 backend tests**, all green, run **warnings-as-errors**; `mypy --strict` clean across
  all 407 source files; `ruff` clean.
- Coverage is **behavioural and invariant-focused**, which matters more than a line-count
  percentage for a safety-critical system: money-safety invariants, fail-closed paths, the
  research-isolation AST check, the paper/live seam, schema integrity, and now the
  realized-PnL fee accounting and the go-LIVE authentication guard.
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

**Still open:** the *candidate* (model) bridge remains one-sided and incompatible on format,
model family and feature space. That is a product decision (audit §7.4), not an
implementation gap.

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
7. **The Strategy Engine's output has no consumer** — `EnsembleSignalGenerated` is published
   and nobody subscribes, so fifteen strategies and the whole weighting apparatus influence
   no decision. Not a safety issue; it is dead weight presented as a pipeline stage. Connect
   it or freeze it explicitly.
8. **Cold start is unblocked, not resolved** — see §7a. The memory reports `is_trainable`
   honestly, and until the platform accumulates both classes it will say `false`.

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
