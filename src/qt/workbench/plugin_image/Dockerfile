# Build from the repository root: docker build -f src/qt/workbench/plugin_image/Dockerfile .
# Docker Hub's official python:3.12.11-slim-bookworm OCI index, resolved 2026-09-07.
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS builder

WORKDIR /build
COPY src/qt/workbench/plugin_image/build-requirements.lock /build/build-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /build/build-requirements.lock
COPY packages/btc-backtest /build/btc-backtest
COPY pyproject.toml README.md /build/qt/
COPY src /build/qt/src
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels /build/btc-backtest /build/qt

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /opt/runtime
COPY src/qt/workbench/plugin_image/requirements.lock /opt/runtime/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/runtime/requirements.lock
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl \
    && python -m pip check \
    && python -c "import btc_backtest, nautilus_trader, qt.nautilus.executor, qt.nautilus.runner" \
    && rm -rf /wheels
COPY src/qt/workbench/plugin_image/entrypoint.py /opt/runtime/entrypoint.py
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin plugin \
    && mkdir -p /data/ohlcv /input /output \
    && chown -R plugin:plugin /data /input /output /opt/runtime
USER 10001:10001
ENTRYPOINT ["python", "/opt/runtime/entrypoint.py"]
