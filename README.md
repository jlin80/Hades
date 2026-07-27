# Hades

> **Professional quantitative platform for Solana, initially focused on meme coins.**
> Modular, decoupled, event-driven, explainable, and built to run 24/7 for years.

Hades continuously discovers, screens, scores, decides on, and (in paper or live mode)
executes trades on Solana tokens — while treating **capital preservation, explainability
and safety as first-class constraints**, not afterthoughts. Every decision is traceable to
the evidence that produced it, and live trading is impossible unless it is explicitly,
verifiably enabled.

- **Full technical reference:** [`hades.md`](hades.md) — the living documentation (§0–§10).
- **Production-readiness & closing report:** [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md).
- **Architecture deep-dive:** [`docs/architecture.md`](docs/architecture.md).
- **Adversarial audit:** [`docs/TECHNICAL_AUDIT.md`](docs/TECHNICAL_AUDIT.md).
- **Operating guide:** [`docs/OPERATING.md`](docs/OPERATING.md) — which process runs
  what (the pipeline is in the **worker**, not the engine), how to see trades live,
  and how to debug "the bot isn't doing anything".
- **External Research Lab bridge:** [`docs/RESEARCH_LAB_BRIDGE.md`](docs/RESEARCH_LAB_BRIDGE.md)
  — how a candidate from the separate Hades Research Lab repository is imported
  (human-gated, never promoted on import).

> **Status:** `v0.10.0` · Phase 11 (Production Hardening). **Paper trading only** — live
> execution is hard-gated OFF and structurally impossible today (no live adapters are wired).
> Backend: **379 tests green**, `mypy --strict` clean (407 files), `ruff` clean.

---

## Table of contents

1. [Description](#1-description)
2. [Architecture](#2-architecture)
3. [Technologies](#3-technologies)
4. [Requirements](#4-requirements)
5. [Installation](#5-installation)
6. [Configuration & environment variables](#6-configuration--environment-variables)
7. [Docker Compose](#7-docker-compose)
8. [Initialization](#8-initialization)
9. [Dashboard](#9-dashboard)
10. [Paper trading](#10-paper-trading)
11. [Live mode](#11-live-mode)
12. [Research Lab](#12-research-lab)
13. [Backups](#13-backups)
14. [Logs & observability](#14-logs--observability)
15. [Updates & migrations](#15-updates--migrations)
16. [Troubleshooting](#16-troubleshooting)
17. [Best practices](#17-best-practices)
18. [License](#18-license)

---

## 1. Description

Hades is a **modular monolith** of independent bounded contexts that cooperate **only
through domain events**. Data flows one way through a pipeline that never lets a downstream
concern leak upstream:

```
Scanner → Feature Store → Security Engine → Wallet Intelligence → AI Committee
        → Strategy Engine → Risk Manager → Execution Engine → Portfolio
```

Design commitments that shape every module:

- **The Scoring / AI layer produces probabilities and confidence — never decisions.**
  Only the **Risk Manager** may authorise a trade (a single, audited code path).
- **Explainability over black boxes.** Every score, signal and decision carries its
  drivers, risks and caveats. The AI Committee is twelve transparent logistic specialists,
  not an opaque model.
- **Capital over profit; safety over speed.** Layers of brakes (kill switch, circuit
  breaker, emergency mode, production checklist) can only ever *withhold* action.
- **Paper and live share the exact same brain.** Only the Execution Engine knows the mode,
  and it confines that knowledge to a single line. Live is gated behind two independent
  switches, a full readiness check, an explicit confirmation, and an authenticated operator.
- **Research can never touch production.** The Research Lab runs offline on copies and is
  *structurally* forbidden (AST-enforced by a test) from importing execution/risk/portfolio.

## 2. Architecture

- **Clean Architecture + DDD** — each context is split `domain / application /
  infrastructure`; the domain has no framework or I/O dependency.
- **Ports & adapters** — every store and external service is a `Protocol` port with an
  in-memory adapter (tests) and a Postgres/HTTP adapter (production).
- **CQRS** — separate command and query buses.
- **Event-driven** — an `EventBus` port with in-memory (single process) and **Redis
  Streams** (multi-service) transports; handlers are idempotent (at-least-once delivery).
- **Shared kernel** — value objects, base aggregates, the event/CQRS machinery, config,
  logging, observability and persistence helpers, reused by every context.

The layout maps cleanly to Kubernetes later (one Deployment per service; the three Docker
networks become network policies). See [`docs/architecture.md`](docs/architecture.md) for
the full component, event and dependency maps.

**Bounded contexts** (`backend/src/hades/contexts/`): `scanner`, `features`, `security`,
`wallet`, `intelligence`, `market`, `scoring`, `learning` (AI Committee), `strategy`,
`risk`, `portfolio`, `execution`, `research`, `notification`, `monitoring`, `audit`,
`common`.

**Runtime services** (`docker-compose.yml`): `api`, `engine`, `worker`, `scheduler`,
`notification`, `watchdog`, `dashboard`, plus a one-shot `migrate` job and the `postgres`
/ `redis` stores. Optional profiles add `clickhouse` (analytics) and `prometheus` /
`grafana` (observability).

## 3. Technologies

| Layer | Choice |
|---|---|
| Language / runtime | Python 3.12 (backend), TypeScript + React + Vite + Tailwind (dashboard) |
| API | FastAPI + Uvicorn (REST + WebSocket) |
| Data model / ORM | SQLAlchemy 2 (async) + Alembic migrations |
| Datastore | PostgreSQL 16 (read-models, audit, config, history) |
| Cache / bus / queues | Redis 7 (cache, Redis Streams event bus) |
| Validation / config | Pydantic v2 + pydantic-settings |
| Logging | structlog (structured JSON) |
| Metrics | prometheus-client, Prometheus + Grafana (optional profile) |
| Analytics (optional) | ClickHouse |
| Packaging / deploy | Docker + Docker Compose (Linux only) |
| Quality gates | pytest, mypy `--strict`, ruff |

The core image is intentionally lean; heavy ML/analytics deps are opt-in extras. The AI
Committee and Research Lab are **pure Python** — no heavy ML runtime is required to operate.

## 4. Requirements

- **Linux host** with **Docker Engine** and the **Docker Compose v2** plugin. Everything
  runs in containers — nothing is installed on the host.
- ~2 vCPU / 4 GB RAM is comfortable for the paper stack; more for the optional
  analytics/observability profiles.
- A **Solana RPC endpoint** (a public one works for paper; a dedicated provider is
  recommended before live).
- Optional: a **Discord webhook** for notifications.

> The backend is Linux/Docker-only by design. You can run the test suite and type/lint
> gates on any OS with Python 3.12+, but the platform is operated exclusively via Compose.

## 5. Installation

```bash
git clone <your-fork-or-remote> hades
cd hades
make init      # creates .env from .env.example
#   → edit .env and set your secrets (see §6)
make up        # docker compose up -d  (auto-migrates, then starts every service)
```

That is the entire happy path: **clone → configure `.env` → `docker compose up -d` → open
the dashboard → start paper**. The schema is applied automatically by the one-shot
`migrate` service before any app service starts — there is no separate manual migration
step.

Useful `make` targets (all run through Compose): `up`, `up-all`, `down`, `logs`,
`logs-service s=<name>`, `ps`, `build`, `migrate`, `backup`, `test`, `lint`, `shell`.

## 6. Configuration & environment variables

All configuration and secrets live in **`.env`** (never committed). Start from
[`.env.example`](.env.example), which documents every key with safe defaults. The most
important groups:

| Group | Keys (examples) | Notes |
|---|---|---|
| **Trading mode** | `HADES_TRADING_MODE`, `HADES_LIVE_TRADING_ENABLED` | Both must be set for live — see §11. Default: paper, gate off. |
| **Database** | `POSTGRES_DB/USER/PASSWORD` | Change the password before any real deployment. |
| **Redis** | `REDIS_*`, `EVENT_BUS_TRANSPORT` | `redis` transport for multi-service; `memory` for single-process. |
| **Solana** | `SOLANA_RPC_HTTP_URL`, `SOLANA_RPC_WS_URL` | Your RPC provider(s); the RPC Manager health-scores and fails over. |
| **Wallet** | `WALLET_KEYPAIR_PATH`, `WALLET_PUBLIC_KEY`, `WALLET_MAX_SOL_PER_TX` | Keypair is a **mounted secret**, never an env value; per-tx SOL cap. |
| **API auth** | `API_AUTH_ENABLED`, `API_AUTH_API_KEY` | Off by default (safe on localhost); enable before exposing the API. |
| **Risk** | `RISK_MAX_POSITION_SIZE_USD`, `RISK_MAX_CONCURRENT_POSITIONS`, `RISK_KILL_SWITCH_ENABLED`, … | Hard limits; must be positive for live readiness. |
| **Notifications** | `NOTIFY_DISCORD_WEBHOOK_URL` | Optional; the only delivery channel, event-driven. |
| **Observability** | `GRAFANA_ADMIN_PASSWORD` | Only used with the `observability` profile. |

> **Secrets are never logged and never leave the box in a URL.** The wallet private key
> exists only inside the signer adapter, loaded from a mounted file — application code
> never sees key material.

## 7. Docker Compose

- **Production posture** (default `docker-compose.yml`): data-store ports are **not**
  published; app services run non-root with all Linux capabilities dropped and
  `no-new-privileges`; observability admin UIs bind to `127.0.0.1` only.

  ```bash
  docker compose -f docker-compose.yml up -d          # exactly the production stack
  ```

- **Local development** (`docker-compose.override.yml`, auto-loaded by plain
  `docker compose up`): mounts source for hot-reload, runs the API with `--reload`, and
  publishes Postgres/Redis ports for local tooling.

- **Optional profiles:**

  ```bash
  docker compose --profile analytics --profile observability up -d   # or: make up-all
  ```

  `analytics` adds ClickHouse; `observability` adds Prometheus (`127.0.0.1:9090`) and
  Grafana (`127.0.0.1:3001`).

## 8. Initialization

`docker compose up -d` runs, in order:

1. **`postgres` / `redis`** start and become healthy.
2. **`migrate`** (one-shot) runs `alembic upgrade head` and exits successfully.
3. **App services** (`api`, `engine`, `worker`, `scheduler`, `notification`, `watchdog`)
   start only after `migrate` completes, and **`dashboard`** after `api`.

To validate a deployment explicitly at any time:

```bash
docker compose run --rm api hades-preflight     # non-zero exit on any required failure
```

The preflight validates configuration, connectivity (Postgres/Redis/RPC/API/dashboard) and
that the schema is at head, and posts a Discord summary.

## 9. Dashboard

- **URL:** http://localhost:5173
- Read-only control center: system status, scanner feed, security screens, wallet
  intelligence, AI Committee explanations, strategy signals, risk/portfolio, execution,
  research, audit and configuration.
- API + interactive docs: http://localhost:8000/docs · health: `/health` · status:
  `/api/v1/status` · metrics: `/metrics`.

## 10. Paper trading

Paper is the **default and the safe fallback everywhere**. The paper executor faithfully
simulates quotes, slippage, fees and confirmation, and drives the exact same
Portfolio/PnL pipeline as live would. To confirm the posture:

```bash
curl -s http://localhost:8000/api/v1/trading/mode        # {"mode":"paper","is_live":false,...}
```

Realized PnL is computed **net of both round-trip frictions** (buy-side and sell-side fees).

## 11. Live mode

**Live trading is disabled and, today, structurally impossible** — the live signer / quote
/ RPC adapters are intentionally not wired, so no configuration can route a real order.
Even once adapters exist, enabling live requires **all** of:

1. the hard env gate `HADES_LIVE_TRADING_ENABLED=true` **and** `HADES_TRADING_MODE=live`;
2. every required **readiness check** to pass (wallet, RPC, risk limits, and the full
   Production Checklist — any failure or an active Emergency Mode refuses the switch);
3. an **explicit confirmation** on the mode-change request;
4. an **authenticated operator** — a switch to LIVE is refused for the implicit `system`
   principal, independently of the above.

See [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md) for the full list of
items that must close before live is considered.

## 12. Research Lab

An **offline** R&D environment that works on **copies** of history and produces
**knowledge only** — it can never place an order, mutate a production strategy or deploy a
model. Isolation is structural (a test AST-parses every research file and fails the build if
it imports execution/risk/portfolio). It ships backtesting (net of frictions), walk-forward,
Monte Carlo, a multi-objective optimizer, shadow evaluation, a validation gauntlet and a
**fail-closed, human-gated** promotion path. Disabled by default (`RESEARCH_LAB_ENABLED`);
served under `/api/v1/research/*`.

## 13. Backups

```bash
make backup        # DB + config + models + research + docs → a timestamped archive
```

The `scheduler` service also runs periodic backups. Backups land in the `hades_backups`
volume. Restore tooling lives alongside the backup manager (`hades.ops.backups`).

## 14. Logs & observability

- **Structured logs** (JSON via structlog) aggregate in the `hades_logs` volume; tail with
  `make logs` or `make logs-service s=<service>`. Compose caps log files (10 MB × 5).
- **Metrics** at `/metrics`; the optional `observability` profile ships Prometheus + a
  pre-provisioned Grafana dashboard.
- **Notifications** (optional) go to Discord with uniform, categorized embeds.

## 15. Updates & migrations

```bash
git pull
make build          # rebuild images
make up             # restart; the migrate job brings the schema to head automatically
```

New migrations are authored with `make revision m="describe the change"` and reviewed
before commit. Migrations are append-only and forward-only in normal operation.

## 16. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Services restart-loop after `up` | Check `.env` (DB password, RPC URL). Run `docker compose run --rm api hades-preflight`. |
| `migrate` fails | Postgres not healthy yet, or a bad DB credential in `.env`; inspect `make logs-service s=migrate`. |
| API returns 403 on mode change | Expected: a switch **to LIVE** requires an authenticated operator (`API_AUTH_ENABLED` + `X-API-Key`). |
| Dashboard can't reach API | Confirm `api` is healthy and `VITE_API_BASE_URL` matches your `API_PORT`. |
| No Discord alerts | `NOTIFY_DISCORD_WEBHOOK_URL` unset — notifications degrade silently by design. |
| RPC errors under load | Add more `SOLANA_RPC_HTTP_URL` providers; the RPC Manager health-scores and fails over. |

## 17. Best practices

- **Never commit `.env`** or any keypair. Change every default password before a real
  deployment.
- **Keep API auth on** and the admin UIs on localhost (SSH-tunnel to reach Grafana).
- **Stay on paper** until the pre-live checklist in the readiness report is fully closed and
  load/resilience suites have run against a real stack.
- **Prefer the Redis event-bus transport** in multi-service deployments; keep handlers
  idempotent (delivery is at-least-once).
- **Run the gates before every change:** `make lint` and `make test` (locally:
  `ruff check src && mypy src && pytest`).

## 18. License

Proprietary — all rights reserved. Not for redistribution without the owner's permission.
