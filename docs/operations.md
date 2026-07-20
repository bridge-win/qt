# Operations Runbook

This runbook covers local research, backtests, dashboard use, and unattended
paper-mode operation.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Secrets must come from the shell or OS secret store, never from committed files:

```bash
export QT_GLASSNODE_API_KEY=...
export QT_FRED_API_KEY=...
export QT_SANTIMENT_API_KEY=...
```

## 2. Backfill Data

Free sources are enough to run the first backtests:

```bash
python scripts/fetch_history.py --days 1095
qt data sources
```

`qt data sources` shows each data source, how the strategy uses it, whether an
API key is required, local row count, and last-seen timestamp.

## 3. Run Backtests

```bash
qt --config config/default.yaml backtest --output-dir data/backtests
```

Each run writes:

- `data/backtests/<run_id>/summary.json`
- `data/backtests/<run_id>/equity.csv`
- `data/backtests/<run_id>/trades.csv`
- `data/backtests/<run_id>/signals.csv`
- `data/backtests/latest.json`

The dashboard reads `latest.json`, so a new backtest is visible immediately.

## 4. Start The Dashboard

```bash
qt --config config/default.yaml dashboard --port 8765
```

Open `http://127.0.0.1:8765`.

The dashboard displays:

- paper-loop heartbeat and last error
- latest backtest metrics and exported file paths
- data-source coverage, freshness, and usage

## 5. Run Paper Mode With Self-Monitoring

For unattended operation, run the parent watchdog:

```bash
python scripts/run_service.py \
  --config config/default.yaml \
  --interval 3600 \
  --state-path data/runtime/monitor_state.json \
  --stale-after-seconds 7200 \
  --startup-grace-seconds 300 \
  --dashboard-port 8765
```

`run_service.py` starts and supervises:

- `scripts/run_paper.py`, the strategy loop
- `scripts/run_dashboard.py`, unless `--no-dashboard` is passed

The paper loop writes `data/runtime/monitor_state.json` after each cycle. The
parent watchdog restarts the paper loop when:

- the process exits
- the heartbeat becomes stale
- the heartbeat is missing after the startup grace period
- the heartbeat reports `failed`
- the heartbeat reports `stopped`
- the heartbeat timestamp is invalid

The dashboard is also restarted if it exits.

## 6. Health Checks

For a machine-readable health probe:

```bash
qt monitor health \
  --state-path data/runtime/monitor_state.json \
  --stale-after-seconds 7200 \
  --json
```

Exit code is `0` only when the heartbeat is healthy. Use this from cron,
launchd, systemd, or another external monitor.

## 7. Recommended Local launchd Pattern

Use launchd/systemd/pm2 only to keep `run_service.py` itself alive. The Python
service already restarts its child paper/dashboard processes.

Minimum command:

```bash
/Users/kwt/x/qt/.venv/bin/python /Users/kwt/x/qt/scripts/run_service.py \
  --config /Users/kwt/x/qt/config/default.yaml
```

Set the working directory to `/Users/kwt/x/qt` so relative data paths resolve to
`data/`.

## 8. Stop Safely

Send `SIGTERM` or press `Ctrl-C` in the service terminal. The parent process
terminates child processes before exiting.

## 9. Validation Before Capital

Before enabling live trading:

- backtest multiple market windows
- inspect `signals.csv` for factor explanations
- paper trade for at least 90 days
- confirm the health command stays green through restarts
- keep `QT_LIVE_TRADING_ENABLED=false` until exchange-specific live execution
  has been implemented and reviewed

## 10. Phase 1 Platform Scope

The Phase 1 PostgreSQL platform provides durable commands, worker heartbeats,
leases, audit events, and read-only health/query APIs. It does **not** replace
`scripts/run_all.py`, `scripts/run_service.py`, paper-strategy execution, or the
guarded live-execution path. The `trading-worker` handles `noop` commands only;
strategy lifecycle ownership moves in Phase 2.

Do not run the Phase 1 stack as evidence that live trading is enabled.

## 11. Initialize Platform Configuration

Create the untracked environment file with the repository helper. It generates
a cryptographic URL-safe password, opens the destination with `O_CREAT|O_EXCL`
at mode `0600`, writes the database password and URL consistently, and removes
the partial file if writing fails. It never prints the password.

For a local staging stack:

```bash
python deploy/create_platform_env.py create \
  --environment staging \
  --image-reference qt-platform:local
```

For production, first set `IMMUTABLE_IMAGE` to the verified registry-qualified
digest captured in Section 13, then create the file on a host where it does not
already exist:

```bash
python deploy/create_platform_env.py create \
  --environment production \
  --image-reference "$IMMUTABLE_IMAGE"
```

The command refuses to overwrite `.env.platform`. Confirm its protection
without displaying its contents:

```bash
test "$(stat -c '%a' .env.platform)" = 600
```

Validate interpolation before creating containers:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml config --quiet
```

Never commit `.env.platform`, generated Compose output, database archives, or
runtime data. `QT_PLATFORM_ENV` must be `production` on production hosts and
`staging` on staging or local hosts.

## 12. Local Build And Start

Local development uses the explicit build overlay. It builds
`qt-platform:local`; it does not pull that local-only tag.

```bash
docker compose --env-file .env.platform \
  -f docker-compose.platform.yml \
  -f docker-compose.platform.local.yml config --quiet
docker compose --env-file .env.platform \
  -f docker-compose.platform.yml \
  -f docker-compose.platform.local.yml build --pull
docker compose --env-file .env.platform \
  -f docker-compose.platform.yml \
  -f docker-compose.platform.local.yml up -d
```

Use the same two Compose files for every subsequent local command. The base
Compose file alone is the production deployment contract.

## 13. Publish, Promote, And Deploy By Digest

Build and publish one manifest list for Linux AMD64 and ARM64. Choose a real
registry path and a release identifier from the release process:

```bash
REGISTRY_IMAGE=registry.example.com/qt/platform
RELEASE_TAG=2026-07-21.1
RELEASE_REF="${REGISTRY_IMAGE}:${RELEASE_TAG}"
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --file Dockerfile.platform \
  --tag "$RELEASE_REF" \
  --push .
docker buildx imagetools inspect "$RELEASE_REF"
PLATFORM_DIGEST="$(docker buildx imagetools inspect "$RELEASE_REF" | awk '$1 == "Digest:" {print $2; exit}')"
printf '%s\n' "$PLATFORM_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$'
IMMUTABLE_IMAGE="${REGISTRY_IMAGE}@${PLATFORM_DIGEST}"
docker buildx imagetools inspect "$IMMUTABLE_IMAGE"
RELEASE_RECORD="${QT_RELEASE_RECORD:-$HOME/.local/state/qt-platform/release-digests.log}"
install -d -m 700 "$(dirname "$RELEASE_RECORD")"
touch "$RELEASE_RECORD"
chmod 600 "$RELEASE_RECORD"
printf '%s %s\n' "$RELEASE_TAG" "$IMMUTABLE_IMAGE" >> "$RELEASE_RECORD"
```

Promote the already-built immutable digest to another tag without rebuilding,
then verify the promoted tag resolves to the same manifest digest:

```bash
PROMOTED_REF="${REGISTRY_IMAGE}:production"
docker buildx imagetools create --tag "$PROMOTED_REF" "$IMMUTABLE_IMAGE"
docker buildx imagetools inspect "$PROMOTED_REF"
test "$(docker buildx imagetools inspect "$PROMOTED_REF" | awk '$1 == "Digest:" {print $2; exit}')" = "$PLATFORM_DIGEST"
```

On the deployment host, create or update `.env.platform` with the immutable
image reference, validate, pull, and start without a local build:

```bash
python deploy/create_platform_env.py set-image --image-reference "$IMMUTABLE_IMAGE"
docker compose --env-file .env.platform -f docker-compose.platform.yml config --quiet
docker compose --env-file .env.platform -f docker-compose.platform.yml pull postgres migrate api trading-worker
docker compose --env-file .env.platform -f docker-compose.platform.yml up -d --no-build
docker compose --env-file .env.platform -f docker-compose.platform.yml ps
```

The release log must be backed up with other operator records. It is the source
of previously recorded digest values used for rollback.

## 14. Migration And Runtime Inspection

Startup order is PostgreSQL health, one successful `migrate` job, then separate
API and trading-worker processes. On a clean volume this applies migrations
exactly once before either long-running role starts. Confirm the migration
reached the current head with `alembic current` and that rerunning it is
idempotent:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml logs migrate
docker compose --env-file .env.platform -f docker-compose.platform.yml run --rm migrate
docker compose --env-file .env.platform -f docker-compose.platform.yml run --rm --entrypoint alembic migrate -c /app/alembic.ini current
```

The runtime user must be the unprivileged `qt` account (`uid=10001`):

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T api id
```

## 15. Health And Command Inspection

The image probes both liveness and database readiness without requiring curl.
External operators can inspect each endpoint directly:

```bash
curl --fail --silent --show-error http://127.0.0.1:8876/api/health/live
curl --fail --silent --show-error http://127.0.0.1:8876/api/health/ready
curl --fail --silent --show-error http://127.0.0.1:8876/api/health/workers
curl --fail --silent --show-error 'http://127.0.0.1:8876/api/v1/commands?limit=50'
```

Verify the worker against its durable heartbeat rather than process existence:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T trading-worker python -m qt.platform.probe worker --role trading --instance-id trading-1
```

If `QT_TRADING_WORKER_ID` differs from `trading-1`, use the configured value.
The API `/api/health/workers` identity and worker healthcheck must always match.

## 16. Logs And Graceful Stop

Inspect bounded service logs and follow one role when diagnosing an incident:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml logs --tail=200 postgres migrate api trading-worker
docker compose --env-file .env.platform -f docker-compose.platform.yml logs --follow api trading-worker
```

Gracefully stop application roles before host maintenance, then PostgreSQL:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml stop -t 30 api trading-worker
docker compose --env-file .env.platform -f docker-compose.platform.yml stop -t 60 postgres
```

`SIGTERM` lets the worker record `stopping` and `stopped` heartbeats. A stopped
worker is intentionally unhealthy until the service restarts.

## 17. Backup And Restore Test

Keep backups outside the source repository. The helper creates the target
directory at mode `0700` and a restrictive temporary file in that directory,
runs `pg_dump` into the temporary name, validates it with
`pg_restore --list`, atomically publishes it with `mv`, writes a mode-`0600`
checksum, and verifies it with `sha256sum --check`. Its exit trap removes all
temporary or production-looking partial artifacts on failure.

```bash
QT_BACKUP_DIR=/var/backups/qt-platform deploy/platform-backup.sh
find /var/backups/qt-platform -type f -name 'qt-platform-*.dump' -exec ls -l {} \;
```

Restore every release-candidate backup into a disposable database, verify the
archive and schema, and remove the test database even if a check fails. Replace
`BACKUP` with the selected absolute path:

```bash
set -eu
BACKUP=/var/backups/qt-platform/qt-platform-YYYYMMDDTHHMMSSZ.dump
CHECKSUM="${BACKUP}.sha256"
(cd "$(dirname "$BACKUP")" && sha256sum --check "$(basename "$CHECKSUM")")
docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T postgres sh -ec 'pg_restore --list >/dev/null' < "$BACKUP"
cleanup_restore_test() {
  docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T postgres sh -ec 'dropdb --username "$POSTGRES_USER" --if-exists qt_restore_test' >/dev/null 2>&1 || true
}
trap cleanup_restore_test EXIT HUP INT TERM
docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T postgres sh -ec 'dropdb --username "$POSTGRES_USER" --if-exists qt_restore_test && createdb --username "$POSTGRES_USER" qt_restore_test'
docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T postgres sh -ec 'pg_restore --username "$POSTGRES_USER" --dbname qt_restore_test --exit-on-error' < "$BACKUP"
docker compose --env-file .env.platform -f docker-compose.platform.yml exec -T postgres sh -ec 'psql --username "$POSTGRES_USER" --dbname qt_restore_test --set=ON_ERROR_STOP=1 --command "TABLE alembic_version" --command "SELECT count(*) FROM worker_heartbeats"'
cleanup_restore_test
trap - EXIT HUP INT TERM
unset BACKUP CHECKSUM
```

Record the archive checksum, restore date, revision, row-count checks, operator,
and elapsed time outside the host being protected.

## 18. Rollback By Recorded Digest

Migrations are forward-only during an operational rollback. Do not run
`alembic downgrade` against production. Select a previously recorded digest,
not a mutable tag. The helper atomically updates only `QT_PLATFORM_IMAGE` and
preserves the existing database password:

```bash
PREVIOUS_IMAGE=registry.example.com/qt/platform@sha256:REPLACE_WITH_PREVIOUSLY_RECORDED_DIGEST
docker buildx imagetools inspect "$PREVIOUS_IMAGE"
python deploy/create_platform_env.py set-image --image-reference "$PREVIOUS_IMAGE"
docker compose --env-file .env.platform -f docker-compose.platform.yml config --quiet
docker compose --env-file .env.platform -f docker-compose.platform.yml pull api trading-worker
docker compose --env-file .env.platform -f docker-compose.platform.yml up -d --no-build --no-deps api trading-worker
curl --fail --silent --show-error http://127.0.0.1:8876/api/health/ready
curl --fail --silent --show-error http://127.0.0.1:8876/api/health/workers
```

Never run `docker compose pull` for `qt-platform:local`; that image exists only
through the local build overlay. If the previous immutable image is not
schema-compatible, restore the pre-deploy backup to a separate PostgreSQL
instance and switch only after its restore test passes.

## 19. Clean Volume Recovery

This destructive clean volume procedure is for a new host or a confirmed total
database loss. Preserve any recoverable archive first:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml down -v --remove-orphans
docker compose --env-file .env.platform -f docker-compose.platform.yml pull postgres migrate api trading-worker
docker compose --env-file .env.platform -f docker-compose.platform.yml up -d postgres
docker compose --env-file .env.platform -f docker-compose.platform.yml run --rm migrate
docker compose --env-file .env.platform -f docker-compose.platform.yml up -d api trading-worker
```

For recovery from backup, start PostgreSQL, restore the tested archive, run the
idempotent migration job, and only then start API and worker services.

## 20. Database Secret Rotation

Schedule secret rotation as downtime because the database and application URL
must change atomically. Stop API and worker, generate a new URL-safe value in
the host secret manager, use the interactive `psql` password prompt to alter
the configured PostgreSQL role, update both
`POSTGRES_PASSWORD` and `QT_DATABASE_URL` in `.env.platform`, then recreate the
services and rerun every health check:

```bash
docker compose --env-file .env.platform -f docker-compose.platform.yml stop -t 30 api trading-worker
docker compose --env-file .env.platform -f docker-compose.platform.yml exec postgres psql --username qt_platform --dbname postgres --command '\password qt_platform'
docker compose --env-file .env.platform -f docker-compose.platform.yml up -d --force-recreate postgres
docker compose --env-file .env.platform -f docker-compose.platform.yml up -d --force-recreate api trading-worker
```

The `\password` prompt keeps the new value out of SQL text, process arguments,
and shell history. Use the host secret manager's protected editor to update the
environment file, and securely remove the old value after readiness and worker
health pass.
