# Hades — Living Technical Documentation

> Professional quantitative platform for Solana, initially focused on meme coins.
> Designed as a modular, decoupled, event-driven system built to run 24/7 for
> years and to evolve through quantitative research.
>
> **This document is the living technical reference.** It is updated whenever an
> important capability is added. It is not a changelog — it describes the system
> as it is *now*.

---

## 0. Status

| | |
|---|---|
| **Current phase** | **Phase 11 — Production Hardening, Stage 2 (final code-quality & deployment closure)** |
| **Version** | `0.10.0` |
| **Trading** | Paper only. Live execution is hard-gated OFF (two switches), blocked by the Production Checklist (any failing required subsystem or an active Emergency Mode refuses the switch to LIVE), **and** — as of Stage 2 — the switch to LIVE additionally requires an authenticated operator (never the implicit `system` principal). The Execution Engine remains the *only* component that knows the mode; everything upstream is mode-agnostic. |
| **Backend tests** | **378 passing** · `mypy --strict` clean (407 src files) · **`ruff` clean (0 findings)** · suite runs **warnings-as-errors** |
| **Deployment** | `docker compose up -d` is now a complete, no-manual-steps bring-up: a one-shot `migrate` service applies the schema to head before any app service starts. |

> **⚠️ Architecture audit, 2026-07-28 — read before planning any new capability.**
> A full audit of the backend and of `HadesResearchLab` is recorded in
> [`docs/ARCHITECTURE_AUDIT_2026-07-28.md`](docs/ARCHITECTURE_AUDIT_2026-07-28.md).
> It found the code sound and the **event graph incomplete**: three open circuits mean
> the platform cannot learn from its own trades, no matter how it is configured.
>
> - **The learning loop has no write path.** `KnowledgeFeedback.record_outcome()` — the
>   method that turns a closed trade into a training sample — has **zero callers**. The
>   outcome ledger therefore holds only weak negatives from `TokenRejected`: a
>   **single-class dataset**, on which `ValidationEngine.min_auc = 0.55` can never be met.
>   No candidate model can ever be validated, so the committee is permanently pinned to its
>   default priors. **This, not the 0.4626 vs. 0.55 threshold, is the real cold-start lock.**
> - **The Strategy Engine has no consumer.** `EnsembleSignalGenerated` is published and
>   nobody subscribes. All fifteen strategies, the weighted ensemble and the dynamic weight
>   engine influence nothing. Risk consumes `CommitteePredictionGenerated` directly.
>   `strategy.gate_risk` is read only for logs.
> - **A model promotion does not reach the runtime** until the worker restarts —
>   `set_active()` is called only at startup and nothing subscribes to `ModelPromoted`.
>
> Also: `contexts/scoring` and `contexts/wallet` have **no wiring anywhere** (§6d/§6i and the
> §4 context list describe them as pipeline stages — they are not); the whole pipeline runs
> in the single `worker` process while `engine` sits idle; and the Research Lab bridge is
> incompatible on format, model family *and* feature space at once (see
> [`docs/RESEARCH_LAB_BRIDGE.md`](docs/RESEARCH_LAB_BRIDGE.md)).
>
> The audit's phase plan is ordered so that **the learning loop is closed before any
> threshold is recalibrated** — lowering thresholds first would produce trades whose results
> still never reach the ledger, hiding the defect behind apparent activity.

Phase 1 established the architecture skeleton (bounded contexts, contracts,
domain events, shared kernel). Phase 2 built the full platform the system runs on
(Docker services, DB, Redis, logging, watchdog, notifications, backups, API/WS,
dashboard shell, paper/live switch). **Phase 3 builds the data-acquisition system
— the Scanner** — still with **no** scoring, **no** strategies and **no** AI/ML,
and it **never trades or decides**. It continuously discovers, analyses and
stores everything about the Solana ecosystem so later phases can decide well. It
adds: a multi-provider **RPC Manager** (health-scored, auto-failover); a
**Discovery Engine** fed by independent **DEX adapters** (pump.fun, Raydium,
Orca, Meteora, Jupiter, DexScreener); a **Metadata Collector**; a rich
**Feature Engine** (hundreds of features); a **Quality Validator**; a
back-pressured **Acquisition Pipeline** with a worker pool; a **History Builder**
(snapshots); the feature store; a live Scanner dashboard screen; and full metrics
(see §6b). It runs inside the `worker` service, designed to run for years without
degrading.

**Phase 4 adds the Security Engine** — a deliberately conservative rug/scam
guardrail that screens every measured token and answers only *"does this token
deserve to keep being analysed?"*. It **never trades or decides**: it approves or
rejects continued analysis and produces an explainable score for the future AI
Committee. Ten single-responsibility analyzers (contract, authority, liquidity,
pool, holder, honeypot, developer, wallet-cluster, transaction, behavior) run as
pure functions over a context the assembler pre-fetches; a single critical flag
hard-vetoes, and reasonable doubt rejects. It adds dynamic append-only
black/white lists, an accumulating developer reputation, bounded funding-graph
wallet clustering, full explainability, research persistence of every verdict
(including rejections), events, metrics and a read-only API (see §6c). Also hosted
by the `worker` service. **No AI/ML yet** — that is a later phase.

**Phase 5 adds Wallet Intelligence** — a wallet-centric, permanent on-chain
knowledge base. Reacting to every completed security analysis, it gives every
observed wallet a lasting identity and accumulates its history, evolving
non-binary reputation, behaviour, funding lineage, relationships, cluster
membership and influence — **never deleting anything** (profiles gain versions,
the timeline and knowledge base only grow). It measures smart vs dumb money rather
than assuming, and every Wallet Score comes with its reasons. Like everything
before it, it **only knows — it never trades, never enables live, and runs no ML**;
it builds one of the platform's most valuable long-lived assets for the future AI
Committee (see §6d). Also hosted by the `worker` service.

**Phase 6 adds the AI Committee** — the platform's explainable quantitative
*brain*. It is deliberately **not** a black box that says "buy": it is a committee
of twelve single-purpose **specialist models** (liquidity, momentum, volatility,
market regime, wallet intelligence, security, holder distribution, developer,
behaviour, risk, timing, microstructure), each emitting only a *quantitative
opinion* — a probability, a confidence and the reasons behind both. A **Meta
Model** fuses those opinions into exactly three calibrated probabilities —
`P(ROI positive)`, `P(hit TP)`, `P(hit SL)` — plus a multi-factor confidence.
Every model is a **transparent linear-logistic scorer** (weights are data, stored
and diffable), so a prediction always decomposes into per-feature contributions;
there are no opaque parameters and **no heavy ML deps** (pure Python — runs on the
low-power VPS). It reacts to `WalletIntelligenceComputed` (the last analytical
stage), assembles a decision context from the Feature Store + security verdict +
wallet snapshot, and emits an auditable `CommitteePrediction`. It adds a
**Feature Store contract** (normalised/versioned/documented features), a
**Dataset Builder** (learns from executed *and* rejected opportunities), a
pure-Python **Training Engine**, a **Validation Engine** (walk-forward, cross-val,
OOS, paper-replay, incumbent comparison — a worse model is never deployed), an
append-only **Model Registry** with human-gated promotion and **model
versioning**, **Shadow models**, a **Confidence Engine**, an **Explainability
Engine**, continuous **Feature Importance**, **Knowledge Feedback**, and a
**Model Monitor** (data / feature / concept drift). Like everything before it, the
AI **only quantifies — it never buys, sells or sizes**; the Risk Manager (a later
phase) is the sole decision-maker (see §6e). Hosted by the `worker` service.

---

## 1. Guiding principles

- **Clean Architecture** — dependencies point inward. Domain knows nothing of
  infrastructure; infrastructure implements domain-defined ports.
- **Domain-Driven Design** — the system is split into *bounded contexts*, each
  with its own domain model, ubiquitous language and contracts.
- **Event-Driven Architecture** — contexts never call each other directly; they
  react to **domain events** on a bus.
- **Event Sourcing** — every meaningful fact is an append-only event. State is a
  fold over history. *We never lose the history.*
- **CQRS** — the write path (commands → operational store) is separated from the
  read path (queries → read models / analytics), so heavy analytics never
  contend with the trading loop.
- **SOLID everywhere** — small single-purpose classes, dependency inversion via
  ports, composition over inheritance, open/closed extension points.
- **Configuration over code** — every RPC, threshold, weight, list and toggle
  lives in `.env`. No secret and no tunable is ever hardcoded.
- **Observability** — structured logs (no `print`), Prometheus metrics, health
  probes. Nothing fails silently.
- **Linux + Docker only** — nothing is installed on a host. `docker compose up -d`
  brings the whole platform up.
- **Same decision engine, paper or live** — only the Execution Engine adapter
  differs. Live trading is gated behind two independent switches.

---

## 2. Repository layout

```
Hades/
├── docker-compose.yml            # full stack; `docker compose up -d`
├── docker-compose.override.yml   # dev hot-reload overrides (auto-loaded)
├── Makefile                      # developer entrypoints (all via Docker)
├── .env.example                  # single source of truth for configuration
├── hades.md                      # THIS document
├── backend/
│   ├── Dockerfile                # multi-stage, non-root runtime
│   ├── pyproject.toml            # deps + ruff/mypy/pytest config
│   ├── alembic.ini, alembic/     # async migrations (DSN injected from settings)
│   ├── src/hades/
│   │   ├── shared_kernel/        # cross-cutting: config, logging (+ring buffer),
│   │   │                         #   events (bus/redis-bus/store/registry), cqrs,
│   │   │                         #   persistence (+models/), cache (redis),
│   │   │                         #   analytics (clickhouse), observability, errors
│   │   ├── contexts/             # the bounded contexts (see §4); Phase 2 fills
│   │   │   │                     #   application/ + infrastructure/ for
│   │   │   │                     #   notification, monitoring, execution
│   │   │   └── common/           # shared "published language" value objects
│   │   ├── api/                  # FastAPI: routers/ (+status/info/config/trading),
│   │   │   │                     #   ws/ (WebSocket), security (auth scaffold)
│   │   ├── ops/                  # process entrypoints + service base, healthcheck,
│   │   │                         #   liveness, backups, watchdog/scheduler/engine
│   │   └── bootstrap.py          # composition root (the only wiring place)
│   ├── alembic/versions/         # migrations (0001 = full baseline schema)
│   └── tests/
├── frontend/                     # React + TS + Vite + Tailwind dashboard shell
│   └── src/{pages,components,api} # 12 screens, ModeSwitch, WS hooks
├── infra/
│   ├── prometheus/               # scrape config (api + all services)
│   └── grafana/                  # provisioning (datasource)
└── docs/
```

### Dependency rule (enforced by convention + review)

```
api / ops  ─►  application (handlers)  ─►  domain (entities, events, ports)
                          │                        ▲
                          ▼                        │
                  infrastructure (adapters) ───────┘  implements ports
shared_kernel  ◄─ everyone may import; it imports no context
contexts.common (published language) ◄─ every context may import
```

A context **never** imports another context's `application` or `infrastructure`.
The only cross-context coupling allowed is: (a) subscribing to another context's
**domain events**, and (b) depending on a **narrow read port** it declares
itself (e.g. Risk's `PortfolioStateProvider`).

---

## 3. Shared Kernel (`hades.shared_kernel`)

Domain-agnostic building blocks. It never imports a context.

| Module | Responsibility |
|---|---|
| `config/settings.py` | Typed, validated settings loaded from env. Nested per-concern models. `settings.is_live` is the single authoritative live-trading predicate (requires mode=`live` **and** the enable flag). |
| `logging/setup.py` | `structlog` config — JSON in prod, console in dev. `get_logger()`. No `print` anywhere. |
| `observability/metrics.py` | Prometheus `MetricsRegistry` with idempotent metric factories; rendered at `/metrics`. |
| `domain/identifiers.py` | `EntityId` (UUIDv7, time-ordered) with a pydantic core schema. |
| `domain/events.py` | `DomainEvent` base — immutable, past-tense facts with metadata + `to_envelope()`. |
| `domain/base.py` | `ValueObject`, `Entity`, `AggregateRoot` (records events, tracks version). |
| `events/bus.py` | `EventBus` port + `InMemoryEventBus`. At-least-once, handlers must be idempotent. |
| `events/store.py` | `EventStore` port + `InMemoryEventStore`. Append-only, per-aggregate, optimistic concurrency (`ConcurrencyError`). |
| `cqrs/` | `Command`/`Query` bases, `CommandHandler`/`QueryHandler`, `CommandBus`/`QueryBus`. |
| `persistence/database.py` | Async SQLAlchemy engine + session context manager, declarative `Base`. |
| `persistence/unit_of_work.py` | `UnitOfWork` contract — atomic persist-then-publish. |
| `errors/exceptions.py` | Error hierarchy split into **domain** (business) vs **infrastructure** (transient); mapped to HTTP in one place. |

---

## 4. Bounded contexts (`hades.contexts`)

Each context ships (in Phase 1) its **domain events** and **ports** (contracts).
Stateful contexts also ship their aggregates/value objects. `application` and
`infrastructure` layers are populated in later phases.

The trade lifecycle flows entirely through events:

```
scanner ─TokenDiscovered─► features ─FeaturesComputed─► security ─TokenApproved─►
(security rejects → TokenRejected, flow stops — the token never reaches the AI Committee)
wallet ─WalletScoreComputed─► market ─MarketSnapshotUpdated─► scoring ─FinalScoreComputed─►
risk ─TradeApproved─► execution ─OrderFilled─► portfolio ─PositionOpened─►
… (price ticks drive PositionUpdated / TrailingStopAdjusted / PositionClosed) …
─► learning ─ModelPromotionProposed─► (human-gated)
research, notification, monitoring observe the whole flow.
```

| Context | Responsibility | Emits | Key ports |
|---|---|---|---|
| **scanner** | Discovery only: poll sources, dedup, coarse gating. | `TokenDiscovered` | `TokenSource`, `SeenTokenRegistry` |
| **features** | Compute the (hundreds of) feature variables; persist to feature store. | `FeaturesComputed` | `FeatureExtractor`, `FeatureStore` |
| **security** | Conservative rug/scam guardrail: 10 analyzers (contract, authority, liquidity, pool, holder, honeypot, developer, wallet-cluster, transaction, behavior) → sub-scores + final `Score` + flags/positives + explanation. Approves or rejects; **never trades**. | `SecurityScoreComputed`, `TokenApproved`, `TokenRejected`, `ContractRiskDetected`, `LiquidityWarning`, `ClusterFound`, `DeveloperRisk` | `SecurityCheck`, `OnChainReader`, `SwapSimulator`, `ClusterDetector`, `ListRegistry`, `DeveloperReputationStore`, `SecurityRepository` |
| **wallet** | Deployer/holder reputation, smart-money clustering → `Score`. | `WalletScoreComputed` | `WalletProfiler`, `HolderGraphProvider` |
| **market** | Price/liquidity/volume snapshots + live price feed. | `MarketSnapshotUpdated`, `PriceTicked` | `MarketDataProvider`, `PriceFeed` |
| **scoring** | Fuse signals → **probabilities + confidence + composite score. Never a decision.** Explainable, versioned model. | `FinalScoreComputed` | `ScoringModel`, `ModelRegistry`, `ScoreAggregator` |
| **risk** | The **sole decision authority**. Policy chain + sizing + kill switch. | `TradeApproved`, `TradeRejected`, `KillSwitchEngaged` | `RiskPolicy`, `PortfolioStateProvider`, `KillSwitch` |
| **portfolio** | Book of record. `Position` aggregate lifecycle + exposure/PnL read model. | `PositionOpened/Updated/Closed`, `TrailingStopAdjusted` | `PositionRepository` |
| **execution** | **The only paper↔live seam.** One `Executor` port, two adapters. | `OrderSubmitted/Filled/Failed` | `Executor`, `TransactionSigner` |
| **learning** | Build datasets from history, retrain/evaluate, **propose** (never auto-promote). | `ModelTrained`, `ModelPromotionProposed` | `TrainingDataAssembler`, `ModelTrainer`, `ModelPublisher` |
| **research** | Offline R&D on **copied** data. **No path to execution.** Human-gated promotion. | `ExperimentCompleted`, `CandidateProposed` | `ExperimentRunner`, `HistoricalDataReader` |
| **notification** | Outward alerts, severity-gated, transport-agnostic (Discord first). | — | `Notifier` |
| **monitoring** | Watchdog (heartbeats) + Health Monitor (dependency probes → `/health`). | `ComponentHeartbeat`, `HealthDegraded/Recovered` | `HealthProbe`, `HeartbeatSink` |

**`contexts/common`** — the *published language*: `TokenMint`, `WalletAddress`,
`TokenRef`, `Money`, `Percentage`, `Probability`, `Confidence`, `Score`. These
immutable value objects enforce their invariants at construction and travel
inside events, so no downstream context re-validates them.

### The "no IA que adivina" rule

The Scoring Engine outputs **only** calibrated probabilities and confidences
(e.g. `P(ROI > 20%) = 0.82, confidence 0.91`) plus an explainable composite
`Score`. It never says "buy". The **Risk Manager** is the only component that
turns a probability into an action, applying hard limits and sizing.

### The paper/live seam

Everything upstream of `execution` is identical in both modes. At bootstrap, the
`Executor` implementation is chosen from `settings.is_live`:
`PaperExecutor` (deterministic simulation) or `LiveExecutor` (real Solana txs).
Both return an identical `FillReport`, so Portfolio/Learning/Dashboard are
oblivious to mode. Live requires **both** `HADES_TRADING_MODE=live` **and**
`HADES_LIVE_TRADING_ENABLED=true`.

---

## 5. Presentation & operations

- **`api/`** — FastAPI app factory (`create_app`). Thin adapter: translates
  HTTP/WS into commands/queries, maps domain errors to HTTP centrally
  (`api/errors.py`). Endpoints today: `GET /health`, `GET /ready`,
  `GET /api/v1/meta`, `GET /metrics`.
- **`api/routers/`** — Phase 2 adds `status`, `version`, `info`, `config`,
  `trading` (the paper↔live switch) alongside `health`, `meta`, `metrics`.
  **`api/ws/`** streams the terminal + live status over WebSocket.
- **`ops/`** — process entrypoints (`hades-api|worker|engine|watchdog|scheduler|`
  `notification|backup`) sharing a `ServiceProcess` base (signals, liveness
  heartbeat, Redis-bus consumer, metrics server, graceful shutdown).
- **`bootstrap.py`** — the composition root; the *only* place adapters are
  chosen and wired. Each process passes its `role` so the Redis bus consumes
  under a per-service group. Explicit, greppable, no DI-magic.

---

## 6. Infrastructure & running

```bash
make init            # create .env from template
make up              # docker compose up -d  (all services)
make up-all          # + analytics (ClickHouse) + observability (Prometheus/Grafana)
make migrate         # alembic upgrade head  (0001 = full baseline schema)
make backup          # create a backup now (DB + config + models + research + docs)
make test / make lint  # pytest / (ruff + mypy strict) in the api container
make logs            # tail all services  ·  make logs-service s=watchdog
```

### Services (all Docker; nothing installed on the host)

| Service | Image / role | Port | Healthcheck |
|---|---|---|---|
| **postgres** | operational + event store | — (dev: 5432) | `pg_isready` |
| **redis** | cache/locks/queues/pub-sub + event bus | — (dev: 6379) | `redis-cli ping` |
| **api** | FastAPI REST + WebSocket | 8000 | HTTP `/health` |
| **dashboard** | React control center | 5173 | HTTP `/` |
| **engine** | decision engine (skeleton) | — | liveness file |
| **worker** | general background loops | — | liveness file |
| **scheduler** | periodic jobs (backups, cleanup) | — | liveness file |
| **notification** | Discord delivery service | — | liveness file |
| **watchdog** | health monitor / watchdog | — | liveness file |
| **clickhouse** | analytics (profile `analytics`) | 9000/8123 | — |
| **prometheus / grafana** | observability (profile) | 9090 / 3001 | — |

Every service: its own Dockerfile command, healthcheck, env from `.env`,
`restart: unless-stopped`, JSON logs. Background services expose no HTTP surface,
so their healthcheck reads a **liveness heartbeat file** on a shared volume
(`python -m hades.ops.healthcheck --role <role>`); if a loop hangs the file goes
stale and Docker restarts the container.

### Networks (three-tier isolation)

- **frontend** — dashboard ↔ api (+ watchdog, which spans all tiers to observe).
- **backend** — inter-service traffic + Redis bus (api, engine, worker, scheduler,
  notification, watchdog).
- **database** — datastores (postgres, redis, clickhouse) + the app services.

Only `api` (8000) and `dashboard` (5173) publish ports to the host in production;
datastore ports are dev-only (in the override). "No exponer servicios innecesarios."

### Volumes (everything durable survives restarts)

`postgres_data`, `redis_data`, `clickhouse_data`, `grafana_data`, and the platform
volumes: `hades_logs`, `hades_run` (liveness), `hades_config`, `hades_models`,
`hades_backups`, `hades_datasets`, `hades_research`, `hades_docs`.

Persistence layers: **PostgreSQL** = write model (event store + operational,
command side). **Redis** = cache + event-bus transport (Streams) + ephemeral
primitives (never a system of record). **ClickHouse** = analytical read model
(optional, prepared).

---

## 6a. Platform infrastructure (Phase 2)

### Database schema (`shared_kernel/persistence/models/`)

26 tables, every one with a UUIDv7 primary key, `created_at`/`updated_at`,
explicit indices and foreign keys (deterministic constraint names via a metadata
naming convention). Grouped: **event store** (`domain_events` append-only with a
global sequence + `(aggregate_id, version)` unique for optimistic concurrency;
`event_snapshots`); **reference** (`tokens`, `wallets`); **market**
(`market_data`, `features`); **scoring** (`signals`); **strategy/AI**
(`strategies`, `strategy_versions`, `models`); **trading** (`positions`, `trades`
+ mode-specific `paper_trades`/`live_trades`); **portfolio** (`portfolio_history`,
`equity_curve`, `pnl_history`); **research** (`research_jobs`, `experiments`,
`accepted_opportunities`, `rejected_opportunities`); **ops** (`notifications`,
`health_checks`, `risk_events`, `audit_log`, `system_configuration`). The baseline
Alembic migration builds directly from `Base.metadata` so it can never drift.

### Redis (`shared_kernel/cache/`)

`RedisProvider` (one lazy async client) plus ephemeral primitives: `CacheService`
(TTL JSON cache), `DistributedLock` (SET NX PX + safe token release),
`RateLimiter` (fixed window), `RedisQueue` (LPUSH/BRPOP), `PubSub`. The
`RedisEventBus` (`events/redis_bus.py`) is a Streams-based `EventBus`: publish
`XADD`s an envelope; each service consumes under its own **consumer group** (so
all services see every event); an `EventRegistry` rebuilds typed events on the
far side. Selected by `EVENT_BUS_TRANSPORT`.

### Logging (`shared_kernel/logging/`)

structlog routed through stdlib logging → stdout (JSON in prod) **plus** rotating
files: a combined `hades.log` and one per domain (engine, api, dashboard,
research, trading, risk, discord, watchdog). A processor also mirrors every
record into an in-memory **ring buffer** that feeds the dashboard terminal over
WebSocket (server push, no polling).

### Watchdog & Health Monitor (`contexts/monitoring/`)

Independent `watchdog` service. Probes (`HealthProbe`): Postgres, Redis, RPC
(`getHealth`), API + dashboard (HTTP), ClickHouse, and host CPU/RAM/disk
(`psutil`). The `HealthMonitor` folds them into `SystemHealth`, persists
`health_checks`, exports Prometheus gauges, and — **only on a status transition**
— emits `HealthDegraded`/`HealthRecovered` and requests a notification. The
`Watchdog` loop also verifies each background service's liveness file and raises a
`risk_event` + critical alert if one goes stale.

### Notifications (`contexts/notification/`)

**Discord only.** No module ever calls Discord directly — they publish a
`NotificationRequested` event (via `NotificationPublisher`); the `notification`
service is the sole consumer. It gates by severity, routes by topic
(alerts/trades webhooks), retries with backoff, rate-limits, and records every
attempt in `notifications`.

### Backups (`ops/backups.py`)

`BackupManager` produces timestamped, restorable archives: `pg_dump` (custom
format) + config/models/research/docs (+ optional logs), with retention pruning.
`restore()` uses `pg_restore --clean`. Run by the scheduler and via `make backup`.

### API & WebSocket

`GET /health`, `/ready`, `/metrics`, `/api/v1/{meta,version,info,status,config}`.
Trading-mode: `GET /api/v1/trading/mode`, `POST …/mode/verify`, `POST …/mode`.
WebSocket: `/ws/terminal` (live logs), `/ws/status` (runtime posture). Auth is
scaffolded (`api/security.py`) and disabled by default.

### Paper ↔ Live switch (`contexts/execution/application/trading_mode.py`)

The switch can **never** enable live directly. Enabling live requires: the hard
env gate `HADES_LIVE_TRADING_ENABLED`, all readiness checks (wallet, RPC, risk
config) to pass, and an explicit confirmation. The change is persisted to
`system_configuration` (DB authority), written to `audit_log` + `risk_events`,
emitted as `TradingModeChanged`, and announced on Discord. Effective `is_live`
always ANDs the persisted mode with the env gate — the DB can never bypass it, so
the posture is consistent across env, DB, API, dashboard and notifications.

### Dashboard (`frontend/`)

React + TS + Tailwind (dark, minimal). Sidebar + header with the guarded
`ModeSwitch` and a live-status banner. 12 screens: System, Trading Mode, Scanner,
**Wallet Intel**, Portfolio, Research, Risk, AI, Health, Logs, Terminal,
Configuration. System / Health / Config / Trading / **Scanner** / **Wallet Intel**
pull real data; the rest show prepared structure. The Terminal streams logs over
WebSocket.

---

## 6b. Data acquisition — the Scanner (Phase 3)

The Scanner is the system's senses. It answers only one question — *"what do we
actually know about this token?"* — and never *"should I buy?"*. It discovers,
enriches, validates and stores facts, emitting a domain event at every stage. It
runs inside the **`worker`** service (`ops/scanner_runtime.py` wires the graph;
`ops/worker.py` starts it), reacting to nothing but its own sources and the clock.

### Flow

```
DEX sources ─► Discovery Engine ─(dedup + coarse gate)─► Acquisition Pipeline
                                                              │  (bounded queue,
                                                              │   worker pool)
                              metadata → validation → storage → events
                                                              │
   TokenDiscovered ─► Feature Engine ─FeaturesComputed─► History Builder (snapshots)
```

Discovery emits `TokenDiscovered`; the Feature Engine reacts (the "features"
stage), stays decoupled per the event-driven rule, and its `FeaturesComputed`
drives the History Builder. No stage is skipped: each validates its input.

### RPC Manager (`shared_kernel/solana/rpc_manager.py`)

The single Solana access layer for every context. It routes each JSON-RPC call
through the **healthiest** of many configured providers (Helius, QuickNode,
Chainstack, Alchemy, a self-hosted node…), declared as JSON in `RPC_ENDPOINTS`
(never hardcoded; falls back to `SOLANA_RPC_HTTP_URL`). Per provider it tracks a
**latency EWMA**, a **failure streak**, a **per-second rate limit** and a derived
**health score**; it ranks by `(health, priority)`, **fails over automatically**
across providers within `RPC_MAX_ATTEMPTS`, **parks** an unhealthy provider until
a recovery-probe window elapses, and **reconnects** it on recovery. Every switch
of the active provider fires an injected callback → a `RpcEndpointSwitched` domain
event, and is recorded in Prometheus (`hades_rpc_*`). It is transport-agnostic
(a `RpcTransport` port; the default wraps `httpx`), so its routing/health/failover
logic is fully unit-tested with no network.

### Discovery Engine (`contexts/scanner/application/discovery_engine.py`)

Runs every configured `TokenSource` in its own supervised task, **deduplicates**
(Redis TTL registry) and applies **coarse gating only** (min liquidity, black/
white lists — whitelisted mints bypass the quantitative gates, never the
blacklist), then hands new candidates to the pipeline. A source that errors or
ends emits `SourceHealthChanged`, backs off exponentially and reconnects — one
bad DEX never stops the others.

### DEX adapters (`contexts/scanner/infrastructure/sources/`)

One independent adapter per protocol — **pump.fun, Raydium, Orca, Meteora,
Jupiter, DexScreener** — each subclassing a shared `HttpPollingSource` and
implementing *only* its own response parsing (logic is never mixed between
DEXes). A `factory` builds the set named in `SCANNER_SOURCES`; adding a protocol
means dropping in one adapter and registering it. A WebSocket-based source can
implement `stream()` directly (the port only needs `name` + `stream`).

### Metadata Collector (`contexts/scanner/application/metadata_collector.py`)

Gathers *everything* about a token by running complementary `MetadataProvider`s
concurrently and merging their partials (first-non-null wins; provenance kept).
The **RPC provider** reads the mint account (decimals, supply, mint/freeze
authorities, owning program); the **DexScreener provider** fills name, symbol,
logo, website and socials. A provider that fails returns `None` and is skipped —
collection is always best-effort and total. Every field is stored even if unused
(`token_metadata`), because the point is to accumulate knowledge.

### Feature Engine (`contexts/features/`)

Transforms a cleaned `FeatureInputs` bundle into a **versioned** `FeatureSet` of
hundreds of numeric features, composed from six single-purpose blocks — **basic**
(magnitudes, growth, ratios), **technical** (EMA/SMA/VWAP/ATR/RSI/MACD/momentum/
ROC/volatility/slopes over several look-backs), **temporal** (age, time-since-
first-event, cyclical calendar encodings), **pool**, **holders** and **market
regime** (micro/macro trend, acceleration, vol compression/explosion, buyer/
seller dominance). Indicators are dependency-free (no numpy) and total. Any
non-finite value is dropped before storage. Every vector is stamped with
`FEATURE_SCHEMA_VERSION`, so a definition change never breaks older vectors
(compatibility is preserved, versions accumulate). It computes measurements only.

### Feature Store (`contexts/features/infrastructure/feature_store.py`)

Durable in PostgreSQL (one row per feature value in `features`, tagged with the
schema version), fronted by a Redis cache of the latest vector (transient only).
So features are never recomputed unnecessarily and training/serving see identical
values. An in-memory store backs the tests.

### History Builder / Snapshot System

`HistoryBuilder` reacts to `FeaturesComputed` and writes a timestamped snapshot
row (`token_snapshots`, JSONB state + schema version) so **any historical moment
can be reconstructed** — feeding future backtests, learning datasets and research.

### Quality Validator (`contexts/scanner/application/quality_validator.py`)

A pure, side-effect-free service checking the five families the spec names —
**negative values, duplicates, extreme outliers, invalid timestamps, incomplete
information**. It returns a `ValidationOutcome` (fatal issues reject the datum;
non-fatal are annotations); the pipeline records every issue to `data_anomalies`
and emits `DataQualityAnomalyDetected`. Corrupt data never reaches the store.

### Acquisition Pipeline (`contexts/scanner/application/pipeline.py`)

A bounded `asyncio.Queue` + worker pool (`PIPELINE_WORKERS`). Discovery enqueues;
when the queue is full it drops the **oldest** pending candidate and counts it
(**backpressure**), so a burst never exhausts memory. Each worker runs the ordered
stages (metadata → validation → storage → events), each **timed and timeout-
guarded**; any stage error/timeout is a metric, never a crash. It is built to run
for months unattended.

### Events, metrics & dashboard

Every stage emits a fact — `TokenDiscovered`, `PoolDiscovered`,
`TokenMetadataCollected`, `SignificantChangeDetected`, `FeaturesComputed`,
`DataQualityAnomalyDetected`, `SourceHealthChanged`, `RpcEndpointSwitched` — all
registered on the Redis event bus. Prometheus counters/gauges/histograms cover
tokens discovered/processed, per-stage timing, errors, timeouts, anomalies,
backpressure drops, features/second, queue depth, active workers, source
availability and RPC health. The `worker` publishes a live status blob to Redis
every 5s (transient); the API `GET /api/v1/scanner/status` reads it (+ DB counts)
and the **Scanner** dashboard screen renders tokens analysed/new, DEX active, RPC
active + latency, features/second, queue depth and workers — **no risk info**.

### Persistence (Phase-3 tables, migration `0002`)

`token_metadata` (one current row per token), `data_anomalies` (validator
findings), `token_snapshots` (history). Additive only — the 26 existing tables are
untouched. Redis holds only transient state (dedup window, latest-feature cache,
live status); PostgreSQL is the system of record.

---

## 6c. Security Engine — the rug/scam guardrail (Phase 4)

The Security Engine answers exactly one question: **"does this token deserve to
keep being analysed?"** It never buys, never sizes, never trades. It screens
every token the Feature Engine measures and either approves it (it may proceed to
the — later — AI Committee) or rejects it (the flow stops here). Its governing
principle is conservatism: *rather miss a hundred opportunities than enter one
rug*. When there is reasonable doubt, it rejects.

### Design: pure analyzers over a pre-assembled context

All I/O happens in one place — the **assembler**
(`infrastructure/assembler.py`). Reacting to `FeaturesComputed`, it gathers
(concurrently, all best-effort) the authoritative mint account, the largest
holders, the Scanner-stored facts (socials, pool, deployer), LP burn/lock
coverage, a honeypot buy+sell probe, the deployer's reputation and the
wallet-cluster funding graph, into an immutable `SecurityInputs`. Every analyzer
is then a **pure function** of that bundle — deterministic and fully testable
without a network. Missing data becomes *doubt* (a penalty), never an implicit
pass.

### The 10 analyzers (`application/analyzers/`)

Each has a single responsibility, returns an `AnalyzerReport` (sub-score + flags +
positives + structured facts), and is composed by the engine (Open/Closed —
adding one touches nothing else):

- **Contract** — owning program (SPL vs Token-2022), Token-2022 behaviour-changing
  extensions (transfer fee/hook, permanent delegate…), decimals, supply.
- **Authority** — mint authority renounced? freeze authority present (can freeze
  wallets = latent honeypot)? metadata mutable? Renounced authorities are surfaced
  as explicit positives.
- **Liquidity** — depth vs floor, and LP burned/locked share (unlocked LP is the
  textbook rug); an undeterminable lock is treated as unsecured.
- **Pool** — venue soundness: exists, on a known DEX, not dust, not abandoned.
- **Holder** — inequality, not headcount: top-1 / top-10 concentration, **HHI**,
  **Gini**; the pool vault and burn addresses are excluded so locked supply is
  never mistaken for a whale.
- **Honeypot** — sellability + buy/sell tax asymmetry from the simulation; an
  active freeze reinforces the concern; unverifiable ⇒ doubt, never a pass.
- **Developer** — the deployer track record Hades **accumulates over time**
  (created / rugged / survived / succeeded). A known scammer vetoes; an unknown
  deployer is a mild penalty, never a free pass (honest cold-start).
- **Wallet-cluster** — funding-graph clustering of the top holders (wallets funded
  by the same source are likely one entity); a cluster controlling a large supply
  share is a coordinated dump risk a naive holder count hides. Bounded RPC budget.
- **Transaction** — wash-trading footprint (volume with few unique buyers,
  recurring-wallet churn) from the aggregated features.
- **Behavior** — aggregate flow psychology (everyone buys nobody sells; wildly
  one-sided pump). Stays neutral when there is too little activity to judge.

### Scoring, rug-pull composite & the veto (`application/scoring.py`)

The scorer produces the per-analyzer sub-scores plus a derived **rug-pull**
composite (dragged down by its weakest rug dimension — a single fatal vector
dominates, it is not averaged away), then a weighted **final security score**.
Two hard rules make it conservative: **any `CRITICAL` flag hard-vetoes** the token
(floors the score, rejects it, regardless of everything else); and the **doubt
rule** rejects a borderline token when too many analyzers ran blind. A whitelisted
subject can waive *doubt* — but never the critical veto or the minimum-score floor.

### Explainability (`application/explainability.py`)

No verdict is ever just "approved". Every assessment carries an ordered list of
the positives that helped and the negatives (by severity) that hurt, plus a
one-line summary — exactly what the dashboard shows and the audit log stores.

### Blacklist / Whitelist engines (`application/lists.py`)

Dynamic, **append-only** reputation lists across every subject kind (token,
deployer, wallet, pool, contract, RPC, domain). A blacklist hit is an immediate
veto (and the engine auto-blacklists a token confirmed to be a honeypot); a
whitelist hit adds a positive and can waive soft doubt. History is never deleted —
an entry is deactivated, never removed, so a past block can always be explained.

### Events, research & metrics

Emits `SecurityAnalysisStarted`, `SecurityScoreComputed`, `TokenApproved` /
`TokenRejected`, and the granular `ContractRiskDetected`, `LiquidityWarning`,
`ClusterFound`, `DeveloperRisk`. **Every** assessment is persisted — *including
rejections*, which are the richest training data the platform produces (Research
requirement). Prometheus surfaces tokens analysed/approved/rejected (by reason),
risk flags by code+severity, clusters detected, vetoes, per-analysis latency and
the running security/developer score averages.

### RPC-budget posture

The engine runs 24/7, so the RPC-heavy features are bounded and configurable:
live cluster funding lookups (`SECURITY_CLUSTER_LIVE_LOOKUPS`, tightly capped) and
the exact-holder-count scan (`SECURITY_FETCH_HOLDER_COUNT`, off by default). When a
feature is disabled or its budget is exhausted, the engine degrades to doubt — it
never pretends the missing data was clean.

### Wiring & persistence

Composed in `ops/security_runtime.py` and hosted by the Worker alongside the
Scanner; it subscribes to `FeaturesComputed` so no measured token is unscreened.
Four new tables (migration additive, all UUID pk + timestamps):
`security_assessments` (audit + research, approved **and** rejected),
`blacklist_entries` / `whitelist_entries` (append-only), `developer_reputation`
(accumulated track record), `wallet_clusters`. The read-only API surface is
`/api/v1/security/{status,token/{mint},rejections,lists}`.

### It never decides a trade

The Security Engine's entire output is a recommendation surface — an input to the
future AI Committee. It approves or rejects continued analysis; it never buys,
sells, or modifies a position.

---

## 6d. Wallet Intelligence — the on-chain knowledge base (Phase 5)

`contexts/intelligence/` is a **wallet-centric** bounded context, not a per-token
check: every wallet Hades ever observes gets a permanent identity whose history,
reputation, behaviour, funding lineage, relationships and influence accumulate for
years. **Nothing is ever deleted** — profiles gain versions, the timeline and the
knowledge base only grow. It only *knows*; it never trades, never enables live,
and runs **no ML**. Its output is a read model for the future AI Committee.

### Design: pure engines over an assembled batch

Like the Security Engine, all I/O lives in one infrastructure assembler; every
engine is a pure, deterministic function. The assembler reacts to the Security
Engine's `SecurityScoreComputed` (fired for every analysed token, approved **and**
rejected — exactly the population worth learning from), reads the deployer and top
holders straight from the assessment facts (no duplicate RPC), and — when
`live_lookups` is on — enriches each wallet with its funders. It hands a
`WalletObservationBatch` to the engine.

### The engines (`application/`)

- **Reputation** (`reputation.py`) — evolving, **non-binary** trust / risk /
  consistency / experience / profitability. Each observation nudges components
  toward an evidence-implied target via EWMA; experience is a ratchet; a confirmed
  scammer is a hard floor. New wallets start neutral (50), never a free pass.
- **Behaviour** (`behavior.py`) — classifies whale / sniper / high-frequency /
  holder / swing / retail with explicit confidence (honest "unknown" until enough
  is seen).
- **Smart / Dumb money** (`smart_money.py`) — *measures* predictive value from
  resolved history (early, avoids rugs, participates in survivors); stays neutral
  below a minimum resolved-history threshold. Never assumes.
- **Influence** (`influence.py`) — a 0–100 weight so one proven wallet outweighs a
  hundred fresh ones; eroded by risk, scaled by evidence.
- **Funding** (`funding.py`) — classifies each inbound edge (exchange / mixer /
  suspicious / wallet).
- **Clustering** (`clustering.py`) — collapses wallets sharing a funder into a
  single entity with a stable `cluster_id` and explicit confidence.
- **Scoring / Explainability** (`scoring.py`, `explainability.py`) — blends the
  eight components into a final banded Wallet Score and always returns the
  positive/negative reasons behind it — never a bare number.
- **Profiler + Engine** (`profiler.py`, `engine.py`) — the profiler folds one
  observation into the next immutable profile version; the engine orchestrates
  clustering, per-wallet profiling, append-only persistence and event emission,
  isolating each wallet so one failure never sinks the batch.

### Events, metrics & API

Emits `WalletRegistered`, `WalletUpdated`, `WalletProfileComputed`,
`ReputationUpdated`, `BehaviorChanged`, `SmartMoneyDetected`,
`FundingRelationshipFound`, `ClusterCreated` and a per-token
`WalletIntelligenceComputed` (the snapshot: smart/dumb money around the token,
avg trust/risk, clusters, funding footprint). Prometheus counters/histograms under
`hades_intel_*`. Read-only API: `/api/v1/intelligence/{status,wallet/{address},
wallet/{address}/timeline,wallet/{address}/relationships,clusters,smart-money}`.
The **Wallet Intel** dashboard screen consumes these: wallet lookup (score dial,
banded reputation bars, behaviour/influence/money-class, the +/- explanation,
funding lineage, timeline and graph edges), a smart-money leaderboard and recent
clusters.

### Wiring & persistence

Composed in `ops/intelligence_runtime.py`, hosted by the Worker alongside the
Scanner and Security Engine (`security → intelligence`). Six new tables (migration
`0003`, additive, all UUID pk + timestamps): `wallet_profiles` (current profile),
`wallet_history` (append-only version log), `wallet_timeline`,
`wallet_relationships` (the graph), `intel_clusters`, `wallet_knowledge`
(append-only ledger). All adapters ship Postgres + in-memory twins.

### It never decides a trade

The whole context is an information layer. It builds one of the platform's most
valuable long-lived assets — a wallet knowledge base — for the future AI Committee
to consult. It takes no decision, sizes nothing, and runs no model.

---

## 6e. AI Committee — the explainable brain (Phase 6)

The Learning context (`contexts/learning`) is Hades' quantitative brain. Its one
non-negotiable rule: **it is not a black box, and it never decides.** It produces
probabilities and evidence; the Risk Manager (a later phase) is the only thing
allowed to act. There is deliberately **no heavy ML** — every model is a
transparent linear-logistic scorer, so it runs anywhere (including the low-power
VPS) and every output decomposes into legible per-feature contributions.

### Flow

```
scanner → features → security → intelligence → COMMITTEE
     WalletIntelligenceComputed ─► [Decision Context Builder → Committee Manager]
        → InferenceCompleted (×12) → CommitteeFinished → ConfidenceCalculated
        → PredictionGenerated → CommitteePredictionGenerated
```

The committee reacts to `WalletIntelligenceComputed` (the last analytical stage,
fired for approved *and* rejected tokens). The **Decision Context Builder**
(`infrastructure/context_builder.py`) does all the I/O: it reads the latest vector
from the **Feature Store**, normalises it, joins the wallet-intelligence snapshot
(from the event) and the most recent security verdict, and hands the pure engine a
`DecisionContext`.

### Feature Store contract (`application/feature_catalog.py`)

Models are **never** fed raw PostgreSQL values. The `FeatureCatalog` is the
curated, versioned schema: every `FeatureSpec` carries a name, description,
version, unit, origin, the members that use it, and a documented normalisation
(z-score / min-max / clip / log-min-max / bool). The `FeatureNormalizer` turns a
raw `FeatureSet` into a model-ready `NormalizedVector` and records **coverage**
(missing inputs become neutral and *lower confidence*, never an error). Upstream
`security.*` / `intel.*` signals are injected as pre-normalised context features
(`committee/context_features.py`).

### The AI Committee (`application/committee/`)

Twelve **specialists**, one per facet: Liquidity, Momentum, Volatility, Market
Regime, Wallet Intelligence, Security, Holder Distribution, Developer, Behaviour,
Risk, Timing, Microstructure. Each is a `SpecialistModel` — a logistic scorer over
its own feature subset with **documented default weights** (so the committee is
meaningful before any training). It emits an `Opinion`: `probability`,
`confidence` (from feature coverage × signal decisiveness), and ranked `reasons`
(contributions centred on the neutral point so a low value of a negatively-weighted
feature reads correctly). A specialist with too little data **abstains** (neutral
0.5, near-zero confidence) rather than pretending. **No member ever decides.**

### Meta Model (`application/meta_model.py`)

The chair. It fuses the opinions into three calibrated probabilities —
`P(ROI positive)`, `P(hit TP)`, `P(hit SL)` — via three logistic heads (a
risk/security-heavy head for the stop, a momentum/wallet-heavy head for the
target). Each opinion enters *centred* on 0.5 and *scaled by its own confidence*,
so an unsure or abstaining member contributes nothing. It **never says "buy"** —
only probabilities plus the per-member echo for audit.

### Confidence, Regime & Explainability

- **Confidence Engine** (`confidence.py`): confidence is *not* the model output. It
  fuses dataset quality, sample support, feature coverage, specialist agreement
  (statistical dispersion), regime stability and a volatility penalty, and returns
  the whole `ConfidenceFactors` decomposition.
- **Market Regime** (`regime.py`): a soft classifier over bull / bear / sideways /
  highly-volatile / low-liquidity / panic / FOMO, with the full distribution and a
  peakedness-based confidence (a later Risk Manager may modulate its thresholds
  per regime).
- **Explainability** (`explainability.py`): every verdict ships a headline plus
  `drivers` (why the probability is high), `risks` (why not higher) and `caveats`
  (why to distrust the number — low coverage, thin data, unstable regime). **Never
  a bare percentage.**

### Model Registry, Versioning & Shadow (`registry.py`, `infrastructure/model_registry.py`)

Append-only and versioned: every training run appends a new immutable
`(name, version)` `ModelCard` (weights, features, dataset, metrics, status) —
**never overwritten**. Promotion is exclusive (one active version per name),
**human/policy-gated**, and archives the incumbent; a *rejected* model can never be
promoted. **Shadow models** run alongside the active committee for comparison —
persisted and flagged `shadow`, never influencing anything.

### Dataset Builder, Training & Validation

- **Dataset Builder** (`dataset_builder.py`) assembles a versioned `Dataset` from
  the append-only **Outcome ledger**, which holds executed trades *and* rejected
  opportunities — so the brain learns from what it declined and from losses, not
  only wins.
- **Training Engine** (`training.py`): a pure-Python gradient-descent logistic fit
  (L2) trains each specialist on its own features and the meta-model's three heads
  on the specialists' probabilities. Runs off the hot path; it only produces
  *candidates*.
- **Validation Engine** (`validation.py`): a candidate must survive **walk-forward**,
  **cross-validation**, an **out-of-sample** tail, a **paper-replay** (would its
  confidence have added ROI?) and a **comparison vs the incumbent**. Absolute
  quality gates (AUC / Brier / calibration) plus "must not be worse than incumbent"
  — **a worse model is never deployed.** Passing only makes it *eligible*.

### Knowledge Feedback, Model Monitor & Feature Importance

- **Knowledge Feedback** (`knowledge_feedback.py`): the write-path into the outcome
  ledger — records closed-trade labels and, reacting to `TokenRejected`, records
  security-rejected tokens as weak negatives.
- **Model Monitor** (`monitor.py`): watches an active model for **data drift**
  (input distribution shift), **feature drift** (a named feature moved) and
  **concept drift** (live metrics decayed vs validated), raising `ModelDriftDetected`.
- **Feature Importance** (`feature_importance.py`): continuously ranks features by
  contribution share and flags **useless / redundant / stale** features so
  complexity can be pruned at the next training run.

### Events, API, dashboard & persistence

Events: `InferenceCompleted`, `ConfidenceCalculated`, `CommitteeFinished`,
`PredictionGenerated`, `CommitteePredictionGenerated`, `ModelTrained`,
`ModelValidated`, `ModelRejected`, `ModelPromotionProposed`, `ModelPromoted`,
`ModelDriftDetected` — all on the registry so they cross the Redis bus. The
read-only API (`/api/v1/committee/*`) exposes status, models & versions,
predictions (with full explanation + opinions), feature importance, drift, and the
one human-gated `POST .../promote`. The **AI Committee dashboard screen**
(`frontend/src/pages/AIScreen.tsx`) shows the latest fused prediction, the model
registry, recent predictions (click for the full breakdown), model drift and
shadow/auto-train status. Persistence (migration `0004`): `committee_predictions`,
`committee_models`, `committee_datasets`, `committee_feature_importance`,
`committee_drift`, `committee_outcomes` — all append-only, Postgres + in-memory
twins. Prometheus metrics via `LearningMetrics`. Wired in `ops/committee_runtime.py`,
hosted by the `worker` service.

### It never decides a trade

The committee outputs probabilities, a confidence decomposition and an explanation
— evidence, not an instruction. It buys nothing, sells nothing, sizes nothing and
enables no live trading. Trade execution is **not** implemented in this phase.

---

## 6f. Risk Manager & Portfolio — the guardian of capital (Phase 7)

The single most important subsystem: the **Risk Manager** is the *only* component
authorised to approve or reject a trade. The AI Committee quantifies, the Scanner
discovers, the Security Engine screens — none may commit money. The design goal is
not to maximise profit but to **survive for years**: protect capital first, earn
second. Two contexts realise it — `contexts/portfolio` (the book) and
`contexts/risk` (the guardian).

### Flow

    committee ─CommitteePredictionGenerated─► [RiskContextBuilder → Risk Manager]
        ─► TradeApproved / TradeRejected  (never an executed order)
    positions ─PositionOpened/Updated/Closed─► [Portfolio Manager] ─► PortfolioUpdated

The Risk Manager sits at the very end of the analytical pipeline, subscribing to
the committee's `CommitteePredictionGenerated` (the only event a decision-maker
may act on). Shadow predictions are ignored.

### The Trade Approval chain (`contexts/risk/application/manager.py`)

Every candidate runs one ordered chain; any step may veto, approval needs all:

1. **Global gates** — Emergency Mode, Circuit Breaker, Kill Switch (blocks-entries).
2. **Quality policies** (`policies.py`) — min probability, min confidence, security
   approved + score, developer score, wallet risk, liquidity.
3. **Position Sizing** (`sizing.py`) — dynamic, conviction-weighted, kill-switch-scaled.
4. **Allocation policies** — max positions, capital-after-reserve, drawdown,
   exposure, correlation, risk budget, trade rate.
5. **APPROVE** (with sized envelope) or **REJECT** (with the one vetoing reason).

The result is a fully explainable `RiskAssessment` — drivers, blockers and caveats —
published and written to the append-only audit store (`risk_decisions`).

### The engines (`contexts/risk/application/`)

- **Position Sizing** (`sizing.py`) — never a fixed lot. Blends probability, edge,
  security, wallet, liquidity and regime into a `conviction ∈ [0,1]`, gates it by
  confidence, converts `risk_usd = equity × risk_per_trade% × conviction ×
  kill_switch_factor` into a notional via the stop distance, bounded by the hard
  per-trade cap and deployable cash. A stellar setup gets a large size, a marginal
  one a small size, anything below the minimum is not traded.
- **Exposure** (`exposure.py`) — aggregates the open book by token / developer /
  cluster / strategy / narrative / regime and caps each as a % of equity.
- **Correlation** (`correlation.py`) — counts positions sharing a developer /
  cluster / narrative; refuses to pile onto an already-concentrated theme.
- **Drawdown** (`drawdown.py`) — daily / weekly / monthly loss ceilings + a daily
  stop-loss count cap.
- **Risk Budget** (`risk_budget.py`) — each strategy gets a slice of equity it may
  not exceed, so none monopolises the book.
- **Capital Engine** — the Portfolio Manager holds back a configurable liquidity
  reserve; `available_usd` is always cash-after-reserve (never commit 100%).

### The defence layer (stateful, persisted)

- **Kill Switch** (`kill_switch.py`) — five graduated levels: `1 REDUCED` (3 losses
  → shrink size), `2 HALTED` (5 losses → stop entries for a cooldown),
  `3 OBSERVATION` (daily drawdown), `4 PAPER_ONLY` (critical drawdown),
  `5 EMERGENCY` (catastrophic → stop everything + notify). Deepest condition wins;
  a win resets the streak.
- **Circuit Breaker** (`circuit_breaker.py`) — refuses to trade under unsafe
  *conditions* (RPC instability, extreme latency, error runs, subsystem failures);
  auto-closes after a cooldown.
- **Emergency Mode** — blocks all entries, flags for close, notifies, audits.

The Kill Switch level, Circuit Breaker and Emergency flag are persisted
(`risk_control_state`) and restored on startup — a halt survives a restart.

### Portfolio Manager (`contexts/portfolio/application/portfolio_manager.py`)

The live book of record and the risk read model. Reacts to the Position stream to
maintain balance, equity, cash, invested capital, realised/unrealised PnL, fees,
drawdown, ROI and exposure in real time, and *is* the `PortfolioReadPort` the Risk
Manager evaluates against (capital-after-reserve, tagged open positions, rolling
drawdown, trade rate). **Portfolio Analytics** (`domain/analytics.py`) is pure,
dependency-free maths: Sharpe, Sortino, Calmar, Profit Factor, Expectancy,
Recovery Factor, Kelly Fraction and Risk of Ruin — every degenerate input guarded.

### Events, API, metrics & persistence

- **Events** — `TradeApproved` / `TradeRejected`, `RiskReduced`,
  `KillSwitchLevelChanged` / `KillSwitchEngaged` / `KillSwitchReset`,
  `CircuitBreakerTripped` / `CircuitBreakerReset`, `EmergencyModeEntered` /
  `EmergencyModeExited`, `DrawdownLimitBreached`, `ExposureLimitBreached`,
  `RiskControlCommandIssued` (operator actions); `PositionOpened/Updated/Closed`,
  `CapitalCommitted/Released`, `PortfolioUpdated`. All registered on the bus.
- **API** — read-only `/api/v1/risk/*` (status, kill-switch, circuit-breaker,
  drawdown, exposure, decisions) + human-gated controls (kill-switch/reset,
  circuit-breaker/reset+trip, emergency/enter+exit) that publish a
  `RiskControlCommandIssued` the Worker acts on; read-only `/api/v1/portfolio/*`
  (status, analytics, capital, equity-curve, history, pnl).
- **Metrics** — `hades_risk_*` (approvals/rejections by reason, kill-switch level,
  breaker open, emergency, review latency) + `hades_portfolio_*` gauges.
- **Persistence** — migration `0005` adds `risk_decisions` (audit) and
  `risk_control_state` (durable posture); reuses `portfolio_history`,
  `equity_curve`, `pnl_history`, `positions`.
- **Config** — the whole `RiskConfig` is assembled from `RISK_*` / `POSITION_*`
  env vars via `risk_config_from_settings`; conservative by default.

### It never executes a trade

A `TradeApproved` is a *permission slip with a size on it*. The Risk Manager
itself never swaps, signs or touches a wallet — it only approves and sizes. The
**Execution Engine (§6g)** is the sole component that turns an approval into a
real or simulated order.

---

## 6g. Execution Engine — turning approvals into fills (Phase 8)

The Execution Engine is where a `TradeApproved` becomes an order. Its defining
property is **total decoupling from the mode**: the AI never knows if it is paper
or live, the strategies never know, the Risk Manager never knows. **Only the
Execution Engine knows**, and it confines that knowledge to a single line —
`ExecutionEngine._executor_for(mode)` — so paper, live, replay and backtest all
run through the *identical* interface (`contexts/execution/domain/ports.py::Executor`).

### Flow (never broken)

    risk ─TradeApproved─► [Execution Engine → Mode resolve → Paper|Live executor]
        ─► OrderSubmitted → (execute) → OrderFilled / OrderFailed
        ─► TransactionManager.record  ─► PositionOpened / PositionClosed
        ─► [Portfolio Manager] ─► PortfolioUpdated  ─► Discord

Because a `FillReport` has the *same shape* whichever executor produced it,
everything downstream (Portfolio, Learning, Dashboard) is oblivious to the mode.

### The paper/live seam (`contexts/execution/application/`)

- **`PaperExecutor`** — a *faithful* simulation: it refuses ideal prices. It
  grounds the fill in a real reference price (via a `PriceOracle`, degrading to a
  unit price when none is wired), applies a **dynamically estimated** slippage
  against the trade direction, charges the **same fees** the live path would, and
  waits a simulated latency + confirmation. It never signs, never touches a wallet.
- **`LiveExecutor`** — independent, fail-closed: `quote → slippage guard → sign →
  send → confirm → report`. Any failure short-circuits to a *failed* fill (never
  optimistic); transient send/confirm errors are retried under the Retry Engine.
  Keys never leave the `TransactionSigner`. **Only built when the hard live gate
  is on AND a signer/quote/RPC adapter are all present** — a config file alone can
  never route a real order (see the factory).

### The sub-engines

- **Slippage Engine** (`slippage.py`) — dynamic, never fixed: scores liquidity,
  volatility, spread, size-vs-pool, venue and time-of-day into a recommended and a
  hard-max tolerance; missing inputs widen it. Cancels a trade over budget.
- **Fee Engine** (`fees.py`) — network + priority + DEX fee (Jito tip scaffolded);
  shared by paper and live so simulated PnL carries the real cost drag.
- **Confirmation Engine** (`confirmation.py`) — polls signature status to the
  configured commitment within a timeout, recording attempts/elapsed/RPC.
- **Retry Engine** (`retry.py`) — bounded exponential backoff with jitter; retries
  only flagged-transient errors; hard failures propagate; never infinite.
- **Wallet Manager** (`wallet_manager.py`) — wallet identity, SOL balance and
  health via RPC. Never touches, logs or returns key material.
- **Order Manager** (`order_manager.py`) — the full order lifecycle
  (`pending → submitted → confirming → filled | failed | cancelled`); trazabilidad
  never lost (terminal orders retained).
- **Transaction Manager** (`transaction_manager.py`) — the transaction trail
  (signature, slot, fees, confirmation time, RPC, attempts, errors).
- **Swap Manager** (`swap_manager.py`) — wraps the quote provider for the live
  path; rejects a quote whose price impact exceeds the order's slippage budget.
- **Mode Manager** — the guarded paper↔live switch from Phase 2
  (`trading_mode.py`): live needs the hard env gate + readiness (wallet, RPC, risk)
  + explicit confirmation; every change is persisted, audited and announced. The
  engine reads the effective mode from it, ANDed with the gate.

### Wiring, API, metrics

- **Runtime** — `ops/execution_runtime.py` assembles the engine, subscribes it to
  `TradeApproved`, and publishes a live status snapshot to Redis (`execution`
  namespace) for the dashboard. Hosted in the **Worker** (`EXECUTION_ENABLED`).
- **Events** — `OrderSubmitted`, `OrderFilled`, `OrderFailed` (+ the Position
  stream it feeds); registered on the bus.
- **API** — read-only `/api/v1/execution/*` (status, orders, transactions, wallet,
  metrics); the mode is changed only via the guarded `/api/v1/trading/mode`.
- **Metrics** — `hades_execution_*` (orders submitted/filled/failed by mode,
  slippage, fees, confirmation latency, retries).
- **Persistence** — reuses the existing `trades` / `paper_trades` / `live_trades`
  ledger tables (migration `0001`); runtime state is in-memory + Redis snapshots,
  mirrored best-effort through the `OrderStore` / `TransactionStore` ports.
- **Config** — `EXECUTION_*` (slippage budget, retry, fees, quote mint) +
  `PAPER_*`; conservative and paper-by-default.

### Paper is the default; live is never implicit

`PaperExecutor` is *always* built and is the safe default. The engine falls back
to paper whenever the mode can't be resolved, the mode names an executor that
isn't wired, or the live gate/adapters are absent. Real funds require the hard env
gate **and** the live adapters **and** an explicit, audited switch to live.

---

## 6h. Research Lab — evolving without risking capital (Phase 9)

The Research Lab is a fully **independent, offline R&D environment** whose single
responsibility is to *investigate*. It runs on **copies** of history, produces
**knowledge**, and can never place a live order, mutate a production strategy, or
deploy a model. Production makes money; Research makes knowledge — and the two
environments never mix. The separation is **structural**: nothing under
`contexts/research` imports `contexts/execution`, `contexts/risk` or
`contexts/portfolio`, and a test (`test_research_isolation.py`) statically
enforces that the lab can never even reference the trading contexts.

Every improvement must climb the same one-way ladder — **Investigation → Validation
→ Paper → Shadow → Manual approval → Production** — and the last step always
belongs to a human.

### Flow

    (copied history) ─► [Dataset Builder → split train/validation/forward]
        ─► [Experiment / Backtest / Walk-Forward / Monte-Carlo / Optimizer]
        ─► [Validation gauntlet]  ─► [Promotion Engine → recommendation]
        ─► StrategyPromoted (governance record only — deploys NOTHING)

    live FeaturesComputed ─► [Shadow strategies] ─► virtual trades (no capital)

### The engines (`contexts/research/application/`)

- **Research Manager** (`manager.py`) — the coordinator. Owns the engines +
  append-only stores, runs studies, records knowledge, publishes the lab's events
  and (sparse) Discord alerts. Pure compute + append-only I/O; every failure is
  swallowed so the lab **never blocks the trading system**. No Execution/Risk/
  Portfolio collaborator exists anywhere in its graph.
- **Experiment Engine** (`experiment_engine.py`) — turns *any* change (TP, SL,
  trailing, add/drop feature, swap model, tune a parameter) into a measured,
  reproducible `ExperimentResult`. Nothing is ever "just changed".
- **Backtesting Engine** (`backtest_engine.py`) — replays a strategy over a window
  **net of frictions** (slippage that widens on thin liquidity, DEX + priority
  fees, latency, depth). A strategy that only survives frictionless is discarded.
- **Walk-Forward Engine** (`walk_forward.py`) — rolling out-of-sample folds; reports
  aggregate OOS metrics + an *efficiency* (OOS/IS ratio) that exposes overfitting.
- **Monte-Carlo Engine** (`monte_carlo.py`) — thousands of seeded, perturbed paths
  (jittered slippage/latency, shuffled order, dropped fills); reports the outcome
  *distribution* and a composite robustness score.
- **Parameter Optimizer** (`optimizer.py`) — seeded multi-objective search. **Never
  optimises ROI alone** — the objective blends return, drawdown, Sharpe, Sortino,
  profit factor and expectancy (`metrics.multi_objective_score`).
- **Shadow Strategy Engine** (`shadow.py`) — strategies that behave exactly like
  real ones on the live stream, except **every trade is virtual**: no order, no
  capital, no Portfolio touch.
- **Candidate Strategies** (`strategies.py`) — the ten archetypes as pure rule
  *genomes* (Momentum, Mean Reversion, Liquidity Breakout, Smart Money Follow,
  Wallet Rotation, Launch Detection, News Driven, Social Momentum, Order Flow,
  Microstructure) — data, never executable trading code.
- **Feature Discovery** (`feature_discovery.py`) — ranks feature importance, flags
  redundant/irrelevant features and proposes interactions. It only ever
  **proposes** — it never removes a feature.
- **Comparators** (`comparators.py`) — Strategy Comparator (ranks on the full
  scorecard) + Model Comparator (production/candidate/shadow deltas + verdict).
- **Validation Engine** (`validation.py`) — the one-way gauntlet
  (`Training → Validation → Forward-Test → Paper-Replay → Shadow`); a candidate
  advances one stage at a time and **never skips**.
- **Promotion Engine** (`promotion.py`) — **fail-closed, human-gated**. Checks a
  configurable bar (trades, Sharpe, drawdown, profit factor, expectancy, paper-
  and shadow-positive) *and* requires an explicit `manual_approve` the lab can
  never set itself. Even an approved decision **deploys nothing**.
- **Knowledge Base** (`knowledge_base.py`) — permanent, append-only memory of
  experiments, results, hypotheses, errors, conclusions, comparisons.
- **Replay / Dataset Builder / Auto Scheduler / Report Generator** — reconstruct a
  historical window through shadows; split copies chronologically; decide what
  recurring work is due; roll activity into daily/weekly/monthly stored reports.

### Wiring, API, metrics & persistence

- **Runtime** — `ops/research_runtime.py` assembles the lab, subscribes shadow
  strategies to the live `FeaturesComputed` stream, runs the (optional) auto-research
  loop, and publishes a Redis status snapshot (`research` namespace). Hosted in the
  **Worker** under `RESEARCH_LAB_ENABLED` (**off by default**). The historical
  reader projects the AI Committee's labelled outcome ledger (`committee_outcomes`)
  into research samples — read-only; the lab reads a *copy* and writes nothing back.
- **Events** — `ExperimentStarted/Finished`, `BacktestCompleted`,
  `WalkForwardCompleted`, `MonteCarloCompleted`, `ReplayCompleted`,
  `ShadowStrategyUpdated`, `ModelCompared`, `StrategyCompared`, `FeatureProposed`,
  `CandidateProposed`, `StrategyPromoted`, `PromotionRejected`,
  `ResearchReportGenerated` — knowledge facts, never trade instructions.
- **API** — read-only `/api/v1/research/*` (status, experiments, backtests,
  candidates, shadows, rankings, promotions, knowledge, hypotheses, reports). The
  one write endpoint, `/candidates/{id}/promote`, is fail-closed (requires
  `approve=true`) and only records a governance decision — it activates nothing.
- **Persistence** — migration `0006` adds the lab's eight append-only tables
  (`research_experiments`, `research_backtests`, `research_candidates`,
  `research_shadows`, `research_promotions`, `research_reports`, `research_knowledge`,
  `research_hypotheses`); history is never deleted.
- **Config** — `RESEARCH_*` (`lab_enabled`, `auto_research`, shadow + report
  intervals, promotion bar); disabled and conservative by default.

### It never touches production

The lab has no path to the Execution Engine — by construction, not by convention.
It proposes and recommends; every promotion to production is a separate, manual,
out-of-lab action. Hades can evolve continuously, but never at the cost of the
capital or the stability of the running system.

---

## 6i. Strategy Engine — the modular set of quantitative strategies (Phase 10)

`contexts/strategy` is where Hades turns the AI Committee's explainable verdict
into *opportunities*. It hosts every trading strategy as an independent,
hot-swappable **plugin** behind one interface, runs the whole roster over each
token, and fuses their signals into a single **weighted ensemble** — never a
simple vote. It sits between the committee and the Risk Manager and, like every
other context, **only detects: it never executes, sizes, modifies a position or
bypasses the Risk Manager.**

### Flow

    committee ─CommitteePredictionGenerated─► [MarketContextBuilder → StrategyEngine]
        ─► per-strategy SignalGenerated / SignalRejected
        ─► EnsembleSignalGenerated  (the fused, weighted opinion — evidence, not an order)

Self-evaluation closes off the Position stream: a strategy attributed to a
position (via the `strategy` tag) is credited/debited when it opens and later
closes, so weights are tapered by *realised* results.

### The Strategy interface (`domain/ports.py`, `application/base.py`)

Every strategy implements the same seven-method lifecycle — `initialize`,
`load_configuration`, `validate_market`, `generate_signal`, `calculate_confidence`,
`calculate_reasoning`, `shutdown` — and exposes a full `StrategyMetadata` spec
sheet (name, version, description, status, priority, supported markets, ideal /
forbidden regimes, features used, expected holding time, historical drawdown /
Sharpe / profit factor / win rate / expectancy, lifecycle stage). `BaseStrategy`
supplies all the boilerplate so a concrete plugin is tiny: it declares its
metadata and implements one pure `_evaluate(context) → Evaluation`. Strategies are
**pure and I/O-free** — the interface exposes no database and no Execution Engine,
so those couplings are structurally impossible.

### The 15 shipped strategies (`application/strategies/`)

Each lives in its own module and subclasses `BaseStrategy`; adding a new one is a
one-file change plus one registry line — the engine, ensemble and weighting never
change. Shipped: Momentum Breakout, Liquidity Expansion, Smart Money Follow, Whale
Tracking, Launch Detection, Volume Expansion, Market Microstructure, Mean
Reversion, Volatility Compression, Liquidity Rotation, Narrative Momentum,
Developer Reputation, Wallet Rotation, Order Flow Imbalance and Cross Signal
Confirmation. Each reads the committee's per-facet member scores (momentum,
liquidity, wallet, security, developer, microstructure…), declares the regimes it
thrives in and the regimes it must never trade, and returns a
`BUY`/`SELL`/`EXIT`/`IGNORE` with an internal score, a confidence and a full
`SignalExplanation` (why it fired, which variables moved it, which conditions
favour it, which risks it saw). **A signal is never a bare number and never an
order.**

### Ensemble, weighting & self-evaluation (`ensemble.py`, `weighting.py`, `evaluation.py`)

- **DynamicWeightEngine** — every strategy's weight is a base prior (priority)
  scaled by legible factors: regime fit, recent Sharpe, recent Profit Factor,
  drawdown, consistency, sample size, the AI's confidence and any Research-Lab
  boost. It is floored above zero — a weak or degrading strategy is **muted, never
  removed** — and every factor is retained for the dashboard.
- **EnsembleBuilder** — each signal contributes a *signed, weighted pull*
  (`weight · confidence · score · direction`); the net conviction is normalised
  into [-1, 1], the decision is the side past a small deadband, and confidence
  blends conviction magnitude with the agreement among participants. **Shadow-stage
  strategies are recorded as contributions but excluded from the production
  decision.**
- **SelfEvaluator** — pure statistics over realised outcomes: win rate, profit
  factor, Sharpe, Sortino, recovery, average win/loss, holding time, expectancy,
  and a `degraded` flag that the weighting engine reads to taper weight.

### Failsafe, lifecycle & configuration

- **Failsafe** — one strategy raising is caught, counted and (past a threshold)
  the strategy is muted (`StrategyError` → `StrategyDisabled`); the engine
  continues with the rest. One broken plugin can never stop Hades.
- **ShadowLifecycle** — new strategies climb Research → Backtest → Replay → Paper
  → Shadow → Production one rung at a time; skips are rejected and everything below
  Production runs in shadow.
- **Configuration** (`STRATEGY_*` / `StrategySettings`) — enable/disable
  strategies, set the global sensitivity, pass per-strategy parameters
  (`STRATEGY_PARAMS` JSON), tune the ensemble deadband, weight floor, failsafe
  threshold and notify threshold — **all from config, never by editing code.**

### Events, API, metrics & wiring

- **Events**: `StrategyLoaded`, `StrategyDisabled`, `StrategyError`,
  `ShadowActivated`, `StrategyPromoted`, `SignalGenerated`, `SignalRejected`,
  `WeightUpdated`, and the headline `EnsembleSignalGenerated`.
- **API** (`/api/v1/strategies/*`): `status`, list, `ranking`, `weights`,
  `performance`, `shadow`, `signals`, `ensembles` — read-only, off the Redis status
  snapshot the runtime publishes.
- **Metrics**: signals / rejections / errors / ensembles counters, evaluate-latency
  and ensemble-confidence histograms.
- **Wiring**: `ops/strategy_runtime.py` hosts the engine in the Worker under
  `settings.strategy.enabled`; `StrategyHandler` subscribes to
  `CommitteePredictionGenerated` (+ the Position stream for self-evaluation).

### It never executes a trade

The engine's output is the `EnsembleSignal` — *evidence* the Risk Manager may
consume. The `gate_risk` flag is a forward-looking seam: it defaults **off**, so
the existing committee→risk path is unchanged and strategies remain advisory until
explicitly promoted, exactly like every other capability in Hades.

---

## 6j. Production Hardening — integration, stability & deployment (Phase 11, Stage 1)

The business architecture was complete; this phase turns those modules into a
platform that can *run for months unattended*. Stage 1 delivers the backend core
of that: audit, configuration-as-an-asset, performance visibility, self-healing
and a hard pre-LIVE gate. It **reuses the existing schema** (`audit_log`,
`system_configuration` already shipped in the baseline) and adds only one table
(`config_snapshots`, migration `0007`). Nothing here enables live trading — it
only adds observability, recoverability and *more* brakes.

### Audit System (`contexts/audit/`)

A cross-cutting, append-only trail of consequential actions
(`who / what / when / before / after`) over the existing `audit_log` table.

- **Ports/domain**: `AuditTrail` (record port), `AuditEntry` / `AuditQuery` value
  objects, `AuditStore` (Postgres + in-memory adapters).
- **`AuditRecorder`** — records entries and, by design, **never raises** to the
  caller (a failed audit must not break the audited action).
- **`AuditSubscriber`** — the non-invasive half: subscribes to events the platform
  *already* publishes (model/strategy promotions & rejections, weight changes,
  kill-switch / circuit-breaker / emergency transitions, risk-control commands)
  and records each generically from the event envelope. It deliberately skips
  `TradingModeChanged` (audited at its source) to avoid double-recording.
- **API**: `GET /api/v1/audit` (filter by actor/action/entity/time). Hosted in the
  Worker via `ops/audit_runtime.py`.

### Configuration Manager (`ops/config_manager.py`)

Treats configuration as a versioned, auditable asset without ever mutating
env-driven `Settings` at runtime.

- **`export()`** — full non-secret posture; every secret-looking leaf is
  redacted by `redact(...)` *before* it can be checksummed, snapshotted or served.
- **Versioning** — `snapshot()` appends to `config_snapshots` (SHA-256 checksum);
  `list_versions` / `get_version`; `diff_versions(a,b)` gives a dotted-path diff;
  `detect_drift()` compares the live posture to the latest snapshot.
- **`import_config()`** — validates shape, records the diff, snapshots the import
  and applies only *allow-listed runtime keys* to `system_configuration`; the
  reserved `trading_mode` / `emergency_mode` keys are **refused** (guarded owners).
- **API**: `GET /config/export|snapshots|snapshots/{v}|diff|drift`,
  `POST /config/snapshots|import`.

### Performance Monitor (`contexts/monitoring/application/performance_monitor.py`)

Latency + throughput, exported to Prometheus and a self-contained snapshot.

- **Latency** of four stages (analysis / decision / execution / confirmation) via
  `observe_*`, each feeding a Prometheus histogram and a bounded `LatencyStat`
  (mean / p50 / p95 / max) — with `shared_kernel/observability/timing.py`
  (`Stopwatch`, `measure`, `LatencyStat`, `RollingRate`) any context can adopt.
- **Throughput** (tokens/min, feature-computations/sec, predictions/min,
  operations/hour) derived **entirely from existing events** — zero hot-path
  intrusion. Hosted by `ops/performance_runtime.py`; published to Redis and served
  at `GET /api/v1/metrics/performance`.

### Auto-Recovery + Emergency Mode (`contexts/monitoring/application/recovery.py`)

The Watchdog no longer only alerts: on an unhealthy component it asks a
`RecoveryOrchestrator` to run bounded, injected `RecoveryAction`s (Redis
reconnect, Postgres pool reset, config reload; an RPC-failover action where an
RPC Manager is in reach). Each component gets a capped number of attempts; a
success resets the counter and notifies recovery. **Exhausting the attempts
escalates to Emergency Mode** — persisted in `system_configuration`, published as
a `RiskControlCommandIssued(enter_emergency)` the Risk Manager already obeys, and
shouted on Discord as `CRITICAL`. Monitoring never reaches into risk directly.

### Deployment Validator + Production Checklist

- **`ops/preflight.py`** — `DeploymentValidator` checks config, connectivity
  (Postgres/Redis/RPC/API/dashboard) and that the schema is migrated to head;
  produces a `PreflightReport` and posts a Discord summary. The `hades-preflight`
  entrypoint runs it once and **exits non-zero** on any required failure (gates a
  deploy / `docker compose up`).
- **`ProductionChecklist`** aggregates the readiness of every subsystem (Risk,
  Wallet, RPC, Health, DB, Redis, Scanner, Security, AI, Execution, Notification,
  Dashboard, Watchdog, Backups, Docs) **plus Emergency-Mode-inactive**. It
  implements a small `ProductionChecklistPort` (defined in execution) that
  `TradingModeService.verify_live_readiness` folds into its live-readiness gate:
  **any required failure — or an active Emergency Mode — blocks the switch to
  LIVE**, fail-closed. Served at `GET /health/preflight` and
  `GET /health/production-checklist`.

### Uniform Discord embeds (`contexts/notification/infrastructure/embed_builder.py`)

One professional look for every alert: a category (INFO/WARNING/ERROR/CRITICAL/
SUCCESS) drives the colour (overridable via a `category` tag, so a fill can be
green though `Severity` has no `SUCCESS`), a fixed ordering of the known
operational fields (mode, token, pnl, roi, wallet, security score, AI confidence,
strategy, exec time, host, container, timestamp) rendered **only when present**,
and a uniform footer. `DiscordNotifier` delegates to it — fully backward
compatible.

### It never enables live

Every capability here is an *observer* or a *brake*: it records, measures,
reconnects, or **withholds** LIVE approval. The paper/live seam is untouched and
live remains hard-gated; Stage 1 only makes the platform more auditable, more
recoverable and harder to send live by accident.

*Not yet in this stage (later stages): the Chaos/Load/Stress test harness,
multi-wallet/node scalability scaffolding, the 24/7 maintenance jobs, the final
dashboard screens, and full generated technical docs.*

---

## 6k. Technical Audit (2026-07-22)

A full adversarial audit was run before considering any LIVE operation. The complete,
evidence-cited report lives in [`docs/TECHNICAL_AUDIT.md`](docs/TECHNICAL_AUDIT.md); this
is the reconciled summary. Every claim below was traced to source or produced by a
reproducible command — nothing was accepted from prior documentation on faith.

### Architecture Review
Bounded contexts, ports-and-adapters, CQRS buses and event-driven decoupling are all real
and clean. No circular context dependencies; the research→execution direction is
**AST-blocked** by a test. Largest file is 755 lines — no god-objects. **Correction to the
record:** the platform is **event-driven with persisted read-models**, *not* durably
event-sourced — the `EventStore` is `InMemoryEventStore()` in the composition root
(`bootstrap.py:308`), and the execution order/txn ledger + open-position map are also
in-memory. Read-model repositories (intelligence, learning, risk/portfolio history, audit,
config) *do* persist to Postgres and are always selected in production.

### Security Review
No hardcoded secrets; wallet key is a mounted secret with a per-tx SOL cap; no private key,
signature or serialized tx is ever logged; CORS is a fixed origin, not `*`. Gaps: **API auth
is OFF by default** and the paper→live switch endpoint is unauthenticated; **WebSocket
endpoints have no auth**; containers run as root; Prometheus/Grafana pinned to `:latest`.
A non-constant-time API-key comparison was **fixed** in this pass.

### Money-safety invariants — VERIFIED
Single `TradeApproved` publisher (Risk Manager, `manager.py:305`); execution reacts only to
that event; live executor is built only behind the hard gate **and** with all live
collaborators present (paper is mandatory); mode resolution and the Risk Manager both fail
**safe/closed**; the wallet layer never touches key material; the Research Lab is
structurally isolated from execution/risk/portfolio.

### Performance Review
Static only — load, chaos, and CPU/RAM profiling were **NOT EXECUTED** (they need the live
Docker stack) and no numbers are claimed. The identified risk is unbounded growth of the
in-memory event store and scanner caches under sustained load; the sequential in-memory bus
fan-out is the next latency suspect (mitigated by the Redis transport, already supported).

### Production Readiness
Baseline is genuinely healthy: **376/376 tests pass, mypy strict clean on all 407 files,
ruff clean but for 9 cosmetic `UP046` nits.** LIVE is **structurally impossible today** (no
live adapters) and must stay disabled until the LIVE-gating items close: durable execution
ledger, persisted open positions + corrected realized-PnL, durable event store, API auth on
+ enforced on the mode switch, WebSocket auth, and built-and-audited live adapters. Full
component classification (READY / NEEDS IMPROVEMENT / CRITICAL-for-LIVE) is in the report.

### Known Issues
5 HIGH (all LIVE-gating: H1 event store, H2 execution ledger, H3 open-position PnL, H4 API
auth, H5 WS auth), 6 MEDIUM (M1 constant-time compare **fixed**, M2 Postgres degradation,
M3 broad catches, M4 Docker hardening, M5 dead code **fixed**, M6 PnL fee accounting), 4 LOW
(toolchain drift, `UP046`, test deprecation warning, `pyproject` version lag).

### Fixes applied in this pass
1. `api/security.py` — constant-time API-key comparison via `hmac.compare_digest` (M1).
2. `execution/application/engine.py` — `execute()` now dispatches through
   `_executor_for(mode)`, removing dead code and making the mode-confinement docstring true
   while exercising the defensive paper fallback (M5). Verified: 111 targeted tests pass,
   mypy clean.

### Recommendations / Future Improvements
The prioritized roadmap (High/Medium/Low + quick wins + future risks) is in §9 of the
report. Headline: **do not enable LIVE** until every CRITICAL-for-LIVE row is closed *and*
the load + resilience suites (§11–12 of the report) have been executed against a real stack.

---

## 6l. Final Hardening (Phase 11, Stage 2 — 2026-07-23)

The closing pass over the whole project. **No new business capability was added** —
the goal was to eliminate the technical debt the audit registered, tighten quality to
the highest bar, and make the deployment turnkey. Every change was validated against the
full gate (`ruff` + `mypy --strict` + `pytest`). The closing report lives in
[`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md).

**Code quality (now 0 lint findings; suite runs warnings-as-errors).**
- **CQRS `CommandHandler` / `QueryHandler`** migrated to PEP-695 native generics (`UP046`).
- **Notification `NotificationRequested.tags` / `Notification.tags`** now use
  `Field(default_factory=dict)` instead of a shared mutable default (`RUF012`).
- Ambiguous Unicode en-dashes in domain docstrings normalized (`RUF002/003`).
- `pytest` now runs with `filterwarnings = ["error", …]` — any warning our own code emits
  fails the build; the one third-party deprecation we don't control (Starlette TestClient's
  httpx import) is explicitly allow-listed (L3).
- `pyproject` version synced to the documented release `0.10.0` (L4).
- **Runtime version drift closed (L4, completion — 2026-07-23).** Stage 2 synced
  `pyproject` but the *runtime* string in `hades/__init__.py` was still the stale `0.3.0`,
  so **every** API surface (`/api/v1/version`, `/api/v1/status`, `/api/v1/info`, the WS
  handshake and OpenAPI) advertised the wrong version while the package was `0.10.0`.
  `hades.__version__` is now the **single source of truth** and `pyproject` *derives* its
  version from it via `[tool.setuptools.dynamic]` (`version = {attr = "hades.__version__"}`),
  so the packaged metadata, the runtime and the docs can no longer diverge — bump one place.
  *Alternatives rejected:* hardcoding `0.10.0` a second time in `__init__.py` (re-creates the
  drift) and reading `importlib.metadata` at runtime (fails when the package is run from source,
  as the test suite does). *Validated:* `setuptools` resolves `0.10.0` from the attr and a
  Docker-context-simulated `pip install .` produces the correct metadata; regression
  `test_all_meta_surfaces_report_the_single_source_of_truth_version` pins all three HTTP
  surfaces to `__version__`. Suite now **379** tests.

**Money-safety / correctness.**
- **Realized-PnL accounting fixed (M6).** On close, PnL is now net of **both** round-trip
  frictions — the buy-side fee (captured at open in `_OpenPosition.entry_fees_usd`) *and*
  the sell-side fee — not just the sell fee. The single-position-per-mint / full-close
  assumption is now documented at the call site. Locked in by a new test
  (`test_realized_pnl_is_net_of_both_round_trip_fees`).

**Security / defence-in-depth.**
- **Go-LIVE now requires a real operator (partial H4).** The `POST /api/v1/trading/mode`
  endpoint refuses a switch **to LIVE** from the implicit unauthenticated `system` principal
  with `403`, *independently* of the env gate, readiness checks and explicit confirmation it
  already enforced. Read and paper operations are unaffected (the dashboard keeps working
  with global auth off). New test: `test_switch_to_live_is_rejected_for_the_implicit_system_principal`.
- **Container hardening (M4).** All app + dashboard services run with `cap_drop: [ALL]` and
  `security_opt: [no-new-privileges:true]` (the image was already non-root, uid 1000); the
  data stores get `no-new-privileges`; Prometheus/Grafana are **pinned** (no `:latest`) and
  their admin UIs bind to `127.0.0.1` only.

**Deployment (turnkey `up -d`).**
- A one-shot **`migrate`** service (`alembic upgrade head`) now runs before any app service
  (gated via `service_completed_successfully`), so `git clone → configure .env →
  docker compose up -d` brings the schema to head automatically — no manual `make migrate`.

**Consciously deferred (documented, not silently dropped).** The remaining LIVE-gating
items are large, are *not* blockers for the paper-only posture, and — critically — cannot be
*validated* without a live Postgres/Redis/RPC stack, so shipping them unvalidated would
*lower* quality, not raise it. They are the headline of Hades v2 / pre-LIVE:
- **H1** durable (Postgres) event store; **H2** durable order/transaction ledger;
  **H3** persisted open-position map (rebuilt from the portfolio read-model on boot);
- **H5** WebSocket authentication (coordinated server + dashboard change);
- **M2** Postgres runtime-degradation strategy (readiness gating + DB circuit breaker);
- building and independently auditing the live signer / quote / RPC adapters.

---

## 7. Testing

`backend/tests` (379 tests, all green; `mypy --strict` clean; `ruff` clean; suite runs
warnings-as-errors):
- Phase 1: `test_value_objects`, `test_event_infrastructure`, `test_position_aggregate`,
  `test_api_health` (**asserts a fresh instance is never live**).
- Phase 2: `test_config` (all sections load), `test_persistence_schema` (26 tables,
  every one has UUID pk + timestamps, FK integrity), `test_notification`
  (event-driven delivery + severity gating), `test_trading_mode` (live needs
  confirmation; readiness logic), `test_events_registry` (transport round-trip),
  `test_api_v2` (status/version/info/config/trading + terminal WS; config never
  leaks secrets), `test_liveness` (heartbeat files).
- Phase 3: `test_rpc_manager` (failover, health, rate-limit, switch callback),
  `test_scanner_discovery` (dedup, gating, source-health events),
  `test_scanner_pipeline` (Quality Validator rules, Metadata Collector merge,
  pipeline end-to-end + backpressure), `test_scanner_sources` (each DEX parser),
  `test_feature_engine` (indicators, extractor blocks, compose/store/emit),
  `test_scanner_schema` (Phase-3 tables + FKs), `test_api_scanner` (status shape,
  **no risk info**).
- Phase 4: `test_security_analyzers` (each analyzer + Gini/HHI math),
  `test_security_scoring` (weighted blend, critical veto, doubt rule, rug-pull
  composite, whitelist waives doubt not veto), `test_security_engine` (end-to-end:
  approve/reject, honeypot auto-blacklist, blacklist pre-veto, rejected verdicts
  persisted for research, events emitted, developer reputation accumulates),
  `test_security_lists` (append-only black/white lists, fail-closed lookups),
  `test_security_schema` (Phase-4 tables + FKs), `test_api_security` (status/token/
  rejections/lists shape + graceful degradation).
- Phase 5: `test_intelligence_units` (reputation evolution + scammer floor +
  experience ratchet, behaviour classification, smart/dumb money thresholds,
  influence scaling, funding classification, deterministic clustering, scoring
  bands), `test_intelligence_engine` (end-to-end: registers wallets, builds a
  common-funder cluster, emits events, **never deletes history — versions grow**,
  isolates invalid addresses), `test_intelligence_assembler` (deployer/holders from
  security facts, scammer marking, graceful degradation with live lookups off),
  `test_intelligence_schema` (Phase-5 tables), `test_api_intelligence` (endpoint
  shapes + graceful degradation).
- Phase 6: `test_committee_units` (sigmoid/AUC/Brier/calibration math, feature
  normalisation + coverage, all 12 specialists present, abstention, explainable
  opinions, regime classification, three-probability fusion, multi-factor
  confidence, "never a bare number"), `test_committee_engine` (end-to-end:
  full prediction, **all events emitted**, persistence, shadow runs without
  emitting the headline event, strong ≻ weak token, low coverage lowers
  confidence, never crashes on thin input), `test_committee_training` (logistic
  trainer learns signal, trains the whole committee + meta heads, skips small
  datasets, full validation gauntlet + gates, **rejects worse-than-incumbent**,
  registry register/promote/**never-overwrite**/**exclusive promotion**/
  **rejected-can't-promote**, drift detection data/feature/concept, feature
  importance flags useless, dataset builder **includes rejected**),
  `test_committee_integration` (knowledge feedback from outcomes + security
  rejections, subscriber runs the committee on the intelligence event, factory
  reconstruction from cards + default fallback, model-card serializer round-trip,
  Phase-6 tables registered).
- Phase 7: `test_portfolio_analytics` (Sharpe/Sortino/Calmar/Profit-Factor/
  Expectancy/Kelly/Risk-of-Ruin + every degenerate input guarded),
  `test_risk_engines` (conviction-scaled sizing, kill-switch factor + capital
  bounds, exposure/correlation/drawdown/risk-budget caps), `test_kill_switch`
  (five graduated levels, deepest condition wins, streak reset, disabled paths;
  circuit-breaker error-streak/latency/manual), `test_risk_manager` (the full
  approval chain end-to-end — **approve + size**, each reject reason, emergency /
  circuit-breaker / kill-switch gates, **events emitted**, streak persists),
  `test_portfolio_manager` (state from the Position stream, capital-after-reserve,
  tags, consecutive losses), `test_risk_schema` (Phase-7 tables + config mapping),
  `test_api_risk_portfolio` (endpoint shapes + graceful degradation).
- Phase 8: `test_execution_slippage_fees` (dynamic slippage widens on thin
  liquidity / large size / illiquid venue / off-peak and is hard-capped; fee
  components sum, DEX fee scales with notional, Jito tip only when enabled),
  `test_execution_paper` (a paper fill is filled *with costs*, BUY/SELL slippage
  moves the price against the taker, unit-price fallback, never a live signature),
  `test_execution_retry` (bounded attempts, exponential backoff cap, retries only
  transient errors, hard failures propagate), `test_execution_engine` (the whole
  flow: a filled BUY opens a position carrying the approval's attribution, a
  `TradeApproved` flows through, a failed order emits failure + no position, a SELL
  closes the same position id, **live mode without a live executor falls back to
  paper**, live is used only when present *and* selected), `test_execution_factory`
  (**paper always built, live never by default, live not built even with the gate
  when adapters are missing, built only with gate + all adapters**).
- Phase 9: `test_research_units` (metrics scorecard + multi-objective penalises
  drawdown, genome scoring bounded, **backtest applies frictions**, walk-forward
  windows, **Monte-Carlo deterministic + bounded**, optimiser stays in-space,
  **shadow trades are zero-capital**, feature discovery only *proposes*, comparators
  rank, **promotion fails closed without a human / approved with one / rejects weak**,
  validation runs the stages in order + never skips, scheduler due-after-interval),
  `test_research_integration` (manager emits `ExperimentFinished`/`BacktestCompleted`/
  `FeatureProposed`/`ResearchReportGenerated`, **promotion needs a human even when
  every metric passes**, strategy comparison ranks the population, replay drives
  shadows without orders), `test_research_isolation` (**statically asserts the lab
  imports no Execution/Risk/Portfolio module**, and a `StrategyPromoted` payload
  carries no order/size/mode field).
- Phase 10: `test_strategy_units` (15 unique strategies each expose full metadata;
  momentum buys on strength / ignores when weak; a forbidden regime blocks the
  signal; weight is dynamic by regime and floored above zero; the ensemble is
  **weighted, not a vote**, excludes shadow strategies and ignores on no consensus;
  self-evaluation math; degradation tapers but never removes weight; one-step
  lifecycle promotion rejects skips; the context builder maps committee opinions),
  `test_strategy_engine` (initialize announces strategies + shadows, the **failsafe
  isolates a broken strategy** while the rest still signal and an ensemble is still
  emitted, shadow strategies are recorded but excluded, repeated errors mute a
  strategy without removing it, self-evaluation attributes Position-stream
  outcomes), `test_api_strategy` (endpoint shapes; **no order/executed-trade info**;
  render without state).
- Phase 11 (Production Hardening, Stage 1): `test_audit_system` (recorder writes;
  **a failing store never propagates**; the subscriber records a published event;
  query filtering), `test_config_manager` (redaction blanks secret leaves; export
  never leaks; snapshot→version→get; diff & drift; **reserved keys refused on
  import**; invalid payload rejected; order-independent checksum),
  `test_performance_monitor` (`LatencyStat`/`RollingRate`/`measure` helpers;
  latency stages; throughput counters; `register` subscribes the four throughput
  events), `test_recovery` (Emergency Mode publishes the risk command and is
  idempotent; the orchestrator recovers via the first working action and
  **escalates to Emergency after max attempts**; a raising action counts as a
  failure), `test_production_checklist` (**an infra failure or an active Emergency
  Mode blocks LIVE**; readiness tuples carry the required flag),
  `test_discord_embed_builder` (category colours; only-present fields; footer +
  timestamp). `test_persistence_schema` now covers `config_snapshots`.

Everything is testable because every dependency is a port; in-memory adapters
back the tests, real adapters back production. The frontend passes `tsc` and a
production `vite build`.

---

## 8. Dependencies

Core: FastAPI, Uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2 (async) +
Alembic + asyncpg, Redis, httpx, structlog, prometheus-client, websockets,
orjson, tenacity, psutil (Phase 2, watchdog). Frontend: React 18, react-router,
Vite, Tailwind.

Opt-in extras (declared, used in later phases): `ml` (scikit-learn, LightGBM,
XGBoost, numpy, pandas), `analytics` (clickhouse-connect), `dev` (pytest, ruff,
mypy).

---

## 9. Pending / next phases (not yet built)

**Production Hardening — remaining stages** (Stage 1 is built, see §6j). Still
ahead for the "run 24/7 for months" goal: the **Chaos/Load/Stress test harness**
(inject RPC/Redis/Postgres/latency/WebSocket faults and assert the platform keeps
serving), **scalability scaffolding** (multi-wallet/account/node abstractions),
the **24/7 maintenance jobs** (log rotation is configured; cache-cleanup and
memory-optimisation jobs plug into the Scheduler), the **final dashboard screens**
(Health/Config/Audit/Terminal/Metrics over WebSocket — backend + API are ready),
and **generated technical docs**.

Earlier pending items remain relevant:

- **Postgres-backed event store + repositories**: the ORM schema exists; the
  next step wires the SQLAlchemy `UnitOfWork` and event store to replace the
  in-memory fallbacks used by the tests.
- **Market Engine**: real price/liquidity/volume snapshots + a live price feed,
  which enrich `FeatureInputs` (today the Feature Engine seeds from the discovery
  liquidity hint; the technical/holder/pool blocks light up once market data and
  the holder graph feed them) and drive periodic minute-by-minute snapshots.
- **Live executor adapters** (Phase 8 built the Execution Engine — §6g — with a
  fully functional paper executor and a *complete* live executor, but the live path
  is only wired when its three adapters exist): a real **`TransactionSigner`**
  (keypair from a mounted secret), a **`QuoteProvider`** (Jupiter/Raydium swap
  quote + build), and the RPC gateway (already available). Until then the engine is
  paper-only regardless of the gate — fail-safe by construction.
- **Durable order/transaction ledger**: the engine persists best-effort through the
  `OrderStore` / `TransactionStore` ports (in-memory today); a Postgres binding to
  the existing `trades` / `paper_trades` / `live_trades` tables is the next step.
- **Research Lab**; the Market Engine (live price/liquidity feed that lights up the
  remaining feature blocks and drives mark-to-market for open positions).
- **Committee training data**: the outcome ledger fills once the executor closes
  trades and feeds realised labels back through Knowledge Feedback, at which point
  scheduled training can propose calibrated models to replace the documented
  default weights.
- **Dashboard live data**: positions/equity, risk controls, research jobs (the
  screen structure is already in place).

## 10. Future improvements

- Split contexts into separate deployable services once load demands it (the
  event-bus + narrow-port design already permits this with no caller changes).
- OpenTelemetry tracing across the event pipeline (`TRACING_ENABLED`).
- Model registry with staged rollout / shadow evaluation before promotion.
- ClickHouse-backed analytical projections + Grafana dashboards.
- Formal architecture-fitness tests (import-linter) to enforce the dependency
  rule automatically in CI.

---

## Changelog of this document

- **2026-07-23** — **Phase 11, Stage 2 — Final Hardening** (§6l; closing report in
  `docs/PRODUCTION_READINESS.md`). Closing pass over the whole project; **no new
  business capability**. Closed audit findings: **M6** realized-PnL now net of both
  round-trip fees (+test); **M4** container hardening (`cap_drop: ALL`,
  `no-new-privileges`, pinned+localhost-bound Prometheus/Grafana); **partial H4** the
  switch to LIVE now requires an authenticated operator (+test); **L1–L4** all lint
  findings cleared (PEP-695 generics, `Field(default_factory=dict)`, Unicode dashes,
  version sync) and the suite now runs **warnings-as-errors**. Deployment made turnkey:
  a one-shot **`migrate`** service brings the schema to head before any app service, so
  `docker compose up -d` needs no manual migration step. Full gate green: **379 tests**,
  `mypy --strict` clean (407 files), `ruff` clean. LIVE-gating durability items
  (H1/H2/H3/H5/M2 + live adapters) consciously deferred to Hades v2 — they cannot be
  *validated* without a live stack, and shipping them unvalidated would lower quality.
- **2026-07-22** — **Technical Audit** run before any LIVE consideration (§6k, full
  report in `docs/TECHNICAL_AUDIT.md`). Baseline reproduced: 376/376 tests pass, mypy
  strict clean on 407 files. Money-safety invariants verified at source. Findings: 5 HIGH
  (all LIVE-gating — in-memory event store, unpersisted execution ledger, in-memory
  open-position PnL, API auth off by default, no WebSocket auth), 6 MEDIUM, 4 LOW. Two
  fixes applied (constant-time API-key comparison; execution mode dispatch through
  `_executor_for`). LIVE remains structurally disabled and must stay so until the
  LIVE-gating items close and load/resilience suites run against a real stack.
- **2026-07-22** — **Phase 10 (Strategy Engine / the modular set of quantitative
  strategies)** documented (§6i): `contexts/strategy` hosts every strategy as an
  independent, hot-swappable **plugin** behind one seven-method interface, runs the
  whole roster over each committee verdict and fuses their signals into a single
  **weighted ensemble — never a simple vote**. Fifteen strategies ship (Momentum
  Breakout, Liquidity Expansion, Smart Money Follow, Whale Tracking, Launch
  Detection, Volume Expansion, Market Microstructure, Mean Reversion, Volatility
  Compression, Liquidity Rotation, Narrative Momentum, Developer Reputation, Wallet
  Rotation, Order Flow Imbalance, Cross Signal Confirmation), each pure/I/O-free,
  regime-aware and fully explainable (`BUY`/`SELL`/`EXIT`/`IGNORE` with drivers,
  influencing variables, favourable conditions and risks). **DynamicWeightEngine**
  weights each strategy by regime fit + recent Sharpe/PF + drawdown + consistency +
  sample + AI confidence + Research boost, floored above zero (**muted, never
  removed**); **SelfEvaluator** scores realised outcomes and flags degradation;
  **ShadowLifecycle** climbs Research→Backtest→Replay→Paper→Shadow→Production one
  rung at a time (shadow strategies recorded but excluded from the decision); a
  **failsafe** mutes a raising strategy without stopping the rest. Events
  (`StrategyLoaded`/`Disabled`/`Error`, `ShadowActivated`, `StrategyPromoted`,
  `SignalGenerated`/`Rejected`, `WeightUpdated`, `EnsembleSignalGenerated`),
  `/api/v1/strategies/*` read API + metrics, `ops/strategy_runtime.py` in the Worker
  under `STRATEGY_*`, wired between committee and risk. **It never executes, sizes,
  modifies a position or bypasses the Risk Manager** — the ensemble is evidence; the
  `gate_risk` flag defaults **off** so the existing committee→risk path is unchanged.
  +22 tests (347 total), `mypy --strict` + `ruff` clean.
- **2026-07-22** — **Phase 9 (Research Lab / evolving without risking capital)**
  documented (§6h): `contexts/research` becomes a fully independent, offline R&D
  environment that runs on **copies** of history and produces **knowledge only** —
  it can never place a live order, mutate a production strategy, or deploy a model.
  The isolation is **structural** (nothing imports Execution/Risk/Portfolio, and a
  test statically enforces it). Engines: **Research Manager** (coordinator, never
  blocks production), **Experiment Engine** (every change becomes a measured
  experiment), **Backtesting Engine** (net of frictions), **Walk-Forward** (OOS
  efficiency), **Monte-Carlo** (seeded robustness), **Parameter Optimizer**
  (multi-objective — never ROI alone), **Shadow strategies** (live-shaped but
  virtual, zero-capital), the **ten candidate archetypes** as rule genomes,
  **Feature Discovery** (only proposes), **Model/Strategy Comparators**, the
  **Validation gauntlet** (Training→Validation→Forward→Paper→Shadow, never skipped),
  a **fail-closed, human-gated Promotion Engine** (approval the lab can never grant
  itself; even approved it deploys nothing), an append-only **Knowledge Base**,
  Replay/Dataset-Builder/Auto-Scheduler/Report-Generator. 14 knowledge events,
  read-only `/api/v1/research/*` (+ a fail-closed promote endpoint), migration
  `0006` (8 append-only tables), `RESEARCH_*` config (off by default), wired in
  `ops/research_runtime.py` and hosted in the Worker; the historical reader projects
  the committee's labelled outcomes as the lab's dataset (read-only). +29 tests
  (325 total), `mypy --strict` + `ruff` clean. `v0.9.0`.
- **2026-07-22** — **Phase 8 (Execution Engine / paper & live executors, fully
  decoupled)** documented (§6g): `contexts/execution` gains the engine that turns a
  `TradeApproved` into an order. **Total mode decoupling** — only the engine knows
  paper vs live, confined to one line; paper/live/replay/backtest share the
  identical `Executor` interface. A faithful **`PaperExecutor`** (real reference
  price, dynamic slippage against the taker, real fees, simulated latency/
  confirmation — never ideal prices, never a wallet) and an independent,
  fail-closed **`LiveExecutor`** (`quote → slippage guard → sign → send → confirm`,
  retried, keys never leaving the signer). Sub-engines: dynamic **Slippage Engine**,
  **Fee Engine** (net+priority+DEX, Jito scaffolded), **Confirmation Engine**,
  bounded **Retry Engine**, **Wallet Manager** (no secrets), **Order Manager**
  (full lifecycle), **Transaction Manager**, **Swap Manager**. The **factory builds
  paper always and live only with the hard gate + all adapters** (a config file can
  never route real orders); the engine falls back to paper whenever the mode can't
  be resolved. Reacts to `TradeApproved`, emits `OrderSubmitted/Filled/Failed` and
  feeds the Position stream (`PositionOpened/Closed`) that closes the Portfolio
  loop; read-only `/api/v1/execution/*`, `hades_execution_*` metrics, wired in
  `ops/execution_runtime.py` and hosted in the Worker; reuses the `trades` ledger
  tables. **Paper is the default; live is never implicit.** +27 tests (296 total),
  `mypy --strict` + `ruff` clean. `v0.8.0`.
- **2026-07-21** — **Phase 7 (Risk Manager & Portfolio / the guardian of capital)**
  documented (§6f): two new contexts — `contexts/portfolio` (the live book of
  record + pure Portfolio Analytics: Sharpe / Sortino / Calmar / Profit Factor /
  Expectancy / Recovery Factor / Kelly / Risk of Ruin) and `contexts/risk` (the
  guardian — the only component that may approve a trade). A composable Trade
  Approval chain (global gates → quality policies → dynamic conviction-weighted
  Position Sizing → allocation policies) yields a fully explainable
  `RiskAssessment`; a five-level graduated **Kill Switch**, a **Circuit Breaker**
  and **Emergency Mode** form a persisted defence layer that survives restarts;
  Exposure / Correlation / Drawdown / Risk-Budget engines cap concentration; the
  Capital Engine always holds a liquidity reserve (never commit 100%). Reacts to
  `CommitteePredictionGenerated`, emits `TradeApproved` / `TradeRejected` +
  defence-layer events, read-only `/api/v1/risk/*` + `/api/v1/portfolio/*` with
  human-gated controls, `hades_risk_*` / `hades_portfolio_*` metrics, two tables
  (migration `0005`: `risk_decisions` audit + `risk_control_state`). **It approves
  but never executes** — no Execution Engine, swap or wallet exists. +68 tests
  (269 total), `mypy --strict` + `ruff` clean.
- **2026-07-21** — **Phase 6 (AI Committee / the explainable brain)** documented
  (§6e): new `contexts/learning` — a Feature Store contract (normalised/versioned/
  documented features), twelve transparent single-purpose specialist models each
  emitting a probability + confidence + reasons, a Meta Model fusing them into
  `P(ROI+)` / `P(TP)` / `P(SL)` + confidence, a multi-factor Confidence Engine, a
  soft Market-Regime classifier, an Explainability Engine (drivers / risks /
  caveats — never a bare %), an append-only versioned Model Registry with
  human-gated promotion, Shadow models, a pure-Python Training Engine, a full
  Validation gauntlet (walk-forward / cross-val / OOS / paper-replay / incumbent
  comparison — a worse model is never deployed), a Dataset Builder that learns
  from executed *and* rejected opportunities, Knowledge Feedback, continuous
  Feature Importance, and a Model Monitor (data / feature / concept drift). Eleven
  events + `hades_committee_*` / `hades_models_*` metrics + read-only
  `/api/v1/committee/*` (with one human-gated promote); six tables (migration
  `0004`); wired in `ops/committee_runtime.py`, hosted in the Worker; plus the
  **AI Committee** dashboard screen. Reacts to `WalletIntelligenceComputed`. No
  heavy ML (transparent logistic models, pure Python). **Only quantifies — never
  buys, sells or sizes; no trade execution.** 201 backend tests, mypy strict +
  ruff clean; frontend `tsc` + vite build clean. `v0.6.0`.
- **2026-07-21** — **Phase 5 (Wallet Intelligence / on-chain knowledge base)**
  documented (§6d): new wallet-centric `contexts/intelligence` — Registry,
  Profiler, evolving non-binary Reputation, Behaviour, Smart/Dumb-money, Influence,
  Funding, Cluster Builder, Scoring + Explainability, append-only Timeline &
  Knowledge Base, Relationship graph; reacts to `SecurityScoreComputed`, reuses the
  assessment facts (no duplicate RPC) with optional live funder enrichment; nine
  events + `hades_intel_*` metrics + read-only `/api/v1/intelligence/*`; six tables
  (migration `0003`), hosted in the Worker; plus the **Wallet Intel** dashboard
  screen (wallet lookup, reputation bars, explanation, timeline, relationships,
  smart-money leaderboard, clusters). Never trades, never enables live, runs no
  ML. 158 backend tests, mypy strict + ruff clean; frontend `tsc` + vite build
  clean. `v0.5.0`.
- **2026-07-21** — **Phase 4 (Security Engine)** documented (§6c): pure analyzers
  over an assembled context, conservative veto, scoring + rug-pull composite,
  explainability, append-only black/white lists, developer reputation, research
  persistence of rejected tokens. Reacts to `FeaturesComputed`; never trades.
- **2026-07-20** — **Phase 3 (Scanner / data acquisition)** documented (§6b): the
  multi-provider health-scored RPC Manager with auto-failover; the Discovery
  Engine + six independent DEX adapters; the Metadata Collector; the Feature
  Engine (hundreds of versioned features across six blocks) + feature store; the
  History Builder / snapshot system; the Quality Validator; the back-pressured
  Acquisition Pipeline; scanner events, metrics and the live Scanner dashboard
  screen; Phase-3 tables (`token_metadata`, `data_anomalies`, `token_snapshots`,
  migration `0002`). Runs in the `worker`, takes no decision, never trades. 80
  tests, mypy strict + ruff clean. `v0.3.0`.
- **2026-07-20** — **Phase 2 (Platform infrastructure)** documented: full Docker
  service set (api, dashboard, engine, watchdog, scheduler, worker, notification)
  on three isolated networks + persistent volumes; complete 26-table PostgreSQL
  schema + baseline migration; Redis primitives + Redis Streams event bus;
  ClickHouse integration; centralised logging (rotation + terminal ring buffer);
  Watchdog / Health Monitor; automatic backups + restore; Discord-only
  Notification Service (event-driven); API base + WebSocket + guarded paper↔live
  switch; React dashboard shell (11 screens). 36 tests, mypy strict + ruff clean.
  `v0.2.0`.
- **2026-07-20** — Phase 1 (Foundations) documented: architecture, shared kernel,
  13 bounded contexts + published language, contracts/events, Docker stack,
  FastAPI skeleton, frontend shell, tests. `v0.1.0`.
