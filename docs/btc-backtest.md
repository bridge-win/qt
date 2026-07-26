# Independent BTC backtesting

`btc-backtest` is the independently installable backtesting package under
`packages/btc-backtest`. The package does not import `qt`; QT consumes it
through a one-way compatibility adapter so the package can be tested, built,
and installed on its own.

This guide covers the operator workflows, custom strategy path, signal input
path, ten-year acceptance window, and the built-in strategy catalog. Backtest
output is evidence for historical behavior under the selected data,
assumptions, costs, and code version. It is not a profitability guarantee.

## Install

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip build
pip install -e packages/btc-backtest
```

The root QT distribution also depends on `btc-backtest==0.1.0`, and the
platform install path builds both local wheels before installing QT.

## Real data defaults

Real data is the default. Synthetic data is only available when explicitly
requested with `--synthetic`; commands that see the synthetic provider without
that flag exit with an error instead of silently falling back.

The ten-year acceptance interval is closed-open UTC:

- start: `2016-07-25T00:00:00Z`
- end: `2026-07-25T00:00:00Z`
- daily expected slots: `3652`
- hourly expected slots: `87648`

Live acceptance uses Bitstamp `BTC/USD` spot OHLCV and checks that delivered
bars plus declared exchange gaps equal the full calendar, with a missing ratio
no higher than 0.1%.

## Data workflows

Fetch and cache real provider data:

```bash
btc-backtest data sync \
  --provider bitstamp \
  --symbol BTC/USD \
  --timeframe 1d \
  --start 2016-07-25T00:00:00Z \
  --end 2026-07-25T00:00:00Z \
  --cache-dir .btc-backtest-cache
```

Inspect a cached dataset:

```bash
btc-backtest data inspect \
  --provider bitstamp \
  --symbol BTC/USD \
  --timeframe 1d \
  --start 2016-07-25T00:00:00Z \
  --end 2026-07-25T00:00:00Z \
  --cache-dir .btc-backtest-cache
```

For local fixture or research data, pass `--provider local --path data.parquet`.
The Parquet file must contain validated OHLCV columns and a timezone-aware UTC
index. All requested intervals are closed-open.

## Built-in algorithms

The exact top-20 programmatic trading algorithms are:

- `fixed_dca`
- `smart_dca`
- `sma_crossover`
- `ema_crossover`
- `macd_trend`
- `rsi_mean_reversion`
- `stochastic_reversal`
- `bollinger_mean_reversion`
- `bollinger_breakout`
- `donchian_breakout`
- `turtle_trend`
- `time_series_momentum`
- `dual_momentum`
- `rate_of_change`
- `adx_trend`
- `atr_volatility_breakout`
- `keltner_channel`
- `vwap_mean_reversion`
- `grid_rebalance`
- `funding_basis_carry`

The package also ships three non-counted strategy IDs used for benchmarks and
QT compatibility:

- `buy_and_hold`
- `capitulation`
- `wick_catcher`

List or inspect the catalog:

```bash
btc-backtest strategies list
btc-backtest strategies describe sma_crossover
```

## Run a backtest

Run a built-in strategy and write a reproducible artifact bundle:

```bash
btc-backtest run sma_crossover \
  --provider bitstamp \
  --symbol BTC/USD \
  --timeframe 1d \
  --start 2016-07-25T00:00:00Z \
  --end 2026-07-25T00:00:00Z \
  --cache-dir .btc-backtest-cache \
  --output data/backtests
```

Useful parameters:

- `--params-json` supplies strategy parameters as JSON.
- `--fee-bps` and `--slippage-bps` set execution cost assumptions.
- `--initial-cash` sets starting capital.
- `--json/--no-json` controls stdout payload shape.

Artifacts include run metadata, data provenance, metrics, validation payloads,
orders, fills, equity, and a standalone HTML report.

## Custom strategies

Custom strategies implement the public SDK:

- `StrategyMetadata`
- `initialize(context: InitializationContext)`
- `on_bar(context: StrategyContext) -> Sequence[OrderIntent]`
- `finalize(context: FinalizationContext)`

Run a local `file.py:ClassName` strategy:

```bash
btc-backtest run-custom packages/btc-backtest/examples/custom_strategy.py:CustomStrategy \
  --provider local \
  --path data/btc.parquet \
  --cache-dir .btc-backtest-cache \
  --output data/backtests
```

Installed packages can also expose strategy entry points. The clean packaging
test installs a fixture plugin and discovers `external_fixture` through normal
Python entry-point discovery.

Custom strategy contexts are point-in-time: `context.bars` and every auxiliary
frame end at the active timestamp, and `context.signals` only contains
observations available by `observed_at`.

## Validation

Run walk-forward validation:

```bash
btc-backtest validate sma_crossover \
  --provider bitstamp \
  --symbol BTC/USD \
  --timeframe 1d \
  --start 2016-07-25T00:00:00Z \
  --end 2026-07-25T00:00:00Z \
  --train-bars 365 \
  --test-bars 90 \
  --purge-bars 5 \
  --embargo-bars 3 \
  --final-test-bars 365 \
  --candidate-json '{"fast_window": 20, "slow_window": 100}' \
  --candidate-json '{"fast_window": 50, "slow_window": 200}'
```

Validation uses immutable train/test windows, optional purge/embargo, stable
candidate ordering, an untouched final test window, seeded Monte Carlo, stress
scenarios, and multiple-testing diagnostics.

## Signals

Signals are normalized into `SignalObservation` records and ranked into
point-in-time `RankedSignal` values. A signal cannot affect a bar before its
`observed_at` timestamp.

Collect an archive into a store:

```bash
btc-backtest signals collect \
  --archive signals.json \
  --store .btc-backtest-cache/signals \
  --symbol BTC/USD \
  --horizon 1d \
  --start 2024-01-01T00:00:00Z \
  --end 2024-02-01T00:00:00Z
```

Rank available signals:

```bash
btc-backtest signals top \
  --archive signals.json \
  --symbol BTC/USD \
  --horizon 1d \
  --start 2024-01-01T00:00:00Z \
  --end 2024-02-01T00:00:00Z \
  --as-of 2024-01-15T00:00:00Z \
  --json
```

Implemented source coverage includes public sentiment, on-chain metrics,
derivatives funding, open interest, long/short ratio, taker flow, local
archives, allowlisted generic HTTP archives, and signed webhook ingestion.
Credentialed providers should load secrets from the environment or OS secret
store, never from committed files.

## QT compatibility

QT legacy aliases remain accepted and map to package strategies:

| QT alias | Package strategy |
| --- | --- |
| `dca` | `smart_dca` |
| `trend` | `sma_crossover` |
| `carry` | `funding_basis_carry` |
| `wick` | `wick_catcher` |

Legacy QT artifact readers retain fields such as `strategy`, `synthetic`,
`bars`, `metrics`, and `data_fingerprint`.

## Verification

The acceptance evidence document is
[`docs/verification/btc-backtest-acceptance.md`](verification/btc-backtest-acceptance.md).
It maps the fourteen design criteria to concrete files, commands, exit codes,
counts, and commit evidence. Treat it as an audit log; rerun the commands on
the current branch before making a release or live-capital decision.
