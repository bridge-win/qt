# QT backtest user guide

Use the `/backtest` page to answer one question: “If this BTC rule had run on this historical data window, what would it have done, and how much risk did it require?”

## Safe operating rule

Backtesting does not use exchange keys and does not send real orders. It replays local OHLCV parquet data, writes artifacts under `data/backtests/`, and publishes the latest result to the dashboard.

## How to use the page

1. Open `https://qt.followkol.live/backtest`.
2. Choose a strategy.
   - `composite`: QT's multi-factor timing model.
   - `dca`: staged accumulation.
   - `trend`: moving-average trend following.
   - `carry`: funding/basis carry.
   - `wick`: crash-wick rebound entries.
3. Choose a data source. Production currently exposes non-empty OKX BTC/USDT 1-hour candles.
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

## Before automatic execution

Follow this progression:

1. Backtest until you understand data, fees, slippage, drawdown, and overfitting.
2. Paper trade the same rules and compare paper fills with backtest assumptions.
3. Add hard risk controls: max position, max daily loss, stale-data halt, exchange-error halt, and kill switch.
4. Lock down live keys: trade-only, no withdrawal, IP allowlist.
5. Monitor heartbeat, logs, data freshness, P&L reconciliation, and alert delivery.

Do not move to live trading because one backtest looks good. Move only after backtest, paper trading, operational monitoring, and risk controls agree.
