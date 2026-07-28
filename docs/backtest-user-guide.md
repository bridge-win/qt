# QT backtest user guide

Use the backtest pages to answer one question: “If this BTC rule had run on this historical data window, what would it have done, and how much risk did it require?”

## Safe operating rule

Backtesting does not use exchange keys and does not send real orders. It replays local OHLCV parquet data, writes artifacts under `data/backtests/`, and publishes the latest result to the dashboard.

Phase 1 is research only: spot long/cash, no leverage, no shorting, no live exchange API keys.

## Which page to use

- `/backtest/build`: beginner-friendly research builder. Use this first. It exposes the full algorithm catalog, validates a small readable rule recipe, submits a job, shows progress, and opens a dedicated result page.
- `/backtest`: classic quick-run page. Use this for the existing composite, DCA, trend, carry, and wick workflows.
- `/api/v1/backtest/catalog`: machine-readable catalog for the UI, including algorithm explanations, parameter impact notes, groups, data defaults, and safe defaults.
- `/api/v1/backtest/jobs/{job_id}`: job status and progress.
- `/backtest/runs/{run_id}`: result page with research verdict, chart, metrics, and robustness checklist.

## How to use `/backtest/build`

1. Open `https://qt.followkol.live/backtest/build`.
2. Choose an algorithm. Start with `sma_crossover`, `fixed_dca`, `smart_dca`, `rsi_mean_reversion`, `bollinger_mean_reversion`, `donchian_breakout`, or `buy_and_hold`.
3. Choose a data source. Serious BTC spot research should use the 10-year Bitstamp BTC/USD daily standard after that local dataset is synced. OKX BTC/USDT is exchange-specific and should be read separately.
4. Set assumptions:
   - `initial_cash`: simulated account size only.
   - `fee_bps`: trading fee in basis points. `10` means 0.10%.
   - `slippage_bps`: assumed execution slippage in basis points. `5` means 0.05%.
5. Add at most three entry conditions. `ALL entry conditions` means every selected condition must be true before entry.
6. Add one or more exit conditions. `ANY exit condition` means one selected exit can close the position.
7. Press `Run research job`.
8. Read progress through queued, validation, indicators, simulation, metrics, robustness, visualization, and complete.
9. Read the result page in this order: data source, trade count, max drawdown, total return, Sharpe, scorecard, chart markers, robustness notes.

## How to use `/backtest`

1. Open `https://qt.followkol.live/backtest`.
2. Choose a strategy.
   - `composite`: QT's multi-factor timing model.
   - `dca`: staged accumulation.
   - `trend`: moving-average trend following.
   - `carry`: funding/basis carry.
   - `wick`: crash-wick rebound entries.
3. Choose a data source. The page lists only non-empty local OHLCV files.
4. Set initial cash. This changes simulated account size only.
5. Press `Run backtest` for one selected strategy, or `Auto compare strategies`
   to run DCA, trend, carry, and wick on the same data and initial cash.
6. Read the result in this order:
   - Data window and row count.
   - Trade count.
   - Max drawdown.
   - Total return.
   - Sharpe or risk-adjusted quality.
   - Buy/sell markers on the chart.
   - If auto compare was used, start with the ranked comparison table before
     inspecting the chart for the best-ranked run.

## When to use auto compare

Use `Auto compare strategies` when you do not yet know which algorithm to test.
It keeps the data source and initial cash fixed, then ranks the safe strategy
set by return, drawdown, Sharpe, and trade sample. The top row is a paper-trading
candidate, not a live-trading approval.

Use a single-strategy run after auto compare when you want to inspect the chart,
trade markers, and artifacts for one specific rule.

## How to interpret the result

- Positive return means the rule made money in the tested window, not that it will make money tomorrow.
- Max drawdown is the pain required to get that result.
- Few trades means the sample is thin.
- A high return with high drawdown is not production-ready.
- A chart marker shows where the replay engine emitted a trade; it is not a live fill.
- The research scorecard is a filter, not an approval. It checks data quality, drawdown, trade sample, and risk-adjusted quality.
- The robustness checklist shows what still needs to be validated before paper trading: walk-forward, Monte Carlo, cost stress, and out-of-sample behavior.

## Algorithm groups

- Accumulation: `fixed_dca`, `smart_dca`.
- Trend and breakout: `sma_crossover`, `ema_crossover`, `macd_trend`, `donchian_breakout`, `turtle_trend`, `time_series_momentum`, `dual_momentum`, `rate_of_change`, `adx_trend`, `atr_volatility_breakout`, `keltner_channel`.
- Mean reversion: `rsi_mean_reversion`, `stochastic_reversal`, `bollinger_mean_reversion`, `vwap_mean_reversion`, `grid_rebalance`.
- Benchmark: `buy_and_hold`.
- Advanced derivative research: `funding_basis_carry`. This needs funding/basis data and is not a beginner default.
- QT special: `capitulation`, `wick_catcher`.

Always compare an active strategy with `buy_and_hold`. If it cannot improve drawdown or behavior versus holding BTC, it is not useful enough for automation.

## Before automatic execution

Follow this progression:

1. Backtest until you understand data, fees, slippage, drawdown, and overfitting.
2. Learn basic algorithm families: DCA, trend following, breakout, mean reversion, volatility filters, position sizing, stop logic, and benchmarks.
3. Paper trade the same rules and compare paper fills with backtest assumptions.
4. Add hard risk controls: max position, max daily loss, stale-data halt, exchange-error halt, and kill switch.
5. Lock down live keys: trade-only, no withdrawal, IP allowlist.
6. Monitor heartbeat, logs, data freshness, P&L reconciliation, and alert delivery.

Do not move to live trading because one backtest looks good. Move only after backtest, paper trading, operational monitoring, and risk controls agree.
