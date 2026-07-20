# Task 2: ORM Schema And Forward Migration

## Scope

Implemented the durable platform control-plane ORM, typed service contracts, Alembic
configuration, and an explicit initial PostgreSQL migration. The migration was written
and reviewed directly; Alembic autogenerate was not used for the committed revision.

## Checkpoint A: ORM Schemas And SQLite Tests

### RED

Command:

```bash
.venv/bin/pytest tests/platform/test_models.py -q
```

Result:

```text
ModuleNotFoundError: No module named 'qt.platform.models'
```

The test suite was created before `schemas.py` and `models.py`, so collection failed for
the expected missing production module.

### GREEN

Added:

- `CommandStatus`, `CommandType`, and `WorkerStatus` string enums.
- Frozen `CommandView` and `LeaseGrant` Pydantic views with aware datetime fields.
- SQLAlchemy `Base`, `PlatformCommand`, `RuntimeLease`, `WorkerHeartbeat`, and
  append-only `AuditEvent` models.
- Bounded `String` columns plus named `CheckConstraint` values instead of PostgreSQL
  native enums.
- Named idempotency, lease, heartbeat, attempt, fencing-token, and claimable-index
  constraints.
- Aware Python UTC defaults for all timestamp defaults.

Command:

```bash
.venv/bin/pytest tests/platform/test_models.py -q
```

Result:

```text
8 passed in 0.54s
```

The SQLite tests cover idempotency, numeric/status constraints, bounded status storage,
lease and heartbeat identities, append-only audit timestamps, aware defaults, and frozen
service views.

## Checkpoint B: Explicit Alembic Migration And PostgreSQL Tests

### RED

Command:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest tests/integration/test_migrations.py -q
```

Result:

```text
alembic.util.exc.CommandError: No 'script_location' key found in configuration.
```

The integration test was added before `alembic.ini` and the migration environment, so it
failed for the expected missing Alembic setup.

### GREEN

Added:

- `alembic.ini`, `migrations/env.py`, and the standard revision template.
- An explicit `20260721_0001_control_foundation` migration that creates all four tables,
  named constraints, and `ix_command_claimable`, and drops them in reverse dependency
  order.
- Alembic environment configuration that reads `QT_DATABASE_URL`, imports `Base.metadata`,
  uses Task 1's `PlatformSettings` and `create_platform_engine`, and enables
  `compare_type=True` and `transaction_per_migration=True`.
- PostgreSQL integration cleanup that creates a unique schema per test and removes it with
  `DROP SCHEMA ... CASCADE`.

Command:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest tests/integration/test_migrations.py -q
```

Result:

```text
1 passed in 1.02s
```

The test upgraded an empty isolated PostgreSQL schema, verified the four platform tables,
named constraints and index, verified no native PostgreSQL enum types, downgraded to base,
and verified tables and enum types were absent afterwards.

Skip behavior was also verified without the environment variable:

```text
1 skipped in 0.39s
SKIPPED: QT_TEST_POSTGRES_URL is not configured
```

## Final Verification

Focused schema and migration tests:

```text
9 passed in 1.11s
```

Ruff:

```text
All checks passed!
```

Full suite:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest -q
```

```text
167 passed in 24.30s
```

`git diff --check` also passed before commit.

## Follow-up: Merge-Blocking Review Fixes

### RED

1. Engine URL normalization test:

   ```bash
   .venv/bin/pytest tests/platform/test_database.py -q
   ```

   Result:

   ```text
   ImportError: cannot import name 'normalize_database_url' from 'qt.platform.database'
   ```

2. Offline Alembic environment test, run with inherited `QT_*` variables removed:

   ```bash
   env -u QT_DATABASE_URL -u QT_PLATFORM_ENV -u QT_TEST_POSTGRES_URL \
     .venv/bin/pytest \
     tests/integration/test_migrations.py::test_offline_migration_uses_validated_normalized_environment_url \
     -q
   ```

   Result:

   ```text
   assert 0 != 0
   ```

   Production SQLite incorrectly generated offline SQL because Alembic bypassed
   `PlatformSettings` validation.

3. PostgreSQL audit immutability test:

   ```bash
   QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
     .venv/bin/pytest \
     tests/integration/test_migrations.py::test_audit_events_reject_update_and_delete_in_postgresql \
     -q
   ```

   Result:

   ```text
   Failed: DID NOT RAISE sqlalchemy.exc.DBAPIError
   ```

4. UTC type and ORM immutability tests:

   ```bash
   .venv/bin/pytest tests/platform/test_models.py -q
   ```

   Result:

   ```text
   ImportError: cannot import name 'UTCDateTime' from 'qt.platform.models'
   ```

### GREEN

- Added `normalize_database_url()` to convert only a bare `postgresql://` prefix to
  `postgresql+psycopg://`; `create_platform_engine()` and both Alembic execution paths
  now use the normalized URL. The engine test checks `engine.url.drivername` without a
  database connection.
- Alembic now builds `PlatformSettings(_env_file=None)`, rejects absent
  `QT_DATABASE_URL`, validates the supplied environment, and runs with the same normalized
  URL in offline and online modes. The subprocess test scrubs inherited `QT_*` variables,
  verifies production SQLite fails, and verifies a bare PostgreSQL URL reaches SQL output.
- Added explicit revision `20260721_0002_audit_event_immutability` with a PostgreSQL
  `BEFORE UPDATE OR DELETE` trigger and its function. Downgrade drops the trigger and
  function. PostgreSQL tests prove both statements raise, roll back, and leave the event
  intact; downgrade verifies the function no longer exists.
- Added `UTCDateTime` to reject naive bound values, normalize aware values to UTC, and
  restore UTC information on naive SQLite results. Every ORM datetime column now uses it.
  Commit/reload tests verify all timestamps are aware UTC and `CommandView` validates.
- Added ORM `before_update` and `before_delete` guards for `AuditEvent`, giving SQLite and
  application callers the same append-only behavior as the PostgreSQL trigger.
- Pinned unit engines to `platform_env="test"` with explicit in-memory SQLite URLs, and
  made migration tests control `QT_PLATFORM_ENV` and `QT_DATABASE_URL` explicitly.

Focused verification:

```text
tests/platform/test_config.py tests/platform/test_database.py tests/platform/test_models.py
22 passed in 0.79s

tests/integration/test_migrations.py
3 passed in 2.25s

Ruff
All checks passed!

Mypy
Success: no issues found in 8 source files
```

Full suite:

```bash
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test \
  .venv/bin/pytest -q
```

```text
174 passed in 26.36s
```

### Follow-up Files

- `src/qt/platform/database.py`
- `src/qt/platform/models.py`
- `migrations/env.py`
- `migrations/versions/20260721_0002_audit_event_immutability.py`
- `tests/platform/test_database.py`
- `tests/platform/test_models.py`
- `tests/integration/test_migrations.py`

### Self-Review

- The URL rewrite is prefix-only and leaves explicit SQLAlchemy drivers and non-PostgreSQL
  URLs unchanged.
- The append-only database guarantee lives in a new forward migration rather than changing
  the already-committed base revision; downgrade removes both database objects.
- `UTCDateTime` changes Python value validation/result hydration only; its underlying SQL
  representation remains `TIMESTAMP WITH TIME ZONE`, so no schema type migration is needed.
- PostgreSQL tests isolate each run in a unique schema and remove it with `CASCADE`.
- `git diff --check`, focused tests, Ruff, Mypy, and the full suite passed.
