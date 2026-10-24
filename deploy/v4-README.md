# Production V4 local operations

Run all commands from the checkout. The wrapper always uses `docker --context orbstack` and owns Compose project `qt-production-v4`. It does not restart the existing workbench or Agent Hub.

```sh
deploy/orbstack.sh v4-init
deploy/orbstack.sh v4-config
deploy/orbstack.sh v4-up
deploy/orbstack.sh v4-status
deploy/orbstack.sh v4-check
```

`v4-init` creates ignored `.env.production-v4` with mode `0600`. Database credentials, API token and signing key are generated independently and never printed. Existing valid configuration is preserved. API ingress binds only `127.0.0.1:8890` through a separate ingress bridge; PostgreSQL remains on the internal control network with no production host port. Validate access from the host as well as inside the container. Native experiment workers use Python 3.12/Nautilus rc4. Six process roles have separate containers and resource limits.

`v4-run [options] SERVICE [command...]` runs a disposable container for a service in this exact project, without starting dependencies. For example, `v4-run --entrypoint python native-candidate -c 'import nautilus_trader; print(nautilus_trader.__version__)'` inspects the isolated candidate without starting the trading stack.

Research stays blocked until an explicit local model ID and `QT_V4_MODEL_ORIGIN` are configured in the private file. The origin is a llama.cpp-compatible server such as `http://host.docker.internal:8080`, without `/v1`. The provider verifies token budgets before inference. Trading stays blocked without a valid, signed `QT_V4_DEPLOYMENT_ID`; no funded venue credentials are mounted by this stack.

For a content-addressed runtime image, set `QT_V4_NATIVE_IMAGE` and matching `QT_EXPERIMENT_IMAGE_DIGEST` in the private configuration, then bind that exact digest in experiment specs. A local image without runtime digest attestation cannot establish a production acceptance claim.

## Automated verification

```sh
deploy/orbstack.sh v4-test
deploy/orbstack.sh v4-test-postgres
deploy/orbstack.sh v4-test-db-down
```

The PostgreSQL test project `qt-v4-test-db` is separate, uses tmpfs data and publishes only `127.0.0.1:55439`. `v4-test-postgres` injects its DSN privately into the project-local pytest process; it never prints a password or DSN. Additional pytest arguments are supported. Browser checks remain deferred.

## Backup and isolated restore checks

```sh
deploy/orbstack.sh v4-backup
deploy/orbstack.sh v4-restore-check /absolute/path/to/private.dump
```

Backups go under ignored `data/runtime/production-v4/backups`, with private file permissions. `pg_dump` and per-table row counts/hashes share an exported repeatable-read snapshot, so concurrent writes do not make the manifest disagree with the dump. Restore checks use a fresh random database in the isolated test cluster, verify hashes/counts, then delete that temporary database. They never cut over or overwrite the production database.

These commands cover database state and checkpoints. They are **not complete platform backups**: immutable Parquet objects and manifests need a separate tested off-host backup in a different failure domain. Machine recovery still requires dataset integrity checks and venue/account reconciliation before new risk.

## Isolated native upgrade comparison

```sh
deploy/orbstack.sh v4-native-compare
deploy/orbstack.sh v4-native-candidate-test
```

The candidate runs Python 3.13/Nautilus rc7 in a separate image with network access disabled, no database credentials and no dataset mounts. A frozen synthetic fixture compares NAV, decisions, economic fills, fees and account invariants against rc4. Reports live under ignored `data/runtime/production-v4/native-parity`. Even exact parity leaves `upgrade_accepted=false`; full regression, causal, execution and recovery acceptance is required before changing any production image.

The compatibility layer preserves rc4's instrument-owned maker/taker rates and explicitly supplies rc7's account-owned `MakerTakerFeeModel(maker_rate=..., taker_rate=...)`. Removing the constructor keywords alone would lose the fee contract. This change follows the [pinned rc7 fee-model source](https://github.com/nautechsystems/nautilus_trader/blob/v2.0.0rc7/crates/execution/src/models/fee.rs), verified against both installed native APIs. The [rc7 release](https://github.com/nautechsystems/nautilus_trader/releases/tag/v2.0.0rc7) requires Python 3.13 or newer.

Parity verification includes the trend fixture, fixed-quantity market and resting-limit round trips with different maker/taker rates, zero-fee controls, prefix replay and future-data mutation. At a maker buy of one unit at 95 followed by a taker sell at 100, rates `0.0025` and `0.01` must produce total fees `1.2375` and final equity `1003.7625` from capital `1000`. Every case checks cash accounting. Comparison reports bind fixture, adapter and registered strategy source hashes, verify raw evidence hashes, and flag missing or newly added evidence fields. Native decision timestamps and financial amounts are compared without tolerance.

One-tick slippage uses a single native `DefaultFillModel` probability hook. Combining the shifted book of `OneTickSlippageFillModel` with that hook previously added two ticks. The forced flag with its legacy default probability `0` means exactly one tick; positive probabilities mean a single tick with the declared probability. Reports record the effective probability. A fixed-unit market round trip at 100 now verifies fills at `100.01` and `99.99`, total fees `0.2` and terminal equity `999.78`. Final NAV uses the engine's actual observation horizon, and quotes available only after the backtest window are excluded. Historical immutable reports remain attached to their original code/image; these corrections require new experiment versions.

The candidate test image runs the historical native tests with network disabled. V2 production experiment tests remain rc4-only; a separate test verifies that an rc7 experiment is refused until release acceptance is recorded. Passing these synthetic checks establishes limited engine compatibility. It does not establish queue realism, market impact, funded-adapter recovery or strategy profitability. The default production image remains Python 3.12/Nautilus rc4 and `upgrade_accepted=false`.

`v4-down` preserves production volumes. `v4-test-db-down` removes only the disposable test stack. Existing data must never be removed to fix a deployment failure.

## Stability and recovery behavior

- Apply additive migration `0003_job_payload_index` through the migration service before starting the new release. It adds an index without changing or rewriting historical experiments.
- `/healthz` reports process liveness. `/readyz` checks connectivity and the exact Alembic revision; the Compose health check uses `/readyz`. Database outages return a sanitized 503 with `Retry-After`, while a liveness response alone never authorizes trading.
- Experiment deadlines default to two hours and may be configured with `QT_V4_EXPERIMENT_TIMEOUT_SECONDS` (60–86,400). Research retains its separate maximum of 15 minutes. SIGTERM and lost leases stop ongoing work; uncertain model calls retain their budget reservations and are not automatically reissued.
- Repeated requests for one frozen experiment share one queued job even when clients use different keys. Every key remains bound to its original request; old job IDs are preserved. Cancelled/exhausted queue jobs leave audited `aborted` trials rather than disappearing from the search population.
- A trading deployment selects its own symbol and closed-bar interval. `QT_V4_SYMBOL`/`QT_V4_BAR_INTERVAL` configure standalone collection and cannot override an approved trading strategy.
- `data-worker --once` completes after its first durable, registered batch. Handshake, bootstrap and stream-idle waits are bounded; invalid books stop decisions until a fresh snapshot/delta bridge is established.
- Manifest notifications are at-least-once and idempotent by dataset ID. If an entire process dies after file publication but before database registration, retain the manifest and re-register it through `POST /api/v4/datasets`; verification checks the original immutable files. Database backups still do not replace an independent dataset backup.
- Execution rechecks the active deployment, lease, market and account mode immediately before sending. A proven unsent Shadow/aborted order releases reservations and records `never_sent`; unknown network outcomes keep reservations for reconciliation.
- Paper and Shadow remain observation stages. Long-running SLOs, venue eligibility, actual queue position/market impact and funded execution are not established by these automated tests.
