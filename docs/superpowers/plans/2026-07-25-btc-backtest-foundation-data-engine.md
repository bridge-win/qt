# BTC Backtest Foundation, Data, And Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the independently installable package, validated real-data pipeline, deterministic execution engine, custom strategy SDK, public runner, and foundation CLI.

**Architecture:** `btc_backtest` owns immutable models and provider/strategy protocols; real providers normalize into one `MarketDataset`; the event engine consumes only point-in-time bars and emits orders, fills, snapshots, and a deterministic `BacktestResult`. The package has no QT imports.

**Tech Stack:** Python 3.10+, setuptools, pandas, NumPy, PyArrow, Pydantic, HTTPX, Typer, Rich, pytest, Hypothesis, Ruff, strict Mypy

## Global Constraints

- `btc_backtest` must never import `qt`.
- Real data is the default; synthetic data requires an explicit flag and label.
- All timestamps are timezone-aware UTC and intervals are closed-open.
- Provider output must include provenance, raw hashes, normalized fingerprint, and gap information.
- Partial or invalid real data is a terminal error.
- Cache publication is atomic.
- The engine is deterministic and uses conservative adverse-first intrabar ordering.
- Spot inventory cannot be negative and orders cannot spend unavailable cash.
- Every task ends with focused tests, `git diff --check`, a commit, and a push.

---

## File Map

- `packages/btc-backtest/pyproject.toml`: independent distribution and CLI metadata.
- `packages/btc-backtest/src/btc_backtest/__init__.py`: stable public exports and version.
- `packages/btc-backtest/src/btc_backtest/errors.py`: typed package exceptions.
- `packages/btc-backtest/src/btc_backtest/data/models.py`: data requests, manifests, datasets.
- `packages/btc-backtest/src/btc_backtest/data/validation.py`: OHLCV and coverage validation.
- `packages/btc-backtest/src/btc_backtest/data/cache.py`: atomic Parquet cache.
- `packages/btc-backtest/src/btc_backtest/data/providers/base.py`: provider protocol and registry.
- `packages/btc-backtest/src/btc_backtest/data/providers/composite.py`: explicit multi-source segments.
- `packages/btc-backtest/src/btc_backtest/data/providers/synthetic.py`: opt-in labeled fixtures.
- `packages/btc-backtest/src/btc_backtest/data/providers/bitstamp.py`: paginated public OHLC.
- `packages/btc-backtest/src/btc_backtest/data/providers/binance_archive.py`: checksummed archives.
- `packages/btc-backtest/src/btc_backtest/data/providers/ccxt.py`: optional recent exchange data.
- `packages/btc-backtest/src/btc_backtest/data/providers/local.py`: immutable Parquet input.
- `packages/btc-backtest/src/btc_backtest/engine/models.py`: specs, order/fill/result models.
- `packages/btc-backtest/src/btc_backtest/engine/accounting.py`: portfolio state transitions.
- `packages/btc-backtest/src/btc_backtest/engine/fills.py`: deterministic bar fill policy.
- `packages/btc-backtest/src/btc_backtest/engine/runner.py`: point-in-time event loop.
- `packages/btc-backtest/src/btc_backtest/strategies/base.py`: custom strategy protocol.
- `packages/btc-backtest/src/btc_backtest/strategies/loader.py`: explicit and entry-point loading.
- `packages/btc-backtest/src/btc_backtest/api.py`: public orchestration.
- `packages/btc-backtest/src/btc_backtest/cli.py`: initial data/run/strategy commands.
- `packages/btc-backtest/examples/custom_strategy.py`: reference external strategy.
- `packages/btc-backtest/tests/`: package-local unit, property, contract, and integration tests.

### Task 1: Independent Package And Public Skeleton

**Files:**
- Create: `packages/btc-backtest/pyproject.toml`
- Create: `packages/btc-backtest/src/btc_backtest/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/errors.py`
- Create: `packages/btc-backtest/tests/test_package_boundary.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: package version `btc_backtest.__version__ == "0.1.0"`.
- Produces: `BacktestError`, `DataValidationError`, `DataCoverageError`,
  `ProviderError`, `StrategyLoadError`, and `ExecutionError`.

- [ ] **Step 1: Write the failing package-boundary test**

```python
from importlib.metadata import version
from pathlib import Path

import btc_backtest


def test_independent_distribution_exports_version() -> None:
    assert btc_backtest.__version__ == "0.1.0"
    assert version("btc-backtest") == "0.1.0"


def test_independent_package_never_imports_qt() -> None:
    root = Path(btc_backtest.__file__).parent
    sources = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert "from qt" not in sources
    assert "import qt" not in sources
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/test_package_boundary.py -q
```

Expected: collection fails because `btc_backtest` is not installed.

- [ ] **Step 3: Add the minimal distribution**

`pyproject.toml` must declare:

```toml
[build-system]
requires = ["setuptools==80.9.0", "wheel==0.45.1"]
build-backend = "setuptools.build_meta"

[project]
name = "btc-backtest"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
  "httpx>=0.27",
  "numpy>=1.26",
  "pandas>=2.1",
  "pyarrow>=14",
  "pydantic>=2.5",
  "rich>=13.7",
  "scipy>=1.11",
  "typer>=0.12",
]

[project.optional-dependencies]
dev = [
  "build>=1.2.2",
  "hypothesis>=6.100",
  "mypy>=1.10",
  "pytest>=8",
  "pytest-httpx>=0.30",
  "ruff>=0.4",
]
exchanges = ["ccxt>=4.2"]

[project.scripts]
btc-backtest = "btc_backtest.cli:app"

[tool.setuptools.packages.find]
where = ["src"]
```

Add typed exceptions with no behavior beyond clear inheritance. Add cache,
artifact, coverage, and build directories under `packages/btc-backtest` to
`.gitignore`.

- [ ] **Step 4: Install editable and verify GREEN**

Run:

```bash
.venv/bin/pip install -e 'packages/btc-backtest[dev]'
.venv/bin/pytest packages/btc-backtest/tests/test_package_boundary.py -q
.venv/bin/python -m build packages/btc-backtest
git diff --check
```

Expected: tests pass and both wheel and source archive build.

- [ ] **Step 5: Commit and push**

```bash
git add .gitignore packages/btc-backtest
git commit -m "feat(backtest): create independent package"
git push origin codex/quantdinger-platform-upgrade
```

### Task 2: Immutable Data Models And OHLCV Validation

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/data/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/data/models.py`
- Create: `packages/btc-backtest/src/btc_backtest/data/validation.py`
- Create: `packages/btc-backtest/tests/data/test_validation.py`

**Interfaces:**
- Produces: `DataRequest`, `DataManifest`, `DataSegment`, `DataGap`,
  `MarketDataset`, and `MarketBundle`.
- Produces: `validate_ohlcv(frame, request) -> tuple[pd.DataFrame, tuple[DataGap, ...]]`.
- Produces: `frame_fingerprint(frame) -> str`.

- [ ] **Step 1: Write failing validation tests**

```python
from datetime import datetime, timezone

import pandas as pd
import pytest

from btc_backtest.data.models import DataRequest
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import DataCoverageError, DataValidationError

UTC = timezone.utc


def request() -> DataRequest:
    return DataRequest(
        provider="fixture",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 4, tzinfo=UTC),
        require_complete=True,
    )


def test_validation_normalizes_numeric_ohlcv_and_fingerprints() -> None:
    index = pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC")
    frame = pd.DataFrame(
        {"open": ["10", "11", "12"], "high": [12, 13, 14], "low": [9, 10, 11],
         "close": [11, 12, 13], "volume": [1, 2, 3]},
        index=index,
    )
    normalized, gaps = validate_ohlcv(frame, request())
    assert normalized.dtypes.tolist() == ["float64"] * 5
    assert gaps == ()
    assert frame_fingerprint(normalized) == frame_fingerprint(normalized.copy())


def test_validation_rejects_missing_required_bar() -> None:
    index = pd.to_datetime(["2024-01-01", "2024-01-03"], utc=True)
    frame = pd.DataFrame(
        {"open": [10, 12], "high": [12, 14], "low": [9, 11],
         "close": [11, 13], "volume": [1, 3]},
        index=index,
    )
    with pytest.raises(DataCoverageError, match="2024-01-02"):
        validate_ohlcv(frame, request())
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_validation.py -q
```

Expected: import failure for `btc_backtest.data.models`.

- [ ] **Step 3: Implement models and validation**

Use frozen Pydantic models:

```python
class DataRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: str
    symbol: str
    timeframe: Literal["1h", "1d"]
    start: datetime
    end: datetime
    market: str = "spot"
    require_real: bool = True
    require_complete: bool = True
    max_missing_ratio: float = Field(default=0.0, ge=0.0, le=1.0)


class DataManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal["1"] = "1"
    provider: str
    market: str
    symbol: str
    timeframe: Literal["1h", "1d"]
    requested_start: datetime
    requested_end: datetime
    delivered_start: datetime
    delivered_end: datetime
    retrieved_at: datetime
    real_data: bool
    raw_sha256: tuple[str, ...]
    normalized_sha256: str
    gaps: tuple[DataGap, ...] = ()
    segments: tuple[DataSegment, ...] = ()


@dataclass(frozen=True)
class MarketDataset:
    frame: pd.DataFrame
    manifest: DataManifest


@dataclass(frozen=True)
class MarketBundle:
    primary: MarketDataset
    auxiliary: Mapping[str, MarketDataset]
```

Validate UTC conversion, closed-open bounds, unique ascending timestamps,
timeframe alignment, finite numeric values, OHLC bounds, non-negative volume,
gap ratio, and exact coverage. Fingerprint the normalized five-column CSV
representation with SHA-256.

- [ ] **Step 4: Run focused and property tests**

Add a Hypothesis test generating valid daily bars, then run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_validation.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): validate immutable market datasets"
git push origin codex/quantdinger-platform-upgrade
```

### Task 3: Atomic Parquet Cache

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/data/cache.py`
- Create: `packages/btc-backtest/tests/data/test_cache.py`

**Interfaces:**
- Consumes: `DataRequest`, `DataManifest`, `MarketDataset`, `validate_ohlcv`.
- Produces: `DataCache(root: Path)`.
- Produces: `DataCache.load(request) -> MarketDataset | None`.
- Produces: `DataCache.publish(request, dataset) -> Path`.

- [ ] **Step 1: Write failing atomic-cache tests**

```python
def test_publish_round_trips_validated_dataset(tmp_path, dataset, request) -> None:
    cache = DataCache(tmp_path)
    path = cache.publish(request, dataset)
    loaded = cache.load(request)
    assert path.suffix == ".parquet"
    assert loaded is not None
    assert loaded.manifest.normalized_sha256 == dataset.manifest.normalized_sha256
    pd.testing.assert_frame_equal(loaded.frame, dataset.frame)


def test_failed_publication_keeps_previous_entry(tmp_path, dataset, request, monkeypatch) -> None:
    cache = DataCache(tmp_path)
    cache.publish(request, dataset)
    monkeypatch.setattr(Path, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        cache.publish(request, dataset)
    assert cache.load(request) is not None
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_cache.py -q
```

Expected: import failure for `DataCache`.

- [ ] **Step 3: Implement cache identity and atomic publication**

Derive the request key from canonical provider, market, symbol, timeframe,
start, end, and schema version JSON. Write Parquet and manifest JSON inside a
content-addressed version directory in a `TemporaryDirectory`, fsync both, then
atomically rename the new version and atomically replace a small request-key
pointer file. Existing version directories are immutable. Validate the
fingerprint on every load and raise `DataValidationError` for corrupt entries.

- [ ] **Step 4: Run cache and data tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add atomic market data cache"
git push origin codex/quantdinger-platform-upgrade
```

### Task 4: Provider Protocol, Registry, And Local Parquet

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/base.py`
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/local.py`
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/composite.py`
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/synthetic.py`
- Create: `packages/btc-backtest/tests/data/test_providers.py`

**Interfaces:**
- Produces: `ProviderMetadata`, `MarketDataProvider`, and `ProviderRegistry`.
- Produces: `LocalParquetProvider(path: Path)`.
- Produces: `CompositeProvider(providers, overlap_tolerance)`.
- Produces: `SyntheticProvider(seed: int)`.
- Produces: `ProviderRegistry.fetch(request, cache) -> MarketDataset`.

- [ ] **Step 1: Write failing registry contract tests**

```python
class FixtureProvider:
    metadata = ProviderMetadata(id="fixture", real_data=True, timeframes=("1d",))

    def fetch(self, request: DataRequest) -> MarketDataset:
        return make_dataset(request)


def test_registry_caches_provider_result(tmp_path, request) -> None:
    registry = ProviderRegistry([FixtureProvider()])
    cache = DataCache(tmp_path)
    first = registry.fetch(request, cache)
    second = registry.fetch(request, cache)
    assert first.manifest.normalized_sha256 == second.manifest.normalized_sha256


def test_registry_rejects_unknown_provider(request, tmp_path) -> None:
    with pytest.raises(ProviderError, match="unknown provider"):
        ProviderRegistry([]).fetch(request, DataCache(tmp_path))


def test_registry_rejects_synthetic_for_real_request(tmp_path, request) -> None:
    registry = ProviderRegistry([SyntheticProvider(seed=7)])
    with pytest.raises(ProviderError, match="requires real data"):
        registry.fetch(request, DataCache(tmp_path))


def test_composite_never_silently_combines_usd_and_usdt() -> None:
    with pytest.raises(DataValidationError, match="symbol"):
        CompositeProvider([btc_usd_provider(), btc_usdt_provider()]).fetch(request())
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_providers.py -q
```

Expected: import failure for provider types.

- [ ] **Step 3: Implement protocol, registry, and local provider**

The registry checks provider ID, timeframe, market, `require_real`, and
real-data capability, uses the cache when valid, fetches otherwise, validates
the returned request identity, and publishes atomically.
`LocalParquetProvider` hashes the raw file, loads it without mutation, and
emits a `DataManifest` with a resolved local path. `CompositeProvider` requires
identical symbol/market/timeframe, rejects conflicting overlaps beyond the
tolerance, and records each `DataSegment`; `SyntheticProvider` is deterministic
and sets `real_data=False`.

- [ ] **Step 4: Run provider contract suite**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): define market data provider contract"
git push origin codex/quantdinger-platform-upgrade
```

### Task 5: Paginated Bitstamp Real Data Provider

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/bitstamp.py`
- Create: `packages/btc-backtest/tests/data/test_bitstamp.py`
- Create: `packages/btc-backtest/tests/integration/test_bitstamp_live.py`

**Interfaces:**
- Consumes: provider protocol, `DataRequest`, `MarketDataset`, validation.
- Produces: `BitstampProvider(client: httpx.Client, page_size: int = 1000)`.
- Produces: daily/hourly `BTC/USD` datasets with raw page hashes.

- [ ] **Step 1: Write failing pagination/parser tests**

```python
def test_bitstamp_paginates_closed_open_interval(httpx_mock) -> None:
    request = daily_request("2024-01-01", "2024-01-04")
    httpx_mock.add_response(json=bitstamp_payload(["2024-01-01", "2024-01-02"]))
    httpx_mock.add_response(json=bitstamp_payload(["2024-01-03"]))
    dataset = BitstampProvider(httpx.Client(), page_size=2).fetch(request)
    assert dataset.frame.index.tolist() == list(
        pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC")
    )
    assert len(dataset.manifest.raw_sha256) == 2


def test_bitstamp_rejects_cursor_stall(httpx_mock) -> None:
    request = daily_request("2024-01-01", "2024-01-04")
    page = bitstamp_payload(["2024-01-01", "2024-01-02"])
    httpx_mock.add_response(json=page)
    httpx_mock.add_response(json=page)
    with pytest.raises(ProviderError, match="cursor"):
        BitstampProvider(httpx.Client(), page_size=2).fetch(request)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_bitstamp.py -q
```

Expected: import failure for `BitstampProvider`.

- [ ] **Step 3: Implement bounded pagination**

Map `1h -> 3600`, `1d -> 86400`. Request
`GET https://www.bitstamp.net/api/v2/ohlc/btcusd/` with `step`, `limit`,
`start`, `end`, and `exclude_current_candle=true`. Use a closed-open cursor,
bounded exponential retry for transport/429/5xx errors, raw response hashes,
strict cursor progress, deduplication validation, and no secret-bearing logs.

- [ ] **Step 4: Run offline tests and bounded live probe**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_bitstamp.py -q
.venv/bin/pytest packages/btc-backtest/tests/integration/test_bitstamp_live.py -q -m integration
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

The live test requests three daily bars, asserts exact timestamps and real-data
provenance, and skips only on an explicit network-unavailable exception.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): fetch paginated Bitstamp history"
git push origin codex/quantdinger-platform-upgrade
```

### Task 6: Checksummed Binance Archive Provider

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/binance_archive.py`
- Create: `packages/btc-backtest/tests/data/test_binance_archive.py`
- Create: `packages/btc-backtest/tests/integration/test_binance_archive_live.py`

**Interfaces:**
- Produces: `BinanceArchiveProvider(client: httpx.Client)`.
- Produces: normalized spot `BTC/USDT` archive data and verified raw hashes.

- [ ] **Step 1: Write failing checksum and timestamp tests**

```python
def test_archive_verifies_checksum_and_normalizes_microseconds(httpx_mock) -> None:
    archive = make_kline_zip(timestamp=1_735_689_600_000_000)
    checksum = hashlib.sha256(archive).hexdigest()
    httpx_mock.add_response(content=archive)
    httpx_mock.add_response(text=f"{checksum}  BTCUSDT-1d-2025-01.zip\n")
    dataset = BinanceArchiveProvider(httpx.Client()).fetch(january_2025_request())
    assert dataset.frame.index[0] == pd.Timestamp("2025-01-01", tz="UTC")


def test_archive_rejects_checksum_mismatch(httpx_mock) -> None:
    httpx_mock.add_response(content=make_kline_zip())
    httpx_mock.add_response(text=f"{'0' * 64}  BTCUSDT-1d-2024-01.zip\n")
    with pytest.raises(ProviderError, match="checksum"):
        BinanceArchiveProvider(httpx.Client()).fetch(january_2024_request())
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_binance_archive.py -q
```

Expected: import failure for `BinanceArchiveProvider`.

- [ ] **Step 3: Implement monthly/daily archive selection**

Construct only allowlisted URLs under `https://data.binance.vision/data/`,
prefer complete monthly files, use daily files for incomplete months, fetch the
matching `.CHECKSUM`, compare SHA-256 before unzip, parse 12-column klines,
detect millisecond versus microsecond timestamps, and validate exact coverage.
Reject zip-slip paths and uncompressed payloads over the configured limit.

- [ ] **Step 4: Run offline and one-file live verification**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_binance_archive.py -q
.venv/bin/pytest packages/btc-backtest/tests/integration/test_binance_archive_live.py -q -m integration
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: checksum, timestamp, and archive safety tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): verify Binance public archives"
git push origin codex/quantdinger-platform-upgrade
```

### Task 7: Optional CCXT Recent-Data Provider

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/data/providers/ccxt.py`
- Create: `packages/btc-backtest/tests/data/test_ccxt_provider.py`

**Interfaces:**
- Produces: `CCXTProvider(exchange_id: str, exchange: object | None = None)`.
- Supports only exchanges/timeframes for which `fetchOHLCV` is declared.

- [ ] **Step 1: Write failing pagination/capability tests**

```python
def test_ccxt_provider_paginates_without_cursor_replay() -> None:
    exchange = FixtureExchange(pages=[ohlcv_page(0, 2), ohlcv_page(2, 1), []])
    dataset = CCXTProvider("fixture", exchange=exchange).fetch(hourly_request(3))
    assert len(dataset.frame) == 3
    assert exchange.cursors == sorted(set(exchange.cursors))


def test_ccxt_provider_rejects_exchange_without_ohlcv() -> None:
    with pytest.raises(ProviderError, match="fetchOHLCV"):
        CCXTProvider("fixture", exchange=NoOHLCVExchange()).fetch(hourly_request(1))
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_ccxt_provider.py -q
```

Expected: import failure for `CCXTProvider`.

- [ ] **Step 3: Implement optional dependency and strict pagination**

Import CCXT only inside the constructor and raise an actionable extras-install
error when absent. Check the exchange capability, map the requested timeframe,
advance by the last timestamp plus one timeframe, enforce the closed-open end,
reject cursor stalls/duplicates, normalize through `validate_ohlcv`, and state
in the manifest that retained history is exchange-dependent.

- [ ] **Step 4: Run CCXT/data/provider tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/data/test_ccxt_provider.py packages/btc-backtest/tests/data/test_providers.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: optional-import, capability, pagination, and failure tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add optional CCXT data provider"
git push origin codex/quantdinger-platform-upgrade
```

### Task 8: Engine Models And Portfolio Accounting

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/engine/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/engine/models.py`
- Create: `packages/btc-backtest/src/btc_backtest/engine/accounting.py`
- Create: `packages/btc-backtest/tests/engine/test_accounting.py`
- Create: `packages/btc-backtest/tests/engine/test_accounting_properties.py`

**Interfaces:**
- Produces: `InstrumentKind`, `OrderSide`, `OrderType`, `OrderStatus`,
  `OrderIntent`, `Order`, `Fill`, `Position`, `PortfolioSnapshot`,
  `BacktestSpec`, `BacktestResult`.
- Produces: `Portfolio(initial_cash: Decimal)`.
- Produces: `Portfolio.apply_fill(fill) -> PortfolioSnapshot`.
- Produces: `Portfolio.apply_funding(event) -> PortfolioSnapshot`.
- Produces: `Portfolio.mark(ts, marks: Mapping[str, Decimal]) -> PortfolioSnapshot`.

- [ ] **Step 1: Write failing accounting tests**

```python
def test_buy_then_sell_reconciles_cash_equity_and_realized_pnl() -> None:
    portfolio = Portfolio(Decimal("10000"))
    portfolio.apply_fill(fill("buy", qty="1", price="100", fee="1"))
    opened = portfolio.mark(ts(1), Decimal("110"))
    assert opened.cash == Decimal("9899")
    assert opened.equity == Decimal("10009")
    portfolio.apply_fill(fill("sell", qty="1", price="110", fee="1"))
    closed = portfolio.mark(ts(2), Decimal("110"))
    assert closed.cash == closed.equity == Decimal("10008")
    assert closed.realized_pnl == Decimal("8")


@given(valid_fill_sequences())
def test_accounting_invariants(sequence) -> None:
    portfolio = Portfolio(Decimal("10000"))
    for fill in sequence:
        snapshot = portfolio.apply_fill(fill)
        assert snapshot.cash.is_finite()
        assert snapshot.equity.is_finite()
        assert snapshot.position("spot").quantity >= 0


def test_perpetual_short_marks_pnl_and_funding_once() -> None:
    portfolio = Portfolio(Decimal("10000"))
    portfolio.apply_fill(perpetual_fill("sell", qty="1", price="100", fee="1"))
    marked = portfolio.mark(ts(1), {"spot": Decimal("100"), "perpetual": Decimal("90")})
    assert marked.unrealized_pnl == Decimal("10")
    funded = portfolio.apply_funding(funding_event(amount="2"))
    assert funded.cash == marked.cash + Decimal("2")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/engine/test_accounting.py packages/btc-backtest/tests/engine/test_accounting_properties.py -q
```

Expected: import failure for engine models.

- [ ] **Step 3: Implement decimal accounting**

Use frozen Pydantic event models and `Decimal` for order, fill, fee, cash,
quantity, and realized P&L state. `OrderIntent` includes `instrument`,
`group_id`, and `atomic_group`; `BacktestSpec` includes primary `data`,
`auxiliary_data`, strategy/parameters, costs, fill policy, cash, and seed;
`BacktestResult` contains data manifests, orders, fills, positions, snapshots,
trades, signals, diagnostics, and warnings. Reject non-finite or non-positive
price/qty, spot sells above holdings, directional long perpetual positions,
duplicate fill/funding IDs, and events before order creation. Marked equity is
cash plus spot value plus perpetual unrealized P&L; realized P&L includes entry
and exit fees exactly once.

Freeze these signatures:

```python
class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument: Literal["spot", "perpetual"] = "spot"
    side: OrderSide
    order_type: OrderType
    quote_amount: Decimal | None = None
    base_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    group_id: str | None = None
    atomic_group: bool = False
    reason: str
    signal_ids: tuple[str, ...] = ()


class BacktestSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    strategy: str
    strategy_params: Mapping[str, object] = Field(default_factory=dict)
    data: DataRequest
    auxiliary_data: tuple[DataRequest, ...] = ()
    initial_cash: Decimal = Decimal("10000")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")
    intrabar_policy: Literal["adverse_first"] = "adverse_first"
    seed: int = 7


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    run_id: str
    strategy_id: str
    data_manifests: tuple[DataManifest, ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    snapshots: tuple[PortfolioSnapshot, ...]
    trades: tuple[Trade, ...]
    signal_ids: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
```

- [ ] **Step 4: Run accounting suite**

```bash
.venv/bin/pytest packages/btc-backtest/tests/engine -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: example and property invariants pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add deterministic portfolio accounting"
git push origin codex/quantdinger-platform-upgrade
```

### Task 9: Conservative Bar Fill Model

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/engine/fills.py`
- Create: `packages/btc-backtest/tests/engine/test_fills.py`

**Interfaces:**
- Consumes: `Order`, `Fill`, OHLCV bar.
- Produces: `FillPolicy(fee_bps, slippage_bps, intrabar_policy="adverse_first")`.
- Produces: `BarFillModel.evaluate(order, bar, ts) -> Fill | None`.

- [ ] **Step 1: Write failing fill-order tests**

```python
def test_buy_market_fill_applies_adverse_slippage_and_fee() -> None:
    fill = model(fee_bps=10, slippage_bps=5).evaluate(
        market_buy(qty="1"), bar(open="100", high="105", low="95", close="102"), ts(1)
    )
    assert fill is not None
    assert fill.price == Decimal("100.05")
    assert fill.fee == Decimal("0.10005")


def test_adverse_first_stop_wins_when_target_and_stop_share_bar() -> None:
    order = bracketed_long(stop="90", target="110")
    events = model().evaluate_bracket(
        order, bar(open="100", high="115", low="85", close="105"), ts(1)
    )
    assert events[0].reason == "stop"
    assert events[0].price <= Decimal("90")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/engine/test_fills.py -q
```

Expected: import failure for `BarFillModel`.

- [ ] **Step 3: Implement market, limit, stop, and stop-limit rules**

Fill market orders at bar open plus adverse slippage; fill crossed limits no
worse than the limit; trigger stops at the worse of open gap or stop; require a
post-trigger limit touch for stop-limit; cancel expired orders. Record the
configured intrabar policy in each fill and reject unsupported policy names.

- [ ] **Step 4: Run engine tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/engine -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): simulate conservative bar fills"
git push origin codex/quantdinger-platform-upgrade
```

### Task 10: Custom Strategy Protocol And Loader

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/strategies/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/strategies/base.py`
- Create: `packages/btc-backtest/src/btc_backtest/strategies/loader.py`
- Create: `packages/btc-backtest/examples/custom_strategy.py`
- Create: `packages/btc-backtest/tests/fixtures/custom-plugin/pyproject.toml`
- Create: `packages/btc-backtest/tests/fixtures/custom-plugin/src/example_btc_strategy/__init__.py`
- Create: `packages/btc-backtest/tests/strategies/test_loader.py`
- Create: `packages/btc-backtest/tests/strategies/test_contract.py`

**Interfaces:**
- Produces: `StrategyMetadata`, `InitializationContext`, `StrategyContext`,
  `FinalizationContext`, and runtime-checkable `Strategy`.
- Produces: `load_strategy(reference: str) -> Strategy`.
- Produces: `discover_entry_point_strategies() -> dict[str, Strategy]`.

- [ ] **Step 1: Write failing explicit-load and contract tests**

```python
def test_load_explicit_custom_strategy() -> None:
    strategy = load_strategy("examples/custom_strategy.py:CustomStrategy")
    assert strategy.metadata.id == "custom_sma"
    assert strategy.metadata.api_version == "1"


def test_duplicate_entry_point_ids_fail(monkeypatch) -> None:
    monkeypatch.setattr(loader, "entry_points", duplicate_entry_points)
    with pytest.raises(StrategyLoadError, match="duplicate"):
        discover_entry_point_strategies()


def assert_strategy_contract(strategy: Strategy) -> None:
    assert strategy.metadata.warmup_bars >= 0
    assert strategy.metadata.supported_timeframes
    assert strategy.metadata.api_version == "1"


def test_entry_point_group_is_discoverable(monkeypatch) -> None:
    monkeypatch.setattr(loader, "entry_points", fixture_plugin_entry_points)
    discovered = discover_entry_point_strategies()
    assert discovered["external_fixture"].metadata.api_version == "1"
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_loader.py packages/btc-backtest/tests/strategies/test_contract.py -q
```

Expected: import failure for strategy protocol.

- [ ] **Step 3: Implement read-only contexts and opt-in discovery**

Contexts expose immutable bar history up to the active timestamp, portfolio
snapshot, open orders, and an empty signal tuple until the signal plan.
Explicit loading resolves the file, imports under a unique module name,
constructs the class, validates metadata and protocol, and rejects paths
outside the explicitly supplied file. Entry points use group
`btc_backtest.strategies` and reject duplicate IDs or API versions other than
`"1"`.

- [ ] **Step 4: Run loader and boundary tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/strategies packages/btc-backtest/tests/test_package_boundary.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: custom example and entry-point fixtures pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): expose custom strategy SDK"
git push origin codex/quantdinger-platform-upgrade
```

### Task 11: Point-In-Time Event Runner

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/engine/runner.py`
- Create: `packages/btc-backtest/tests/engine/test_runner.py`
- Create: `packages/btc-backtest/tests/engine/test_no_lookahead.py`

**Interfaces:**
- Consumes: `MarketBundle`, `Strategy`, `BacktestSpec`, `Portfolio`,
  `BarFillModel`.
- Produces: `EventRunner.run(spec, bundle, strategy) -> BacktestResult`.

- [ ] **Step 1: Write failing deterministic and no-look-ahead tests**

```python
def test_runner_is_deterministic(bundle, one_shot_strategy, spec) -> None:
    first = EventRunner().run(spec, bundle, one_shot_strategy)
    second = EventRunner().run(spec, bundle, one_shot_strategy)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_future_bar_mutation_cannot_change_past_events(dataset, strategy, spec) -> None:
    cutoff = dataset.frame.index[50]
    first = EventRunner().run(spec, dataset, strategy)
    mutated = mutate_bars_after(dataset, cutoff)
    second = EventRunner().run(spec, mutated, strategy)
    assert events_through(first, cutoff) == events_through(second, cutoff)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/engine/test_runner.py packages/btc-backtest/tests/engine/test_no_lookahead.py -q
```

Expected: import failure for `EventRunner`.

- [ ] **Step 3: Implement event loop**

Initialize once, align auxiliary data by availability timestamp, expose only
primary/auxiliary rows through the active timestamp in a read-only context,
process previously open orders before new intents, assign deterministic IDs
from run ID plus counters, validate intents, apply fills/funding, mark
portfolio, and finalize once. Validate and fill `atomic_group` intents as an
all-or-none unit. Return frozen tuples of orders/fills/snapshots and every
source data manifest. On strategy error, raise `ExecutionError` with strategy
ID and active timestamp and do not return a partial success result.

- [ ] **Step 4: Run engine, property, and contract tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/engine packages/btc-backtest/tests/strategies -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all pass, including future mutation isolation.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): run point-in-time strategy events"
git push origin codex/quantdinger-platform-upgrade
```

### Task 12: Public Runner And Foundation CLI

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/api.py`
- Create: `packages/btc-backtest/src/btc_backtest/cli.py`
- Create: `packages/btc-backtest/tests/test_api.py`
- Create: `packages/btc-backtest/tests/test_cli.py`
- Modify: `packages/btc-backtest/src/btc_backtest/__init__.py`

**Interfaces:**
- Produces: `BacktestRunner(provider_registry, strategy_registry, cache, engine)`.
- Produces: `BacktestRunner.run(spec, strategy=None) -> BacktestResult`.
- Produces CLI groups `data`, `strategies`, and `run-custom`.

- [ ] **Step 1: Write failing API and CLI tests**

```python
def test_public_runner_fetches_and_executes(registry, cache, strategy, spec) -> None:
    result = BacktestRunner(
        provider_registry=registry.providers,
        strategy_registry=registry.strategies,
        cache=cache,
    ).run(spec, strategy=strategy)
    assert result.data_manifest.provider == spec.data.provider
    assert result.snapshots


def test_run_custom_cli_exports_json(runner: CliRunner, fixture_parquet: Path) -> None:
    result = runner.invoke(
        app,
        ["run-custom", "examples/custom_strategy.py:CustomStrategy",
         "--provider", "local", "--path", str(fixture_parquet)],
    )
    assert result.exit_code == 0
    assert '"strategy_id": "custom_sma"' in result.stdout
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/test_api.py packages/btc-backtest/tests/test_cli.py -q
```

Expected: import failure for public runner/CLI.

- [ ] **Step 3: Implement orchestration and initial commands**

`BacktestRunner` resolves/fetches primary and auxiliary requests into a
`MarketBundle`, resolves or accepts the strategy, validates declared data
requirements, and delegates exactly once to `EventRunner`. CLI commands:

```text
btc-backtest data sync
btc-backtest data inspect
btc-backtest strategies list
btc-backtest run-custom
```

Every command supports `--cache-dir`; errors exit 2 with a concise typed
message. JSON output uses Pydantic JSON mode and never emits credentials.

- [ ] **Step 4: Run package foundation gate**

```bash
.venv/bin/pytest packages/btc-backtest/tests -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
.venv/bin/python -m build packages/btc-backtest
git diff --check
```

Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): expose runner and foundation CLI"
git push origin codex/quantdinger-platform-upgrade
```

## Foundation Exit Gate

Run:

```bash
.venv/bin/pytest packages/btc-backtest/tests -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
.venv/bin/python -m build packages/btc-backtest
.venv/bin/pip install --force-reinstall --no-deps packages/btc-backtest/dist/btc_backtest-0.1.0-py3-none-any.whl
btc-backtest --help
git diff --check
git status --short --branch
```

Evidence required before starting the strategy plan:

- independent wheel installs and imports without QT;
- a real-data provider response validates and caches;
- custom strategy API and CLI execute;
- accounting/property/no-look-ahead tests pass;
- branch is committed, pushed, and clean.
