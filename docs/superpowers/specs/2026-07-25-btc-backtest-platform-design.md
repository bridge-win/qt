# Independent BTC Backtest Platform Design

**Date:** 2026-07-25
**Status:** Approved through continued execution of the persistent objective
**Package:** `btc-backtest` / Python namespace `btc_backtest`

## 1. Objective

Extract BTC backtesting into an independently installable, reproducible package
that QT consumes through a compatibility adapter. The package must:

- run against validated real BTC market data, including genuine ten-year
  histories;
- provide twenty common built-in algorithmic strategies plus QT-specific
  strategies;
- expose a stable SDK for custom strategies;
- normalize and rank internal and external trading signals without scraping
  protected pages;
- model realistic orders, fees, slippage, funding, and portfolio accounting;
- detect look-ahead bias and overfitting through automated validation;
- produce immutable, attributable artifacts that can reproduce every result;
- keep current QT CLI, dashboard, reporting, and platform-job integrations
  working during migration.

The package is a research and simulation system. It does not claim
profitability, automatically promote strategies to live trading, or bypass
QT's risk and paper-trading gates.

## 2. Architectural Decision

Create an independent workspace package:

```text
packages/btc-backtest/
├── pyproject.toml
├── src/btc_backtest/
│   ├── api.py
│   ├── cli.py
│   ├── models.py
│   ├── data/
│   ├── engine/
│   ├── strategies/
│   ├── plugins/
│   ├── signals/
│   ├── validation/
│   └── reporting/
└── tests/
```

`btc_backtest` must never import `qt`. QT may import `btc_backtest` through
`qt.backtest` compatibility wrappers. This dependency direction makes the
backtester installable and testable outside QT while preserving the current
application.

Alternatives rejected:

1. Refactoring only inside `src/qt/backtest` would reduce migration work but
   would not create a genuinely independent module.
2. A standalone HTTP backtest service would add deployment, persistence, and
   distributed-job complexity to the calculation core. The independent
   package can later run inside QT's planned job worker without changing its
   public API.

## 3. Public API

The primary programmatic interface is immutable and typed:

```python
from datetime import datetime, timezone

from btc_backtest import BacktestRunner, BacktestSpec, DataRequest

spec = BacktestSpec(
    strategy="sma_crossover",
    strategy_params={"fast_window": 50, "slow_window": 200},
    data=DataRequest(
        provider="bitstamp",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2016, 7, 25, tzinfo=timezone.utc),
        end=datetime(2026, 7, 25, tzinfo=timezone.utc),
    ),
    initial_cash=10_000,
    fee_bps=10,
    slippage_bps=5,
)

result = BacktestRunner().run(spec)
```

Required public models:

- `DataRequest`: provider, market, symbol, timeframe, UTC interval, cache
  policy, and real-data requirement;
- `BacktestSpec`: data request, strategy identifier or custom strategy,
  parameters, portfolio constraints, fill settings, and random seed;
- `BacktestResult`: equity, positions, orders, fills, trades, signals,
  diagnostics, metrics, and reproducibility manifest;
- `ValidationSpec` and `ValidationResult`: walk-forward and stress-test
  configuration and outcomes;
- `SignalObservation` and `RankedSignal`: normalized signal input and
  aggregate output.

The standalone CLI mirrors the public API:

```text
btc-backtest data sync --provider bitstamp --symbol BTC/USD --timeframe 1d --years 10
btc-backtest data inspect --dataset <dataset-id>
btc-backtest strategies list
btc-backtest strategies describe sma_crossover
btc-backtest run sma_crossover --provider bitstamp --symbol BTC/USD --timeframe 1d --years 10
btc-backtest run-custom ./examples/custom_strategy.py:CustomStrategy ...
btc-backtest validate sma_crossover --walk-forward ...
btc-backtest signals collect
btc-backtest signals top --limit 20
```

Real data is the default. Synthetic data is available only through an explicit
test/demo flag and is always labeled in the result and artifact manifest.

## 4. Real Data And Ten-Year Guarantee

### 4.1 Provider Contract

Every `MarketDataProvider` implements:

```python
class MarketDataProvider(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def fetch(self, request: DataRequest) -> MarketDataset: ...
```

`MarketDataset` contains normalized UTC OHLCV data plus a manifest describing
the provider, endpoint or archive path, requested and delivered intervals,
retrieval time, license note, raw hashes, normalized fingerprint, gaps,
timeframe, market, and symbol.

Providers paginate internally. Downloads are written to temporary files,
validated, and atomically published to the Parquet cache only after all pages
pass validation. Cache identity includes provider, market, symbol, timeframe,
start, end, and schema version.

### 4.2 Built-In Providers

1. **Bitstamp OHLC** is the authoritative full-history provider for
   `BTC/USD`. Its public endpoint accepts `start`, `end`, `step`, and up to
   1,000 candles per request. Pagination supports genuine ten-year histories
   at daily and hourly resolution. The initial acceptance window is
   `2016-07-25T00:00:00Z` through `2026-07-25T00:00:00Z`.
2. **Binance Public Data** supplies checksummed spot and futures archives for
   `BTC/USDT` over Binance's available history. It is preferred for
   exchange-specific, higher-frequency, funding, and futures research.
3. **CCXT** remains an optional recent-data adapter for supported exchanges.
   It is not the ten-year guarantee because exchange pagination and retained
   history vary.
4. **Local Parquet** loads immutable user-provided datasets through the same
   validation and manifest pipeline.

A `CompositeProvider` may stitch explicitly configured sources. It must retain
per-row source identity, reject overlapping values outside a configured
tolerance, and record quote-currency transitions. It never silently combines
`BTC/USD` and `BTC/USDT`.

### 4.3 Validation

All real datasets must satisfy:

- UTC, unique, strictly increasing timestamps;
- required finite `open`, `high`, `low`, `close`, and non-negative `volume`;
- valid OHLC bounds and positive prices;
- no bars outside the requested closed-open interval;
- timeframe-aligned timestamps;
- explicit gap report and configurable maximum missing-bar ratio;
- no duplicate pages or cursor stalls;
- exact delivered duration for `--years 10`;
- stable SHA-256 normalized fingerprint.

Partial data is an error for a real-only run. The engine must never replace
missing real data with synthetic bars.

## 5. Backtest Engine

The engine is event-driven so custom and stateful strategies can share one
contract. A vectorized-strategy adapter is provided for indicator strategies
that naturally calculate target weights over an entire series.

Per bar, the engine:

1. exposes only data whose availability timestamp is at or before the current
   simulation time;
2. updates marks, funding, stops, and existing orders;
3. passes a read-only `StrategyContext` to the strategy;
4. validates returned order intents against portfolio constraints;
5. simulates eligible market, limit, stop, and stop-limit fills;
6. applies fees, spread/slippage, funding, and optional borrow costs;
7. updates cash, positions, realized and unrealized P&L;
8. records an immutable event, order, fill, and equity stream.

Initial execution scope:

- one BTC market per run;
- spot long/flat for every strategy;
- short perpetual leg only when a strategy declares derivative data and
  capability, initially funding-basis carry;
- deterministic bar-based fill rules;
- no liquidation model for unsupported leveraged directional strategies.

Order precedence and intrabar ambiguity are explicit. If both stop and target
could fill inside the same OHLC bar, the default conservative policy assumes
the adverse event occurred first. Alternative policies are permitted only when
declared in `BacktestSpec` and recorded in the manifest.

Core accounting invariants:

- cash plus marked positions equals equity at every bar;
- fills cannot precede order creation;
- fees and funding are applied exactly once;
- spot holdings cannot become negative;
- orders cannot spend unavailable cash;
- every realized P&L value reconciles to its fills;
- identical input, configuration, package version, and seed produce identical
  artifacts.

## 6. Strategy SDK

### 6.1 Contract

Custom strategies implement:

```python
class Strategy(Protocol):
    metadata: StrategyMetadata

    def initialize(self, context: InitializationContext) -> None: ...

    def on_bar(self, context: StrategyContext) -> Sequence[OrderIntent]: ...

    def finalize(self, context: FinalizationContext) -> None: ...
```

`StrategyMetadata` declares a stable identifier, version, description,
parameters with types and bounds, required fields, warm-up bars, supported
timeframes, market capabilities, and signal dependencies.

The context is read-only and point-in-time. Strategies receive bars, permitted
features, current positions, cash, open orders, and signal observations
available at that instant. They cannot access the runner's future dataset.

Custom strategies load through:

1. an explicit `module.py:ClassName` path;
2. Python package entry points under `btc_backtest.strategies`;
3. direct construction through the Python API.

Entry-point discovery is opt-in. Duplicate identifiers, incompatible API
versions, invalid parameter schemas, missing data requirements, and unsupported
capabilities fail before the run starts.

An `examples/custom_strategy.py` implementation and contract tests form the
reference for external users.

### 6.2 Twenty Built-In Algorithms

The required common catalog is:

| # | Identifier | Family | Default behavior |
| --- | --- | --- | --- |
| 1 | `fixed_dca` | Accumulation | Buy a fixed quote amount on a calendar schedule |
| 2 | `smart_dca` | Accumulation | Scale scheduled buys with price/sentiment valuation inputs |
| 3 | `sma_crossover` | Trend | Fast/slow simple moving-average crossover |
| 4 | `ema_crossover` | Trend | Fast/slow exponential moving-average crossover |
| 5 | `macd_trend` | Trend | MACD/signal-line regime allocation |
| 6 | `rsi_mean_reversion` | Mean reversion | Enter oversold and exit on normalization |
| 7 | `stochastic_reversal` | Mean reversion | Stochastic oscillator reversal |
| 8 | `bollinger_mean_reversion` | Mean reversion | Lower-band entry and center/upper-band exit |
| 9 | `bollinger_breakout` | Breakout | Volatility-band breakout with trailing exit |
| 10 | `donchian_breakout` | Breakout | Channel-high entry and channel-low exit |
| 11 | `turtle_trend` | Breakout | Donchian entry, ATR sizing, and shorter-channel exit |
| 12 | `time_series_momentum` | Momentum | Position from trailing absolute return |
| 13 | `dual_momentum` | Momentum | Absolute momentum plus cash-relative filter |
| 14 | `rate_of_change` | Momentum | Thresholded multi-period rate of change |
| 15 | `adx_trend` | Trend | Directional movement gated by trend strength |
| 16 | `atr_volatility_breakout` | Volatility | Prior range plus ATR breakout and ATR stop |
| 17 | `keltner_channel` | Volatility | EMA/ATR channel breakout |
| 18 | `vwap_mean_reversion` | Mean reversion | Deviation from rolling volume-weighted price |
| 19 | `grid_rebalance` | Market making | Bounded, non-leveraged inventory grid |
| 20 | `funding_basis_carry` | Relative value | Spot-long/perpetual-short funding carry |

QT's `capitulation` and `wick_catcher` strategies are migrated as additional
domain strategies and are not used to reduce the twenty-strategy requirement.
A `buy_and_hold` benchmark is also included but is not counted as a trading
algorithm.

Every built-in strategy has:

- a typed parameter model with conservative defaults and bounds;
- declared warm-up and data requirements;
- deterministic unit fixtures with hand-checkable expected signals;
- at least one no-trade, entry, exit, and insufficient-data test;
- a golden-result regression test;
- walk-forward and sensitivity coverage;
- documentation that explains mechanics, risks, and suitable regimes without
  profitability claims.

## 7. Hybrid Network Signal Layer

### 7.1 Normalized Observation

Every source maps into:

```text
SignalObservation
├── id and source event id
├── provider and source type
├── symbol and market
├── observed_at, effective_at, and expires_at
├── horizon
├── direction in [-1, 1]
├── confidence in [0, 1]
├── raw value and normalized value
├── provenance URL or dataset reference
├── payload hash
└── tags and data-quality flags
```

Backtests gate signals by `observed_at`, not the timestamp of the event being
described. This prevents revised on-chain metrics, delayed publications, or
late web signals from leaking into earlier bars.

### 7.2 Providers

Built-in adapters cover:

- Binance spot/futures market and derivative observations;
- Alternative.me Fear & Greed history;
- Coin Metrics community on-chain metrics available for BTC;
- existing QT intelligence findings through a local adapter;
- authenticated generic JSON HTTP feeds;
- signed inbound webhooks for user-configured providers such as alerting
  platforms;
- immutable CSV/JSON/Parquet signal archives for historical research.

Optional providers require their own credentials and are disabled when
unconfigured. Secrets remain in environment variables or the deployment secret
store and never appear in manifests, logs, or artifacts.

No adapter scrapes protected pages or labels a live-only provider as
historically backtestable. A provider must supply point-in-time history before
its observations can participate in a historical run.

### 7.3 Ranking

`SignalAggregator`:

1. validates, deduplicates, and normalizes observations;
2. rejects expired or future-known observations;
3. groups compatible horizon and symbol values;
4. applies source reliability, confidence, recency decay, and disagreement
   penalties;
5. emits ranked bullish, bearish, and neutral consensus signals with complete
   contributing-source attribution.

Source reliability begins at a conservative configured prior and may be
calibrated only on completed out-of-sample windows. In-sample performance
cannot increase a source's weight in that same window.

Strategies may consume ranked signals only when their metadata declares the
dependency. The final artifact lists every observation that affected an order.

## 8. Metrics, Validation, And Reports

### 8.1 Metrics

Results include:

- total return, CAGR, annualized volatility;
- Sharpe, Sortino, Calmar, and Omega ratios;
- maximum and average drawdown with duration;
- exposure, turnover, fees, slippage, and funding;
- trade count, win rate, profit factor, expectancy, average holding period;
- Value at Risk and Conditional Value at Risk;
- monthly and yearly return tables;
- performance against buy-and-hold and fixed-DCA benchmarks;
- data coverage and signal-source attribution.

Annualization is derived from the requested timeframe rather than hard-coded
to hourly bars.

### 8.2 Anti-Overfitting Validation

The validation suite provides:

- expanding and rolling walk-forward evaluation;
- explicit train, validation, and untouched test windows;
- parameter-grid sensitivity surfaces;
- purged time-series splits with an embargo interval;
- stationary/block bootstrap Monte Carlo simulations;
- randomized fees, slippage, and execution delay stress;
- missing-bar and provider-outage stress;
- regime-segment reporting;
- multiple-testing-aware strategy comparison;
- minimum-trade and minimum-exposure warnings.

Validation never chooses parameters on the final test window. Reports clearly
separate in-sample, validation, and out-of-sample results.

### 8.3 Reproducibility Manifest

Each run writes:

```text
run.json
data_manifest.json
equity.parquet
positions.parquet
orders.parquet
fills.parquet
trades.parquet
signals.parquet
metrics.json
validation.json
report.html
```

`run.json` records strategy identifier/version, validated parameters, data and
signal fingerprints, package and Python versions, fill policy, costs, seed,
UTC run time, and Git revision when available. Artifacts are written to a new
run directory and never overwrite a previous run.

## 9. Error Handling

- HTTP providers use bounded retries with exponential backoff and honor rate
  limits.
- Cursor stalls, duplicate pages, malformed payloads, incomplete coverage, and
  checksum failures are terminal for a real-only run.
- Interrupted downloads leave no published dataset.
- Corrupt cache entries are quarantined and refetched when network access is
  allowed.
- Provider errors retain provider, request interval, cursor, and retry context
  without credentials.
- Strategy exceptions include strategy and bar context, terminate the run, and
  preserve a failure manifest; the engine never continues with partial state.
- Non-finite orders, invalid quantities, unsupported shorts, overspending, and
  capability violations are rejected before fill simulation.
- Signal-provider failure is visible. Aggregation may continue only when the
  request's configured minimum source count and required-source rules remain
  satisfied.

## 10. Test Architecture

The package test suite has six layers:

1. **Unit tests:** indicators, fill rules, accounting, metrics, provider
   parsers, ranking, and every strategy branch.
2. **Property tests:** Hypothesis-generated bars and orders enforce accounting,
   monotonic event time, finite results, deterministic replay, and no negative
   spot inventory.
3. **Look-ahead tests:** mutating future bars or future-observed signals cannot
   change earlier orders, fills, or equity.
4. **Contract tests:** every built-in and custom plugin passes one shared
   strategy contract suite; every provider passes one data-provider suite.
5. **Golden tests:** versioned deterministic fixtures verify complete artifacts
   for all twenty algorithms and QT-specific strategies.
6. **Live integration tests:** marked tests fetch bounded official data,
   validate pagination, and prove a real ten-year Bitstamp dataset. Binance
   archive tests verify published checksums. They are separated from the
   offline default suite but are required by the release gate.

Performance acceptance:

- a ten-year daily run for one simple strategy completes within 5 seconds on
  the reference development machine after data is cached;
- a ten-year hourly run completes within 60 seconds for a simple strategy;
- memory remains bounded by the normalized dataset and artifact streams;
- the twenty-strategy daily matrix completes without network access after one
  data sync.

Repository-wide release verification includes package tests, QT compatibility
tests, Ruff, strict Mypy, build/install of both distributions in a clean
virtual environment, artifact-schema validation, the ten-year live-data test,
and `git diff --check`.

## 11. QT Integration And Migration

Migration is incremental:

1. Add the independent package and make it installable without QT.
2. Implement data contracts, Bitstamp/Binance providers, cache, and ten-year
   acceptance.
3. Implement engine, accounting, fills, public API, and custom strategy SDK.
4. Deliver the complete twenty-strategy catalog and shared contract tests.
5. Deliver hybrid signal providers, ranking, historical gating, and
   attribution.
6. Deliver validation, reports, artifacts, and standalone CLI.
7. Replace `qt.backtest.strategy_backtest` internals with compatibility calls
   while preserving existing imports and CLI aliases.
8. Connect finite backtests to QT's platform job worker when that worker phase
   is implemented; the package remains usable synchronously without it.

Compatibility requirements:

- existing `qt strategy run dca|trend|carry|wick` commands remain valid;
- existing dashboard and benchmark readers continue receiving compatible
  summaries during a documented transition window;
- legacy artifact readers either receive the old fields or a precise schema
  migration error;
- no live broker or strategy-runner behavior changes as part of extraction;
- current synthetic demos remain available but cannot be mistaken for real
  validation.

## 12. Acceptance Criteria

The objective is complete only when all of the following are proven:

1. `pip install packages/btc-backtest` works in a clean environment without
   installing QT.
2. The documented public API and standalone CLI run successfully.
3. A live integration test retrieves and validates real BTC/USD bars from
   2016-07-25 through 2026-07-25 and executes a ten-year backtest.
4. Real-only mode never silently uses synthetic or incomplete data.
5. Exactly twenty documented common algorithms are discoverable and pass the
   shared contract and golden suites.
6. `capitulation`, `wick_catcher`, and `buy_and_hold` remain available in
   addition to the twenty.
7. An external custom strategy can be installed through an entry point and run
   through API and CLI without changing package source.
8. At least five distinct signal-source types can be normalized, historically
   gated, ranked, and attributed in a result.
9. Future-bar and future-signal mutation tests prove point-in-time isolation.
10. Walk-forward, purged split, Monte Carlo, cost stress, and parameter
    sensitivity validations produce reproducible artifacts.
11. Accounting property tests and complete metrics pass at daily and hourly
    frequencies.
12. Existing QT backtest, benchmark, dashboard, and CLI regression suites pass
    through the compatibility layer.
13. Ruff, strict Mypy, package builds, clean-environment installs, offline
    tests, live integration tests, and artifact-schema validation all pass.
14. The implementation, validation evidence, and current branch are committed
    and pushed without secrets or generated market data.
