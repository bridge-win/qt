# QT isolated plugin image

This image is the only supported execution environment for submitted Python
strategy versions. Build it from the repository root:

```sh
docker build --pull=false \
  --tag qt-plugin-runtime:py312-nautilus-2.0.0rc4 \
  --file src/qt/workbench/plugin_image/Dockerfile .
```

Both stages use the official Docker Hub `python:3.12.11-slim-bookworm` OCI
index pinned to `sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7`.

It installs hash-locked PEP 517 build tools, builds local `btc-backtest` and
`qt` wheels, then installs the pinned native research environment.
`requirements.lock` is a hash-locked universal resolution
of every declared non-local Qt and `btc-backtest` dependency; regenerate it with
`uv pip compile --generate-hashes --universal --python-version 3.12 --output-file
src/qt/workbench/plugin_image/requirements.lock src/qt/workbench/plugin_image/requirements.in`.
`build-requirements.lock` pins the `btc-backtest` PEP 517 backend requirements
(`setuptools==80.9.0` and `wheel==0.45.1`) and is regenerated the same way from
`build-requirements.in`.
The Docker build runs `pip check` and imports the native modules after installing
the local wheels. Runtime execution never calls `pip`, pulls images, mounts
the Docker socket, forwards host environment variables, or enables network.

## Plugin source contract

The immutable source must target `plugin_api_version: "1"`, import only the
preinstalled `btc_backtest`, `collections`, `decimal`, `math`, and `typing`
roots, and define exactly:

```python
def build_strategy(parameters: dict[str, object]) -> Strategy:
    return MyStrategy(parameters)
```

The returned object must implement `btc_backtest.strategies.base.Strategy`:
`metadata`, `initialize(context)`, `on_bar(context)`, and `finalize(context)`.
TA-Lib and Optuna are not installed in this image. They are neither supported
plugin imports nor advertised indicator or optimization capabilities here.
The Nautilus bridge supplies causal completed-bar history, but that is not a
temporal-integrity guarantee for arbitrary plugin source: the container also
mounts the selected full-history Parquet file and loads native Python modules.
An unaudited plugin can read future data directly or monkeypatch those modules.
No submitted source is executed during API validation; AST checks are only an
entrypoint/dependency preflight and are not a security or causality boundary.

Every plugin result and environment manifest is marked
`temporal_integrity: "unverified"` and `verified_leaderboard_eligible: false`.
The integrating worker/result service must exclude these results from verified
leaderboards and causal-performance claims. See
[`INTEGRATION-CONTRACT.md`](INTEGRATION-CONTRACT.md).

The entrypoint executes the returned strategy through the real
`NautilusDataFrameRunner` and uses `NautilusResearchExecutor` artifact
publication. It writes `result.json`, `environment-manifest.json`, and native
reports only under `/output`.

## Host-mount and image identity contract

Each execution uses the invoking host worker's non-root UID/GID; a root host is
rejected and must use a dedicated unprivileged worker account. The fresh task,
`/input`, and `/output` mounts are respectively `0700`, `0500`, and `0700`.
The host never broadens permissions on data or artifact roots. The runtime
inspects the configured image tag once, then runs the returned immutable Docker
image ID (`sha256:...`) with `--pull=never`; it does not run the mutable tag
directly.

For acceptance evidence, the trusted environment manifest records the effective
UID/GID, a rootfs write probe, network interfaces, Docker-socket absence, and
the cgroup CPU, memory, and PID limits observed from inside the container.
Those observations prove a run used the Docker boundary; they do not make
plugin-origin metrics or temporal behavior trusted.
