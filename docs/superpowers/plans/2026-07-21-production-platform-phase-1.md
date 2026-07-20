# Production Platform Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build QT's durable control foundation with transactional commands, renewable runtime leases, worker heartbeats, audit events, health APIs, a trading-worker shell, migrations, and a runnable PostgreSQL deployment.

**Architecture:** Add a focused `qt.platform` package around SQLAlchemy 2 and PostgreSQL while preserving every existing trading-domain path. FastAPI exposes health and command-query contracts; independent worker processes claim persisted work and prove ownership with fencing tokens. SQLite supports fast unit tests, while PostgreSQL is mandatory for concurrency and production acceptance.

**Tech Stack:** Python 3.10+, Pydantic 2, FastAPI 0.139.x, Uvicorn 0.51.x, SQLAlchemy 2.0.x, Alembic 1.18.x, Psycopg 3.3.x, PostgreSQL 17, Pytest, Ruff, Mypy, Docker Compose

## Global Constraints

- Preserve `qt.data`, `qt.signal`, `qt.risk`, `qt.backtest`, `qt.execution`, `qt.portfolio`, and strategy behavior.
- HTTP processes must never start strategy, scheduler, or exchange polling loops.
- PostgreSQL is required in production; SQLite is limited to unit tests and local development.
- Every mutating command requires a unique `(owner_id, idempotency_key)` pair.
- Workers claim commands before side effects and complete them only with the active claim token.
- Runtime ownership uses renewable leases and monotonically increasing fencing tokens.
- All timestamps are timezone-aware UTC values.
- Production configuration must fail closed when `QT_PLATFORM_ENV=production` uses a non-PostgreSQL URL.
- No direct QuantDinger source is copied in this phase.
- Each task follows red-green-refactor and ends with a focused commit.

---

## File Map

- `src/qt/platform/config.py`: platform environment and production validation.
- `src/qt/platform/database.py`: SQLAlchemy engine and session construction.
- `src/qt/platform/models.py`: ORM schema for commands, leases, heartbeats, and audits.
- `src/qt/platform/schemas.py`: enums and immutable service return models.
- `src/qt/platform/commands.py`: idempotent enqueue, claim, complete, and fail operations.
- `src/qt/platform/leases.py`: acquire, renew, release, and fencing behavior.
- `src/qt/platform/operations.py`: heartbeat and append-only audit operations.
- `src/qt/platform/health.py`: database and worker readiness evaluation.
- `src/qt/platform/api.py`: FastAPI application and Phase 1 endpoints.
- `src/qt/platform/worker.py`: bounded command processor and worker heartbeat loop.
- `scripts/run_platform_api.py`: API process entry point.
- `scripts/run_trading_worker.py`: trading-worker process entry point.
- `migrations/`: Alembic configuration and first forward migration.
- `docker-compose.platform.yml`: PostgreSQL, migration, API, and worker services.
- `Dockerfile.platform`: one hardened runtime image for independent roles.
- `tests/platform/conftest.py`: isolated database, repository, and controlled-clock fixtures.
- `tests/platform/`: unit and API tests using isolated SQLite databases.
- `tests/integration/`: PostgreSQL concurrency and migration acceptance tests.

---

### Task 1: Platform Configuration And Database Boundary

**Files:**
- Create: `src/qt/platform/__init__.py`
- Create: `src/qt/platform/config.py`
- Create: `src/qt/platform/database.py`
- Modify: `pyproject.toml`
- Test: `tests/platform/test_config.py`
- Test: `tests/platform/test_database.py`

**Interfaces:**
- Produces: `PlatformSettings`, `create_platform_engine(settings)`, and `create_session_factory(engine)`.
- Consumes: existing Pydantic settings conventions from `qt.core.config`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_production_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PlatformSettings(platform_env="production", database_url="sqlite+pysqlite:///:memory:")


def test_development_accepts_sqlite() -> None:
    settings = PlatformSettings(
        platform_env="development",
        database_url="sqlite+pysqlite:///:memory:",
    )
    assert settings.database_url.startswith("sqlite")
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `.venv/bin/pytest tests/platform/test_config.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'qt.platform'`.

- [ ] **Step 3: Add bounded platform dependencies**

Add to `[project].dependencies` in `pyproject.toml`:

```toml
"fastapi>=0.139,<1",
"uvicorn>=0.51,<1",
"sqlalchemy>=2.0.51,<2.1",
"alembic>=1.18.5,<2",
"psycopg[binary]>=3.3.4,<4",
```

- [ ] **Step 4: Implement settings and database factories**

```python
class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QT_", env_file=".env", extra="ignore")

    platform_env: Literal["development", "test", "staging", "production"] = "development"
    database_url: str = "sqlite+pysqlite:///data/runtime/platform.db"
    database_echo: bool = False
    command_lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_stale_seconds: int = Field(default=60, ge=10, le=3600)

    @model_validator(mode="after")
    def require_postgresql_in_staging_and_production(self) -> PlatformSettings:
        if self.platform_env in ("staging", "production") and not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://")
        ):
            raise ValueError("staging and production platform storage must use PostgreSQL")
        return self
```

```python
SessionFactory = sessionmaker[Session]


def create_platform_engine(settings: PlatformSettings) -> Engine:
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, expire_on_commit=False)
```

- [ ] **Step 5: Install and verify tests**

Run: `.venv/bin/pip install -e ".[dev]"`

Run: `.venv/bin/pytest tests/platform/test_config.py tests/platform/test_database.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/qt/platform tests/platform
git commit -m "feat: add platform database configuration"
```

---

### Task 2: ORM Schema And Forward Migration

**Files:**
- Create: `src/qt/platform/models.py`
- Create: `src/qt/platform/schemas.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260721_0001_control_foundation.py`
- Test: `tests/platform/test_models.py`
- Test: `tests/integration/test_migrations.py`

**Interfaces:**
- Produces: `Base`, `PlatformCommand`, `RuntimeLease`, `WorkerHeartbeat`, `AuditEvent`.
- Produces: `CommandStatus`, `CommandType`, `WorkerStatus`, `CommandView`, `LeaseGrant`.
- Consumes: `PlatformSettings` and `create_platform_engine` from Task 1.

- [ ] **Step 1: Write failing schema tests**

```python
def test_command_idempotency_is_unique(session: Session) -> None:
    session.add(make_command(idempotency_key="request-1"))
    session.commit()
    session.add(make_command(idempotency_key="request-1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_audit_event_has_no_update_timestamp() -> None:
    columns = AuditEvent.__table__.columns
    assert "created_at" in columns
    assert "updated_at" not in columns
```

- [ ] **Step 2: Run tests and verify missing schema types**

Run: `.venv/bin/pytest tests/platform/test_models.py -q`

Expected: collection fails because `qt.platform.models` does not exist.

- [ ] **Step 3: Define explicit enums and service views**

```python
class CommandStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommandType(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    RECONCILE = "reconcile"
    NOOP = "noop"


class LeaseGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource_type: str
    resource_id: str
    owner_id: str
    fencing_token: int
    expires_at: datetime
```

- [ ] **Step 4: Define ORM tables with database constraints**

`PlatformCommand` must include UUID `id`, `owner_id`, `command_type`, `target`,
JSON `payload`, `idempotency_key`, status, attempts, max attempts,
`available_at`, claim owner/token/expiry, JSON result, error, version, and UTC
created/updated/completed timestamps. Add these constraints:

```python
UniqueConstraint("owner_id", "idempotency_key", name="uq_command_owner_idempotency")
CheckConstraint("attempts >= 0", name="ck_command_attempts_non_negative")
CheckConstraint("max_attempts >= 1", name="ck_command_max_attempts_positive")
Index("ix_command_claimable", "status", "available_at", "created_at")
```

`RuntimeLease` uses a unique `(resource_type, resource_id)` pair and positive
`fencing_token`. `WorkerHeartbeat` uses unique `(role, instance_id)` identity.
`AuditEvent` is append-only and stores actor, action, target, correlation ID,
JSON details, and `created_at` only.

- [ ] **Step 5: Add Alembic configuration and the explicit first migration**

The migration must create the four tables and all named constraints and indexes.
`migrations/env.py` must read `QT_DATABASE_URL`, import `Base.metadata`, and
enable `compare_type=True` and `transaction_per_migration=True`.

- [ ] **Step 6: Verify unit schema and migration tests**

Run: `.venv/bin/pytest tests/platform/test_models.py -q`

Run with PostgreSQL available:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest tests/integration/test_migrations.py -q
```

Expected: tables upgrade from an empty database and downgrade cleanly in the
isolated test database.

- [ ] **Step 7: Commit**

```bash
git add alembic.ini migrations src/qt/platform/models.py \
  src/qt/platform/schemas.py tests/platform/test_models.py \
  tests/integration/test_migrations.py
git commit -m "feat: add platform control schema"
```

---

### Task 3: Durable Command Repository

**Files:**
- Create: `src/qt/platform/commands.py`
- Test: `tests/platform/test_commands.py`
- Test: `tests/integration/test_command_concurrency.py`

**Interfaces:**
- Produces: `CommandRepository.enqueue`, `claim_next`, `complete`, `fail`, `get`, and `list_recent`.
- Produces: `StaleCommandClaimError` for invalid completion ownership.
- Consumes: `PlatformCommand`, `CommandView`, and `CommandStatus` from Task 2.

- [ ] **Step 1: Write failing idempotency and claim tests**

```python
def test_enqueue_replays_existing_command(repository: CommandRepository) -> None:
    first = repository.enqueue(
        owner_id="operator",
        command_type=CommandType.START,
        target="dca",
        payload={"mode": "paper"},
        idempotency_key="start-dca-1",
    )
    replay = repository.enqueue(
        owner_id="operator",
        command_type=CommandType.START,
        target="dca",
        payload={"mode": "paper"},
        idempotency_key="start-dca-1",
    )
    assert replay.id == first.id


def test_active_claim_cannot_be_claimed_twice(repository: CommandRepository) -> None:
    repository.enqueue(
        owner_id="operator",
        command_type=CommandType.NOOP,
        target="worker",
        payload={},
        idempotency_key="noop-1",
    )
    first = repository.claim_next(worker_id="worker-a", lease_seconds=30)
    second = repository.claim_next(worker_id="worker-b", lease_seconds=30)
    assert first is not None
    assert second is None
```

- [ ] **Step 2: Run tests and verify missing repository failure**

Run: `.venv/bin/pytest tests/platform/test_commands.py -q`

Expected: collection fails because `CommandRepository` is missing.

- [ ] **Step 3: Implement transactional enqueue and claim**

Define `Clock = Callable[[], datetime]`. Construct `CommandRepository` with
`(session_factory: SessionFactory, clock: Clock = utc_now)`. Implement
`enqueue(*, owner_id: str, command_type: CommandType, target: str,
payload: Mapping[str, object], idempotency_key: str, max_attempts: int = 3)
-> CommandView` and `claim_next(*, worker_id: str, lease_seconds: int)
-> CommandView | None`.

`claim_next` selects one eligible pending/retry command or expired processing
command ordered by `available_at, created_at`, using
`with_for_update(skip_locked=True)`. It increments attempts, assigns a new UUID
claim token, sets `processing`, and commits before returning.

- [ ] **Step 4: Implement fenced completion and bounded failure**

Implement `complete(*, command_id: UUID, claim_token: UUID,
result: Mapping[str, object]) -> CommandView` and `fail(*, command_id: UUID,
claim_token: UUID, error: str, retry_delay_seconds: int | None) -> CommandView`.

Both methods lock the row and require `processing` plus the current claim token.
`fail` uses `retry_wait` only when a delay is provided and attempts remain;
otherwise it writes terminal `failed`. Terminal rows are never claimable.

- [ ] **Step 5: Verify state-machine tests**

Run: `.venv/bin/pytest tests/platform/test_commands.py -q`

Expected: idempotency, active claims, expired-claim recovery, delayed retry,
terminal failure, completion, and stale-token tests all pass.

- [ ] **Step 6: Verify PostgreSQL concurrent claiming**

The integration test starts two threads at one barrier, gives each an independent
session factory, and asserts exactly one receives the only queued command.

Run:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest tests/integration/test_command_concurrency.py -q
```

Expected: one command ID and one `None` result, with no duplicate attempts.

- [ ] **Step 7: Commit**

```bash
git add src/qt/platform/commands.py tests/platform/test_commands.py \
  tests/integration/test_command_concurrency.py
git commit -m "feat: add durable command claiming"
```

---

### Task 4: Runtime Leases, Heartbeats, And Audit Events

**Files:**
- Create: `src/qt/platform/leases.py`
- Create: `src/qt/platform/operations.py`
- Test: `tests/platform/test_leases.py`
- Test: `tests/platform/test_operations.py`
- Test: `tests/integration/test_lease_concurrency.py`

**Interfaces:**
- Produces: `LeaseRepository.acquire`, `renew`, and `release`.
- Produces: `OperationsRepository.record_heartbeat`, `list_heartbeats`, `append_audit`, and `list_audit`.
- Consumes: `LeaseGrant`, `RuntimeLease`, `WorkerHeartbeat`, and `AuditEvent`.

- [ ] **Step 1: Write failing fencing tests**

```python
def test_takeover_increments_fencing_token(
    lease_repository: LeaseRepository,
    clock: MutableClock,
) -> None:
    first = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-a",
        ttl_seconds=10,
    )
    assert first is not None
    clock.advance(seconds=11)
    second = lease_repository.acquire(
        resource_type="strategy",
        resource_id="dca",
        owner_id="worker-b",
        ttl_seconds=10,
    )
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
```

- [ ] **Step 2: Run tests and verify missing repository failure**

Run: `.venv/bin/pytest tests/platform/test_leases.py tests/platform/test_operations.py -q`

Expected: collection fails because the lease and operations modules are missing.

- [ ] **Step 3: Implement lease acquisition, renewal, and release**

`acquire` locks the unique resource row. A missing row starts at fencing token
`1`; the same owner may extend an active lease without changing the token; a
different owner receives `None` while active; takeover after expiry increments
the token. `renew` and `release` require owner and fencing token equality.

Implement `renew(*, resource_type: str, resource_id: str, owner_id: str,
fencing_token: int, ttl_seconds: int) -> LeaseGrant | None`.

- [ ] **Step 4: Implement heartbeat upsert and append-only audit writes**

Implement `record_heartbeat(*, role: str, instance_id: str,
status: WorkerStatus, version: str, details: Mapping[str, object])
-> WorkerHeartbeatView` and `append_audit(*, actor_id: str, action: str,
target_type: str, target_id: str, correlation_id: str,
details: Mapping[str, object]) -> AuditEventView`.

- [ ] **Step 5: Verify unit and PostgreSQL concurrency tests**

Run: `.venv/bin/pytest tests/platform/test_leases.py tests/platform/test_operations.py -q`

Run:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest tests/integration/test_lease_concurrency.py -q
```

Expected: one active owner, monotonic fencing, stale renewal rejection, heartbeat
upsert, and immutable audit ordering all pass.

- [ ] **Step 6: Commit**

```bash
git add src/qt/platform/leases.py src/qt/platform/operations.py \
  tests/platform/test_leases.py tests/platform/test_operations.py \
  tests/integration/test_lease_concurrency.py
git commit -m "feat: add runtime ownership and audit state"
```

---

### Task 5: Health Service And FastAPI Contract

**Files:**
- Create: `src/qt/platform/health.py`
- Create: `src/qt/platform/api.py`
- Create: `scripts/run_platform_api.py`
- Test: `tests/platform/test_health.py`
- Test: `tests/platform/test_api.py`

**Interfaces:**
- Produces: `HealthService.readiness`, `worker_health`, and `create_app`.
- API routes: `/api/health/live`, `/api/health/ready`, `/api/health/workers`, `/api/v1/commands`.
- Consumes: session factory and Phase 1 repositories.

- [ ] **Step 1: Write failing API tests**

```python
def test_liveness_has_no_dependency_side_effects(client: TestClient) -> None:
    response = client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_returns_503_when_database_is_unavailable() -> None:
    app = create_app(session_factory=broken_session_factory())
    response = TestClient(app, raise_server_exceptions=False).get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "not_ready"
```

- [ ] **Step 2: Run tests and verify missing API failure**

Run: `.venv/bin/pytest tests/platform/test_health.py tests/platform/test_api.py -q`

Expected: collection fails because health and API modules are absent.

- [ ] **Step 3: Implement health evaluation**

Readiness executes `SELECT 1` and returns structured dependency results. Worker
health compares each heartbeat to the configured stale threshold and returns
`healthy`, `stale`, or `missing` without converting liveness into readiness.

- [ ] **Step 4: Implement the FastAPI application factory**

```python
def create_app(
    *,
    settings: PlatformSettings | None = None,
    session_factory: SessionFactory | None = None,
) -> FastAPI:
    app = FastAPI(title="QT Control API", version="1.0.0")
    # Construct dependencies once; route functions only validate and delegate.
    return app
```

`GET /api/v1/commands` returns recent command views. Mutation routes are deferred
to the authenticated operator phase; Phase 1 creates commands through repository
tests and the worker smoke script only.

- [ ] **Step 5: Add the process entry point**

`scripts/run_platform_api.py` loads `PlatformSettings`, constructs the app, and
runs Uvicorn with host/port from arguments. It must not import `qt.strategies`.

- [ ] **Step 6: Verify API contract tests**

Run: `.venv/bin/pytest tests/platform/test_health.py tests/platform/test_api.py -q`

Expected: liveness, ready/unready database, fresh/stale workers, command listing,
OpenAPI generation, and absence of runner imports all pass.

- [ ] **Step 7: Commit**

```bash
git add src/qt/platform/health.py src/qt/platform/api.py \
  scripts/run_platform_api.py tests/platform/test_health.py tests/platform/test_api.py
git commit -m "feat: add platform health API"
```

---

### Task 6: Trading Worker Command Shell

**Files:**
- Create: `src/qt/platform/worker.py`
- Create: `scripts/run_trading_worker.py`
- Test: `tests/platform/test_worker.py`

**Interfaces:**
- Produces: `TradingWorker.run_once`, `run_forever`, and `stop`.
- Consumes: `CommandRepository`, `OperationsRepository`, `PlatformSettings`.
- Handler contract: `Callable[[CommandView], Mapping[str, object]]`.

- [ ] **Step 1: Write failing processor tests**

```python
def test_worker_completes_claimed_noop(worker: TradingWorker, commands: CommandRepository) -> None:
    queued = commands.enqueue(
        owner_id="operator",
        command_type=CommandType.NOOP,
        target="platform",
        payload={},
        idempotency_key="worker-noop-1",
    )
    assert worker.run_once() is True
    completed = commands.get(queued.id)
    assert completed is not None
    assert completed.status is CommandStatus.SUCCEEDED


def test_worker_terminally_fails_unknown_handler(
    worker: TradingWorker,
    commands: CommandRepository,
) -> None:
    queued = enqueue_start(commands)
    assert worker.run_once() is True
    failed = commands.get(queued.id)
    assert failed is not None
    assert failed.status is CommandStatus.FAILED
```

- [ ] **Step 2: Run tests and verify missing worker failure**

Run: `.venv/bin/pytest tests/platform/test_worker.py -q`

Expected: collection fails because `TradingWorker` is absent.

- [ ] **Step 3: Implement one-cycle processing**

```python
class TradingWorker:
    def run_once(self) -> bool:
        self._operations.record_heartbeat(
            role="trading",
            instance_id=self._worker_id,
            status=WorkerStatus.HEALTHY,
            version=self._version,
            details={},
        )
        command = self._commands.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._command_lease_seconds,
        )
        if command is None:
            return False
        self._execute(command)
        return True
```

The built-in `noop` handler returns `{"handled": True}`. Unsupported lifecycle
commands fail terminally with a precise Phase 2 message. Handler exceptions use
bounded retry with exponential delay based on the attempt count.

- [ ] **Step 4: Implement bounded polling and graceful stop**

`run_forever` uses a stop event and bounded poll interval. It records `stopping`
then `stopped` heartbeats during normal shutdown and never starts a strategy.

- [ ] **Step 5: Add worker CLI and verify tests**

`scripts/run_trading_worker.py` accepts `--worker-id`, `--poll-seconds`, and
`--once`, installs SIGTERM/SIGINT handlers, and constructs repositories from
`PlatformSettings`.

Run: `.venv/bin/pytest tests/platform/test_worker.py -q`

Expected: noop success, unsupported command failure, retry scheduling, idle
heartbeat, and graceful stop tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/qt/platform/worker.py scripts/run_trading_worker.py \
  tests/platform/test_worker.py
git commit -m "feat: add trading worker command shell"
```

---

### Task 7: PostgreSQL Compose Stack And CI Acceptance

**Files:**
- Create: `Dockerfile.platform`
- Create: `docker-compose.platform.yml`
- Create: `.env.platform.example`
- Create: `deploy/platform-entrypoint.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/operations.md`
- Test: `tests/integration/test_platform_stack.py`

**Interfaces:**
- Produces: `postgres`, `migrate`, `api`, and `trading-worker` Compose services.
- Consumes: Alembic, API, worker, and PostgreSQL integration suite from Tasks 1-6.

- [ ] **Step 1: Write failing deployment contract tests**

```python
def test_compose_separates_api_and_trading_worker() -> None:
    config = yaml.safe_load(Path("docker-compose.platform.yml").read_text())
    services = config["services"]
    assert services["api"]["command"] != services["trading-worker"]["command"]
    assert "postgres" in services
    assert services["migrate"]["restart"] == "no"


def test_platform_image_runs_as_non_root() -> None:
    dockerfile = Path("Dockerfile.platform").read_text()
    assert "USER qt" in dockerfile
```

- [ ] **Step 2: Run tests and verify missing deployment files**

Run: `.venv/bin/pytest tests/integration/test_platform_stack.py -q`

Expected: tests fail because the Compose and image definitions are absent.

- [ ] **Step 3: Add the hardened platform image**

The image uses Python 3.12 slim, installs the package from a wheel, creates an
unprivileged `qt` user, exposes no secrets, and delegates to an exec-form entry
point. The entry point supports `migrate`, `api`, and `trading-worker` roles.

- [ ] **Step 4: Add the Compose topology**

PostgreSQL uses a named volume and health check. `migrate` waits for PostgreSQL,
runs `alembic upgrade head`, and exits. API and worker depend on successful
migration. API health uses `/api/health/ready`; worker health checks a fresh
database heartbeat through a small CLI query. No service embeds a real password;
`.env.platform.example` contains only non-secret examples and generation notes.

- [ ] **Step 5: Add PostgreSQL CI service and integration command**

Extend CI with one Python 3.12 integration job using a PostgreSQL service on
port 5432. Set only test credentials and run:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:5432/qt_test \
  pytest tests/integration -q
```

- [ ] **Step 6: Document operations and rollback**

Add exact commands for environment initialization, migration, startup, health,
logs, graceful shutdown, backup, restore-test, and rollback to
`docs/operations.md`. State that Phase 1 does not yet replace `run_all.py`.

- [ ] **Step 7: Verify the complete Phase 1 gate**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src tests
docker compose -f docker-compose.platform.yml config --quiet
docker compose -f docker-compose.platform.yml up -d postgres
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest tests/integration -q
docker compose -f docker-compose.platform.yml up -d --build migrate api trading-worker
curl --fail http://127.0.0.1:8876/api/health/live
curl --fail http://127.0.0.1:8876/api/health/ready
docker compose -f docker-compose.platform.yml down
```

Expected: all tests, lint, typing, Compose validation, PostgreSQL concurrency,
migrations, and both live HTTP probes pass.

- [ ] **Step 8: Review and commit**

Inspect `git diff`, confirm no environment or credential file is tracked, then:

```bash
git add Dockerfile.platform docker-compose.platform.yml .env.platform.example \
  deploy/platform-entrypoint.sh .github/workflows/ci.yml docs/operations.md \
  tests/integration/test_platform_stack.py
git commit -m "feat: ship durable platform foundation"
```

---

## Phase 1 Completion Audit

Before declaring Phase 1 complete, record evidence for every gate:

1. Two PostgreSQL claimers receive exactly one command owner.
2. Replaying one idempotency key returns exactly one command row.
3. Expired lease takeover increments the fencing token.
4. Stale claim and stale lease tokens cannot mutate current state.
5. Readiness returns HTTP 503 when PostgreSQL is unavailable.
6. Migration upgrades an empty database and the stack starts from a clean volume.
7. API and trading worker are separate processes and commands.
8. API source contains no strategy-runner startup path.
9. Existing QT tests, Ruff, and Mypy remain green.
10. No secret, `.env`, runtime database, or generated data is staged.

After the audit, push `codex/quantdinger-platform-upgrade` and begin a separate
Phase 2 plan for strategy runtime isolation.
