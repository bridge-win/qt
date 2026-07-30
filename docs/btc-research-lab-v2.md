# BTC Research Lab V2

BTC Research Lab is a historical research tool for a single private user. It
does not connect to an exchange, hold API keys, use leverage, short BTC, or
place real orders.

## First run

1. Open `https://qt.followkol.live/backtest/build` and enter the Basic Auth
   username and password stored on the server.
2. Read the research goal. A backtest describes historical behavior; it is not
   a forecast.
3. Select a dataset marked `ready`. BTC/USD and BTC/USDT remain separate.
4. Choose one creation mode:
   - **Template** runs the named built-in algorithm and its parameters.
   - **Custom rules** runs an explicit `custom_rule_recipe`; it never borrows a
     template's name.
   - **Ensemble** combines the desired BTC exposure of two or three compatible
     algorithms. Entered weights are normalized to 100%.
5. Review cash, fee, slippage, validation profile, and deterministic seed.
6. Launch the job. The page reports the persisted stage and percentage. It is
   safe to reload the browser. Cancellation takes effect between stages and
   validation batches.

Use **Quick** while learning or checking a configuration. It runs the selected
strategy plus buy-and-hold and fixed-DCA benchmarks. A quick run deliberately
cannot become a paper-research candidate.

Use **Standard** only with the verified Bitstamp ten-year daily dataset. It
runs purged walk-forward selection, an untouched final year, parameter
sensitivity, 500 seeded bootstrap paths, cost stress, future-data consistency,
warm-up stability, and Deflated Sharpe.

## Reading the result

Start with the verdict, not total return.

- **Rejected** means data integrity, future-data consistency, recursive
  stability, or execution failed.
- **Fragile** means at least one robustness threshold failed.
- **Paper-research candidate** means all gates passed and the final untouched
  test either beat buy-and-hold or cut its drawdown by at least 20% while
  remaining profitable.

No result is called live-ready. The result page includes:

- strategy, buy-and-hold, and fixed-DCA metrics;
- candles, volume, buy/sell markers, equity comparisons, and drawdown;
- walk-forward folds, parameter trials, Monte Carlo percentiles, and 1×/2×/3×
  cost evidence;
- data coverage, fingerprint, exact failed gates, downloads, and checksums.

## Parameter intuition

- A shorter indicator window reacts sooner, trades more often, and is more
  sensitive to noise.
- A longer window reacts later, needs more warm-up data, and may miss fast
  reversals.
- A looser entry threshold creates more trades and usually more costs.
- A stricter entry threshold creates fewer trades and increases the danger of a
  result based on too small a sample.
- Higher allocation or ensemble weight increases both potential return and
  drawdown.
- Higher fee and slippage assumptions make the test more conservative.

Change one concept at a time. Keep the seed and dataset fingerprint fixed when
comparing two parameter choices.

## Suggested learning path

1. Learn OHLCV candles, returns, compounding, volatility, maximum drawdown, and
   Sharpe ratio.
2. Compare buy-and-hold with fixed DCA on several complete cycles.
3. Study SMA trend, RSI mean reversion, Bollinger mean reversion, and Donchian
   breakout. Explain each trade before optimizing it.
4. Learn overfitting, lookahead bias, survivorship bias, walk-forward testing,
   multiple testing, and execution costs.
5. Maintain a written hypothesis and a fixed acceptance rule before every
   standard run.
6. Only after repeated research candidates, build an isolated paper-trading
   phase with exchange connectors, reconciliation, kill switches, secrets
   management, and operational monitoring.

Paper fills should run for months before considering live execution. Research
code and future exchange execution must remain separate services.

## Operations

```bash
# Public authenticated health (run on the server so the password is not shown)
sudo bash -lc 'curl -u "$(cat /etc/qt/research-auth)" \
  https://qt.followkol.live/api/v2/backtests/health'

# Worker and dashboard logs
sudo journalctl -u qt -u qt-research-worker -u caddy -n 100 --no-pager

# Refresh the managed dataset immediately
sudo systemctl start qt-research-data-refresh.service

# Show the generated browser credentials locally on the server
sudo cat /etc/qt/research-auth
```

Generated data, SQLite state, artifacts, and credentials are deployment state;
they are excluded from Git.
