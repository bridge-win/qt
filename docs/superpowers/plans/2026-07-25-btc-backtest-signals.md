# BTC Backtest Hybrid Network Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize, store, historically gate, rank, calibrate, and attribute internal and external BTC signals from at least five source types.

**Architecture:** Providers emit immutable `SignalObservation` values into a point-in-time archive. `SignalAggregator` deduplicates compatible observations and ranks consensus using configured reliability, declared confidence, recency decay, and disagreement penalties. Strategies receive only observations whose `observed_at` is available at the active bar.

**Tech Stack:** Python 3.10+, Pydantic, pandas, PyArrow, HTTPX, hashlib/HMAC, pytest, Hypothesis, Ruff, strict Mypy

## Global Constraints

- Historical availability is gated by `observed_at`, not event time.
- Live-only providers cannot participate in earlier backtests.
- Direction is in `[-1, 1]` and confidence/reliability are in `[0, 1]`.
- Every observation carries provider, source event ID, symbol, horizon, expiry, provenance, payload hash, and quality flags.
- Provider credentials are read from environment/config inputs and never serialized.
- No protected-page scraping.
- Reliability updates use completed out-of-sample windows only.
- Aggregation continues after provider failure only when required-source and minimum-source rules pass.
- Every task ends with focused tests, `git diff --check`, a commit, and a push.

---

## File Map

- `signals/models.py`: observations, queries, ranked signals, provider metadata.
- `signals/base.py`: provider protocol and registry.
- `signals/store.py`: immutable point-in-time Parquet archive.
- `signals/providers/binance.py`: derivatives observations.
- `signals/providers/alternative.py`: Fear & Greed history.
- `signals/providers/coinmetrics.py`: community on-chain metrics.
- `signals/providers/local.py`: immutable archives and QT-intel adapter.
- `signals/providers/generic_http.py`: authenticated JSON mapping.
- `signals/webhook.py`: signed inbound payload verification.
- `signals/ranking.py`: normalization, decay, consensus, attribution.
- `signals/calibration.py`: out-of-sample reliability updates.
- `tests/signals/`: model, provider, archive, ranking, no-leak, and integration tests.

### Task 1: Signal Models And Provider Protocol

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/signals/models.py`
- Create: `packages/btc-backtest/src/btc_backtest/signals/base.py`
- Create: `packages/btc-backtest/tests/signals/test_models.py`
- Create: `packages/btc-backtest/tests/signals/test_provider_contract.py`

**Interfaces:**
- Produces: `SignalObservation`, `SignalQuery`, `RankedSignal`,
  `SignalProviderMetadata`, `SignalProvider`, `SignalProviderRegistry`.
- Produces: `SignalProvider.fetch(query) -> tuple[SignalObservation, ...]`.

- [ ] **Step 1: Write failing model and contract tests**

```python
def test_observation_requires_point_in_time_bounds() -> None:
    with pytest.raises(ValidationError):
        SignalObservation(
            id="x", source_event_id="1", provider="fixture", source_type="sentiment",
            symbol="BTC/USD", horizon="1d", direction=1.1, confidence=0.5,
            observed_at=utc("2024-01-02"), effective_at=utc("2024-01-01"),
            expires_at=utc("2024-01-03"), provenance="fixture://1",
            payload_sha256="0" * 64,
        )


def test_registry_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        SignalProviderRegistry([FixtureProvider(), FixtureProvider()])
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_models.py packages/btc-backtest/tests/signals/test_provider_contract.py -q
```

Expected: import failures for signal models.

- [ ] **Step 3: Implement frozen models and protocol**

Use frozen Pydantic models. Validate timezone-aware UTC timestamps,
`effective_at <= expires_at` and `observed_at <= expires_at`, finite numeric
values, lowercase provider IDs, 64-character lowercase SHA-256, non-empty
provenance, and immutable quality flags. `SignalQuery` has closed-open
`start`, `end`, symbol, horizons, and `require_historical`.

- [ ] **Step 4: Run model/contract tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_models.py packages/btc-backtest/tests/signals/test_provider_contract.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): define normalized signal contract"
git push origin codex/quantdinger-platform-upgrade
```

### Task 2: Immutable Point-In-Time Signal Store

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/store.py`
- Create: `packages/btc-backtest/tests/signals/test_store.py`
- Create: `packages/btc-backtest/tests/signals/test_signal_no_lookahead.py`

**Interfaces:**
- Produces: `SignalStore(root: Path)`.
- Produces: `append(observations) -> str` returning archive fingerprint.
- Produces: `query(query, available_at) -> tuple[SignalObservation, ...]`.

- [ ] **Step 1: Write failing deduplication/availability tests**

```python
def test_store_deduplicates_by_provider_and_source_event(tmp_path) -> None:
    store = SignalStore(tmp_path)
    first = observation(id="a", source_event_id="source-1")
    replay = observation(id="b", source_event_id="source-1")
    store.append((first, replay))
    assert store.query(full_query(), available_at=utc("2024-02-01")) == (first,)


def test_store_hides_late_observation_from_earlier_bar(tmp_path) -> None:
    store = SignalStore(tmp_path)
    late = observation(
        effective_at=utc("2024-01-01"), observed_at=utc("2024-01-05")
    )
    store.append((late,))
    assert store.query(full_query(), available_at=utc("2024-01-04")) == ()
    assert store.query(full_query(), available_at=utc("2024-01-05")) == (late,)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_store.py packages/btc-backtest/tests/signals/test_signal_no_lookahead.py -q
```

Expected: import failure for `SignalStore`.

- [ ] **Step 3: Implement atomic append and point-in-time queries**

Key identity is `(provider, source_event_id)`. Identical payload replay is
idempotent; conflicting payload hashes raise `DataValidationError`. Merge into
a sorted Parquet snapshot under a lock, write atomically, and store a manifest
fingerprint. Queries filter `observed_at <= available_at`, query interval,
symbol, horizon, and `expires_at > available_at`.

- [ ] **Step 4: Run archive/no-leak tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_store.py packages/btc-backtest/tests/signals/test_signal_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: replay, conflict, expiry, and late-observation tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): archive point-in-time signals"
git push origin codex/quantdinger-platform-upgrade
```

### Task 3: Binance Derivatives Signal Provider

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/providers/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/signals/providers/binance.py`
- Create: `packages/btc-backtest/tests/signals/test_binance_provider.py`

**Interfaces:**
- Produces: `BinanceDerivativesSignalProvider(client: httpx.Client)`.
- Emits source types: `funding`, `open_interest`, `long_short_ratio`,
  `taker_flow`.

- [ ] **Step 1: Write failing normalization tests**

```python
def test_funding_rate_maps_to_bounded_direction(httpx_mock) -> None:
    httpx_mock.add_response(json=[
        {"symbol": "BTCUSDT", "fundingTime": 1704067200000, "fundingRate": "0.0005"}
    ])
    observations = BinanceDerivativesSignalProvider(httpx.Client()).fetch(query())
    assert observations[0].source_type == "funding"
    assert observations[0].direction == Decimal("-1")
    assert observations[0].observed_at == utc("2024-01-01")


def test_binance_provider_rejects_future_observed_timestamp(httpx_mock) -> None:
    httpx_mock.add_response(json=[funding_payload("2025-01-01")])
    with pytest.raises(ProviderError, match="outside query"):
        BinanceDerivativesSignalProvider(httpx.Client()).fetch(query_2024())
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_binance_provider.py -q
```

Expected: import failure for Binance signal provider.

- [ ] **Step 3: Implement explicit endpoint mappers**

Map official public funding, open-interest history, global long/short account
ratio, and taker buy/sell ratio payloads separately. Each mapper declares a
normalization scale and contrarian/trend direction rule in metadata. Preserve
raw value and hash the canonical raw event. Use bounded retries, pagination,
strict timestamp checks, and no API key.

- [ ] **Step 4: Run provider tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_binance_provider.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: four source mappings, pagination, bounds, and failure paths pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): ingest Binance derivative signals"
git push origin codex/quantdinger-platform-upgrade
```

### Task 4: Fear/Greed And Coin Metrics Providers

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/providers/alternative.py`
- Create: `packages/btc-backtest/src/btc_backtest/signals/providers/coinmetrics.py`
- Create: `packages/btc-backtest/tests/signals/test_alternative_provider.py`
- Create: `packages/btc-backtest/tests/signals/test_coinmetrics_provider.py`

**Interfaces:**
- Produces: `FearGreedSignalProvider`.
- Produces: `CoinMetricsSignalProvider(metrics: Mapping[str, MetricRule])`.
- Emits source types: `sentiment` and `onchain`.

- [ ] **Step 1: Write failing historical/availability tests**

```python
def test_fear_greed_maps_extremes_and_timestamp(httpx_mock) -> None:
    httpx_mock.add_response(json={"data": [
        {"value": "10", "timestamp": "1704067200", "time_until_update": "0"}
    ]})
    item = FearGreedSignalProvider(httpx.Client()).fetch(query())[0]
    assert item.direction == Decimal("0.8")
    assert item.source_type == "sentiment"


def test_coinmetrics_uses_response_time_as_observed_when_status_time_missing(httpx_mock) -> None:
    httpx_mock.add_response(
        headers={"Date": "Tue, 02 Jan 2024 00:00:00 GMT"},
        json={"data": [{"asset": "btc", "time": "2024-01-01T00:00:00Z", "PriceUSD": "42000"}]},
    )
    item = provider().fetch(query())[0]
    assert item.observed_at == utc("2024-01-02")
    assert "delayed_observation" in item.quality_flags
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_alternative_provider.py packages/btc-backtest/tests/signals/test_coinmetrics_provider.py -q
```

Expected: import failures for both providers.

- [ ] **Step 3: Implement explicit metric rules**

Fear/Greed direction is contrarian `(50 - value) / 50`, clipped to `[-1, 1]`.
Coin Metrics accepts only allowlisted metric rules defining source field,
transform, direction, horizon, and expiry. Use metric status-time as
`observed_at` when present; otherwise mark delayed availability using response
time so historical backtests cannot assume same-day publication.

- [ ] **Step 4: Run both provider suites**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_alternative_provider.py packages/btc-backtest/tests/signals/test_coinmetrics_provider.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: mapping, pagination, availability, and malformed-response tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add sentiment and on-chain signals"
git push origin codex/quantdinger-platform-upgrade
```

### Task 5: Local Archives, QT Intel, And Generic JSON

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/providers/local.py`
- Create: `packages/btc-backtest/src/btc_backtest/signals/providers/generic_http.py`
- Create: `packages/btc-backtest/tests/signals/test_local_provider.py`
- Create: `packages/btc-backtest/tests/signals/test_generic_http_provider.py`

**Interfaces:**
- Produces: `SignalArchiveProvider(path: Path)`.
- Produces: `QTIntelArchiveProvider(path: Path)`.
- Produces: `GenericJSONSignalProvider(config: JSONProviderConfig, client)`.

- [ ] **Step 1: Write failing schema/credential tests**

```python
def test_qt_intel_archive_maps_ranked_finding(tmp_path) -> None:
    path = write_qt_intel(tmp_path, score=0.75, observed_at="2024-01-02T00:00:00Z")
    item = QTIntelArchiveProvider(path).fetch(query())[0]
    assert item.provider == "qt_intel"
    assert item.direction == Decimal("0.75")


def test_generic_json_uses_header_secret_without_serializing_it(httpx_mock) -> None:
    provider = GenericJSONSignalProvider(config_with_bearer("secret"), httpx.Client())
    httpx_mock.add_response(json=[generic_event()])
    item = provider.fetch(query())[0]
    assert "secret" not in item.model_dump_json()
    assert httpx_mock.get_request().headers["Authorization"] == "Bearer secret"
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_local_provider.py packages/btc-backtest/tests/signals/test_generic_http_provider.py -q
```

Expected: import failures for local/generic providers.

- [ ] **Step 3: Implement immutable mapping configs**

Local archives require explicit columns for source event, effective,
observed, expiry, direction, confidence, horizon, and provenance. QT intel
maps its known finding schema without importing QT. Generic JSON uses
allowlisted HTTPS URLs, declarative dotted field paths, environment-resolved
headers, bounded pagination, and rejects missing availability fields.

- [ ] **Step 4: Run local/generic suites and package boundary**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_local_provider.py packages/btc-backtest/tests/signals/test_generic_http_provider.py packages/btc-backtest/tests/test_package_boundary.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: schema, secret redaction, URL allowlist, and no-QT-import tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): ingest archived and JSON signals"
git push origin codex/quantdinger-platform-upgrade
```

### Task 6: Signed Webhook Verification

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/webhook.py`
- Create: `packages/btc-backtest/tests/signals/test_webhook.py`

**Interfaces:**
- Produces: `WebhookVerifier(secret, max_age_seconds=300)`.
- Produces: `verify(body: bytes, timestamp: str, signature: str, now) -> SignalObservation`.

- [ ] **Step 1: Write failing replay/signature tests**

```python
def test_valid_webhook_maps_observation() -> None:
    body = canonical_webhook_body()
    signature = sign("secret", "1704067200", body)
    item = WebhookVerifier(b"secret").verify(
        body, "1704067200", signature, now=utc("2024-01-01T00:01:00Z")
    )
    assert item.provider == "webhook"


@pytest.mark.parametrize("timestamp", ["1704060000", "1704070000"])
def test_webhook_rejects_stale_or_future_timestamp(timestamp) -> None:
    with pytest.raises(ProviderError, match="timestamp"):
        verifier().verify(body(), timestamp, sign_for(timestamp), now=utc("2024-01-01"))
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_webhook.py -q
```

Expected: import failure for `WebhookVerifier`.

- [ ] **Step 3: Implement constant-time HMAC verification**

Signature payload is `timestamp + b"." + body`; compare HMAC-SHA256 with
`hmac.compare_digest`. Enforce maximum age and bounded clock skew, strict JSON
size/schema, HTTPS provenance, declared `observed_at`, and payload hash.
Duplicate/replay handling remains the store's source-event responsibility.

- [ ] **Step 4: Run webhook suite**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_webhook.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: valid, invalid signature, stale, future, oversized, and malformed tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): verify signed signal webhooks"
git push origin codex/quantdinger-platform-upgrade
```

### Task 7: Consensus Ranking And Attribution

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/ranking.py`
- Create: `packages/btc-backtest/tests/signals/test_ranking.py`
- Create: `packages/btc-backtest/tests/signals/test_ranking_properties.py`

**Interfaces:**
- Produces: `RankingConfig`.
- Produces: `SignalAggregator.rank(observations, as_of) -> tuple[RankedSignal, ...]`.

- [ ] **Step 1: Write failing formula/attribution tests**

```python
def test_consensus_applies_reliability_confidence_and_decay() -> None:
    observations = (
        observation("a", direction=1, confidence=0.8, observed_hours_ago=0),
        observation("b", direction=-1, confidence=0.5, observed_hours_ago=24),
    )
    ranked = SignalAggregator(
        RankingConfig(reliability={"a": 0.75, "b": 0.50}, half_life_hours=24)
    ).rank(observations, as_of=NOW)
    expected = (1 * 0.8 * 0.75 - 1 * 0.5 * 0.50 * 0.5) / (0.8 * 0.75 + 0.5 * 0.50 * 0.5)
    assert ranked[0].direction == pytest.approx(expected)
    assert {item.provider for item in ranked[0].contributors} == {"a", "b"}


@given(observation_sets())
def test_ranked_direction_and_confidence_are_bounded(items) -> None:
    for ranked in aggregator().rank(items, as_of=NOW):
        assert -1 <= ranked.direction <= 1
        assert 0 <= ranked.confidence <= 1
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_ranking.py packages/btc-backtest/tests/signals/test_ranking_properties.py -q
```

Expected: import failure for ranking.

- [ ] **Step 3: Implement deterministic grouping/ranking**

Reject expired/future-known observations, group by symbol/horizon, deduplicate
source event IDs, compute weight `reliability * confidence * 2**(-age/half_life)`,
calculate weighted direction, apply disagreement penalty to confidence, and
sort by absolute direction then confidence then stable key. Preserve each
contributor's ID, provider, weight, and provenance.

- [ ] **Step 4: Run ranking and no-look-ahead tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_ranking.py packages/btc-backtest/tests/signals/test_ranking_properties.py packages/btc-backtest/tests/signals/test_signal_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: formula, bounds, deterministic ordering, expiry, and attribution pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): rank attributed signal consensus"
git push origin codex/quantdinger-platform-upgrade
```

### Task 8: Out-Of-Sample Reliability Calibration

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/signals/calibration.py`
- Create: `packages/btc-backtest/tests/signals/test_calibration.py`

**Interfaces:**
- Produces: `CalibrationWindow`, `ProviderOutcome`, `ReliabilitySnapshot`.
- Produces: `ReliabilityCalibrator.update(previous, completed_window)`.

- [ ] **Step 1: Write failing leakage/calibration tests**

```python
def test_completed_window_updates_beta_prior() -> None:
    previous = ReliabilitySnapshot(provider="a", alpha=2, beta=2, through=utc("2023-12-31"))
    outcomes = completed_window("2024-01", wins=3, losses=1)
    updated = ReliabilityCalibrator().update(previous, outcomes)
    assert updated.alpha == 5 and updated.beta == 3
    assert updated.reliability == pytest.approx(5 / 8)


def test_window_cannot_calibrate_itself() -> None:
    with pytest.raises(ValueError, match="completed before"):
        ReliabilityCalibrator().weights_for(
            snapshots=(snapshot(through="2024-01-31"),),
            window_start=utc("2024-01-01"),
        )
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_calibration.py -q
```

Expected: import failure for calibration.

- [ ] **Step 3: Implement Bayesian reliability snapshots**

Use a Beta prior with configurable `alpha0=2`, `beta0=2`; score an observation
only after its horizon closes; update immutable monthly snapshots; require
snapshot `through < evaluation_window.start`; clamp configured fallback priors
to `[0.1, 0.9]`; record sample count and source fingerprint.

- [ ] **Step 4: Run calibration and ranking suites**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_calibration.py packages/btc-backtest/tests/signals/test_ranking.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: prior, window isolation, horizon completion, and deterministic snapshots pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): calibrate signal reliability out of sample"
git push origin codex/quantdinger-platform-upgrade
```

### Task 9: Strategy Context, Public API, And Signal CLI

**Files:**
- Modify: `packages/btc-backtest/src/btc_backtest/strategies/base.py`
- Modify: `packages/btc-backtest/src/btc_backtest/engine/runner.py`
- Modify: `packages/btc-backtest/src/btc_backtest/api.py`
- Modify: `packages/btc-backtest/src/btc_backtest/cli.py`
- Create: `packages/btc-backtest/tests/signals/test_strategy_integration.py`
- Modify: `packages/btc-backtest/tests/test_cli.py`

**Interfaces:**
- Produces: `StrategyContext.signals: tuple[RankedSignal, ...]`.
- Produces: CLI `signals collect` and `signals top`.

- [ ] **Step 1: Write failing strategy and CLI integration tests**

```python
def test_strategy_sees_only_declared_point_in_time_signals(dataset, store, strategy) -> None:
    store.append((early_signal(), late_signal()))
    result = runner(store=store).run(spec(), strategy=strategy)
    assert result.orders[0].signal_ids == (early_signal().id,)
    assert late_signal().id not in result.orders[0].signal_ids


def test_signals_top_cli_includes_provenance(cli_runner, signal_archive) -> None:
    result = cli_runner.invoke(app, ["signals", "top", "--archive", str(signal_archive), "--json"])
    assert result.exit_code == 0
    assert '"contributors"' in result.stdout
    assert '"provenance"' in result.stdout
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals/test_strategy_integration.py packages/btc-backtest/tests/test_cli.py -q
```

Expected: missing context field and CLI commands fail.

- [ ] **Step 3: Inject declared signals and add commands**

Before each strategy call, query the store at active bar timestamp, rank only
the strategy metadata's declared horizons/source types, and attach immutable
ranked values. Copy contributing observation IDs to every resulting order.
`signals collect` resolves configured providers into the store; `signals top`
queries/ranks at `--as-of` and emits table or JSON.

- [ ] **Step 4: Run signal exit gate**

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals packages/btc-backtest/tests/engine/test_no_lookahead.py packages/btc-backtest/tests/test_cli.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
git diff --check
```

Expected: all signal provider, store, ranking, calibration, strategy, and CLI tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): integrate ranked signals with strategies"
git push origin codex/quantdinger-platform-upgrade
```

## Signal Exit Gate

Run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/signals -q
.venv/bin/pytest packages/btc-backtest/tests/engine/test_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
btc-backtest signals top --archive packages/btc-backtest/tests/fixtures/signals.parquet --as-of 2024-01-31T00:00:00Z --json
git diff --check
git status --short --branch
```

Evidence required:

- at least five source types normalize successfully;
- live-only and late observations are historically gated;
- ranking is bounded, deterministic, and fully attributed;
- reliability cannot train on its evaluation window;
- strategy orders identify every contributing observation;
- current branch is committed, pushed, and clean.
