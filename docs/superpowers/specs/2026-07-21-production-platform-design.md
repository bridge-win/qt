# QT Production Platform Design

**Date:** 2026-07-21
**Status:** Approved
**Target:** Single-operator production trading platform with tenant-ready boundaries

## 1. Objective

Upgrade QT from a local research, paper-trading, and dashboard application into
a production-grade operator platform without replacing its tested quantitative
domain core. The design adopts the strongest operational patterns from
QuantDinger: durable commands, strict process ownership, renewable leases,
idempotent side effects, explicit state machines, worker heartbeats, and staged
deployment.

The platform remains intentionally single-operator. It will have one
administrative identity and one logical trading account boundary. Database rows
will carry an `owner_id` where future tenancy would otherwise require a breaking
schema change, but this release does not include billing, public registration,
social features, or customer account administration.

## 2. Architectural Decision

QT remains the source of truth for:

- market data adapters and normalized data;
- indicators, signals, and strategy implementations;
- risk evaluation and capital limits;
- backtesting and benchmark reporting;
- paper and live broker behavior;
- portfolio and reconciliation rules.

A new platform layer owns orchestration:

```text
Operator Web / CLI
        |
Authenticated FastAPI control API
        |
PostgreSQL command, audit, job, and runtime state
        |
Trading Worker ---- Scheduler Worker ---- Job Worker
        |                   |                   |
QT strategy/risk      data/monitoring     backtests/reports
        |
PaperBroker / LiveBroker
        |
Exchange reconciliation
```

This is an evolutionary modular-monolith design. The API and workers ship from
one Python package and one container image, but run as independent processes.
That keeps deployment understandable while enforcing ownership boundaries that
can later be split into services if measured load requires it.

## 3. Non-Negotiable Invariants

1. HTTP processes never start strategy, scheduler, or exchange polling loops.
2. Every command that can cause an external side effect has an idempotency key.
3. Workers claim persisted work before calling an exchange or provider.
4. One renewable lease and fencing token owns each long-running runtime.
5. A stale worker cannot mutate state after another worker takes ownership.
6. Live orders use deterministic client order IDs and reconcile on ambiguous
   responses before retrying.
7. Risk limits are enforced in the execution path, not only in the UI or config.
8. The database is authoritative for control state; exchange reconciliation is
   authoritative for balances, orders, and fills.
9. Paper mode is the default. Live mode remains explicitly gated and fail-closed.
10. Every operator mutation produces an immutable audit event.

## 4. Components

### 4.1 Platform Database

PostgreSQL is required in production. SQLAlchemy 2 provides typed persistence
and Alembic owns forward-only schema migrations. SQLite may be used only for
fast unit tests and local read-only development paths; concurrency acceptance
tests run against PostgreSQL.

Initial tables:

- `platform_commands`: lifecycle and reconciliation commands;
- `runtime_leases`: renewable strategy and singleton-worker ownership;
- `worker_heartbeats`: process role, instance, status, version, and freshness;
- `audit_events`: append-only operator and system actions;
- `order_intents`: idempotent pre-exchange order records;
- `jobs`: finite backtest, report, and data-sync work;
- `outbox_events`: transactionally committed queue publications.

All mutable tables include UTC timestamps and an integer version used for
optimistic concurrency or fencing. Identifiers are UUIDs. JSON payloads are
schema-versioned and validated before persistence. Alembic's version table is
the authoritative migration compatibility marker.

### 4.2 Control API

FastAPI provides a typed OpenAPI contract and integrates naturally with QT's
Pydantic models. The API owns authentication, authorization, validation,
command submission, query endpoints, health endpoints, and event streaming.
It never imports or invokes a long-running strategy runner.

Required endpoint groups:

- `/api/health/live`, `/api/health/ready`, `/api/health/workers`;
- `/api/v1/session` for the single operator;
- `/api/v1/strategies` and lifecycle commands;
- `/api/v1/commands` and audit history;
- `/api/v1/portfolios`, positions, orders, fills, and reconciliation status;
- `/api/v1/jobs` for backtests, reports, and data synchronization;
- `/api/v1/events` for server-sent operational updates.

Mutation endpoints require an idempotency key and return the existing resource
when the same request is replayed.

### 4.3 Trading Worker

The trading worker is the only process allowed to own strategy runtimes and
broker sessions. It claims lifecycle commands, acquires a strategy lease,
starts or stops the existing QT runner, renews ownership, writes heartbeats,
and performs startup and periodic reconciliation.

Each runtime transition follows an explicit state machine:

```text
stopped -> starting -> running -> stopping -> stopped
                     -> degraded -> recovering
                     -> failed
```

An expired lease can be taken over by a healthy worker. The new fencing token
invalidates writes from the previous owner.

### 4.4 Scheduler Worker

The scheduler owns recurring finite dispatch, not trading runtimes. It creates
idempotent jobs for market-data synchronization, health checks, reconciliation,
monthly walk-forward validation, benchmark reports, and retention cleanup.
Leader ownership uses the same renewable lease model.

### 4.5 Job Worker

Redis and Celery own bounded, serializable jobs such as backtests, reports, and
data synchronization. Trading loops, broker sessions, and exchange order state
must never run in Celery. The job Redis uses persistence and `noeviction`; it is
separate from any disposable cache policy.

### 4.6 Operator Console

The existing server-rendered dashboard remains available during migration. It
will move from direct filesystem reads to the control API one surface at a time.
The production console must expose strategy controls, command progress, worker
health, portfolio and order reconciliation, risk status, jobs, audit history,
and paper-to-live readiness. Destructive or live-capital actions require clear
confirmation and recent operator authentication.

## 5. Command And Execution Flow

### 5.1 Strategy Lifecycle

1. The operator submits `start`, `stop`, `restart`, or `reconcile` with an
   idempotency key.
2. The API validates the requested transition and inserts one pending command.
3. A trading worker atomically claims the command using `FOR UPDATE SKIP LOCKED`.
4. The worker acquires or validates the runtime lease and fencing token.
5. It performs the transition, records the result, and appends an audit event.
6. Replayed requests return the original command and result.

Command states are:

```text
pending -> processing -> succeeded
                      -> retry_wait -> pending
                      -> failed
                      -> cancelled
```

Retries are bounded with exponential backoff and jitter. Expired processing
leases return commands to the eligible queue without losing attempt history.

### 5.2 Order Submission

1. A strategy emits an opportunity.
2. Risk evaluation produces an approved or rejected decision with a complete
   reason and the limits used.
3. An `order_intent` is committed before any exchange call.
4. The broker submits using the intent's deterministic client order ID.
5. The response is persisted, then balances, orders, fills, and portfolio state
   are reconciled.
6. A timeout or ambiguous response triggers lookup by client order ID before
   any retry.

Order intent states are explicit and terminal transitions are immutable:

```text
created -> approved -> submitting -> submitted -> partially_filled -> filled
                  |             |             -> cancelled
                  |             -> unknown -> reconciling
                  -> rejected                  -> failed
```

### 5.3 Finite Jobs

The API or scheduler inserts a job row and publishes its ID after commit. A job
worker claims the row, writes progress, stores artifact metadata, and records a
terminal status. Publishing is recoverable through an outbox-style dispatcher,
so a database commit cannot be lost because Redis was temporarily unavailable.

## 6. Security Model

- One operator account, initialized out of band; no public registration.
- Passwords use Argon2id. Browser sessions use secure, HTTP-only, same-site
  cookies with CSRF protection. API automation uses scoped hashed tokens.
- All mutation and secret-management routes require authentication; live-order
  controls require recent re-authentication.
- Exchange credentials remain outside Git, are encrypted at rest with a
  deployment-provided master key, and are never returned by APIs or logs.
- Startup validates trade-only permissions, withdrawal disabled status where the
  venue exposes it, configured symbol allowlists, and hard exposure limits.
- Containers run as non-root with read-only filesystems where possible, dropped
  Linux capabilities, explicit writable volumes, and resource limits.
- Dependency, secret, static-analysis, and container-image scans run in CI.

## 7. Reliability And Observability

Every process emits structured JSON logs with request, command, job, strategy,
and order correlation IDs. Prometheus metrics cover command latency, queue age,
worker and lease freshness, strategy cycle duration, provider errors, broker
errors, reconciliation drift, order outcomes, exposure, and kill-switch state.

Liveness proves that a process is responsive. Readiness proves database and
required dependency access. Worker health proves fresh heartbeats and lease
renewal. Alertmanager routes stale workers, reconciliation drift, repeated
broker errors, exhausted commands, and kill-switch activation.

PostgreSQL receives automated backups with retention and restore drills. The
deployment runbook defines recovery-point and recovery-time targets and records
the latest successful restore test.

## 8. Deployment Topology

Docker Compose is the first production topology because QT currently targets a
single host. Services are:

- PostgreSQL;
- Redis cache;
- Redis jobs with AOF and `noeviction`;
- migration job;
- API;
- trading worker;
- scheduler worker;
- Celery worker and beat;
- operator console/reverse proxy;
- optional Prometheus, Alertmanager, and Grafana profile.

Startup order is health-based: databases, migrations, API/workers, then console.
Deployments use a compatibility window so the previous application version can
run during a forward migration. Rollback never attempts a destructive schema
downgrade; it deploys the previous compatible image.

## 9. Delivery Phases And Gates

### Phase 1: Durable Control Foundation

Deliver PostgreSQL configuration, migrations, command repository, runtime
leases, worker heartbeats, audit events, health endpoints, and a trading-worker
command shell. The existing strategy process continues operating until Phase 2.

Gate: concurrent command claims produce one owner; idempotent replay produces
one command; lease takeover increments fencing; readiness fails on database
loss; migrations and PostgreSQL integration tests pass.

### Phase 2: Runtime Isolation

Move strategy lifecycle ownership from `run_all.py` into the trading worker.
Add start, stop, restart, status, and reconcile commands while preserving paper
execution behavior and portfolio ledgers.

Gate: the API contains no runner threads; two workers cannot run the same
strategy; crash takeover is tested; graceful shutdown releases ownership;
paper-trading smoke results match the pre-migration baseline.

### Phase 3: Authenticated Operator Platform

Add operator authentication, CSRF protection, API tokens, command and audit
views, worker health, runtime controls, SSE updates, and API-backed dashboard
queries.

Gate: unauthenticated mutations fail; replay is idempotent; all operator
controls are accessible from the website; browser and API contract tests pass.

### Phase 4: Execution Safety And Reconciliation

Persist order intents, deterministic client order IDs, reconciliation status,
global exposure reservations, paper-to-live promotion evidence, and live-action
reauthentication.

Gate: ambiguous exchange responses cannot duplicate an order; restart rebuilds
state from the venue; risk limits hold under concurrent strategies; live mode
cannot start without complete acceptance evidence.

### Phase 5: Jobs And Research Operations

Move backtests, reports, synchronization, and scheduled validation into durable
jobs with progress, cancellation, artifacts, retries, and retention.

Gate: jobs survive process restart; duplicate publication is harmless; bounded
workers apply backpressure; artifacts remain reproducible from frozen inputs.

### Phase 6: Observability, Security, And Deployment

Add metrics, dashboards, alerts, hardened containers, dependency scanning,
backups, restore drills, deployment checks, and complete operations runbooks.

Gate: failure drills detect stale workers, database loss, Redis loss, provider
timeouts, broker failures, and reconciliation drift; backup restore is proven;
deployment and rollback are rehearsed on a clean host.

### Phase 7: Competitive Operator Features

Add strategy API V2 metadata and timing contracts, multi-venue portfolio views,
paper/live comparison, capacity and slippage analytics, strategy promotion and
retirement workflows, and evidence-linked performance reports.

Gate: every enabled strategy declares its data, market, frequency, timing, risk,
and promotion contract; the console explains actual net performance versus DCA;
operators can trace every decision from input snapshot to fill and P&L.

## 10. Testing Strategy

- Unit tests cover state machines, validation, authorization, risk invariants,
  retries, and serialization.
- PostgreSQL integration tests cover concurrent claims, leases, fencing,
  migrations, idempotency, and transaction rollback.
- Contract tests cover OpenAPI requests, responses, and error envelopes.
- Worker tests use real repositories and controlled clocks; external exchanges
  are replaced by deterministic broker fakes at the adapter boundary.
- End-to-end tests cover operator command to worker result and paper order to
  reconciled portfolio.
- Failure tests kill workers between claim and completion, interrupt exchange
  responses, expire leases, disconnect Redis, and restart the complete stack.
- Existing QT backtest and strategy tests remain regression gates.

## 11. Migration And Compatibility

The migration is additive. Phase 1 introduces platform state without changing
trading behavior. Phase 2 runs one strategy through the new worker behind a
configuration gate, compares outputs, then migrates the remaining strategies.
Filesystem artifacts remain readable until their API-backed replacements have
passed parity checks. The current `./start.sh` interface remains available and
delegates to the control API as capabilities move.

Direct QuantDinger code is not required for this design. If code is copied in a
later phase, Apache 2.0 attribution and modification notices must be retained.
QuantDinger branding and private frontend artifacts are out of scope.

## 12. Definition Of Production Ready

The platform is production ready only when all phase gates pass and there is
current evidence for:

- deterministic deployment from a clean checkout;
- authenticated and auditable operation;
- single ownership of every trading runtime;
- idempotent commands, jobs, and exchange side effects;
- tested reconciliation and recovery after process failure;
- enforced capital and credential guardrails;
- live health, metrics, logs, and actionable alerts;
- successful backup restoration and rollback rehearsal;
- paper-to-live promotion evidence for each enabled strategy;
- documented residual risks and operator runbooks.

Passing unit tests alone is not sufficient evidence for this definition.
