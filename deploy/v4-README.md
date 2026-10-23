# Production V4 local operations

Run all commands from the checkout. The wrapper always uses `docker --context orbstack` and owns Compose project `qt-production-v4`. It does not restart the existing workbench or Agent Hub.

```sh
deploy/orbstack.sh v4-init
deploy/orbstack.sh v4-config
deploy/orbstack.sh v4-up
deploy/orbstack.sh v4-status
deploy/orbstack.sh v4-check
```

`v4-init` creates ignored `.env.production-v4` with mode `0600`. Database credentials, API token and signing key are generated independently and never printed. Existing valid configuration is preserved. API ingress binds only `127.0.0.1:8890`; PostgreSQL has no production host port. Native experiment workers use Python 3.12/Nautilus rc4. Six process roles have separate containers and resource limits.

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
```

The candidate runs Python 3.13/Nautilus rc7 in a separate image with network access disabled, no database credentials and no dataset mounts. A frozen synthetic fixture compares NAV, decisions, economic fills, fees and account invariants against rc4. Reports live under ignored `data/runtime/production-v4/native-parity`. Even exact parity leaves `upgrade_accepted=false`; full regression, causal, execution and recovery acceptance is required before changing any production image.

The initial real candidate check built both images successfully but **blocked the rc7 upgrade**: the existing adapter passes `maker_fee` to `CurrencyPair`, whose rc7 constructor rejects that keyword. The frozen replay therefore stops before economic parity can be established. This is an adapter compatibility failure, not a strategy return result. The accepted execution baseline remains Python 3.12/Nautilus rc4; an explicit adapter migration and the full acceptance process are required before changing it.

`v4-down` preserves production volumes. `v4-test-db-down` removes only the disposable test stack. Existing data must never be removed to fix a deployment failure.
