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
