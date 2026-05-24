# QT — BTC Quantitative Trading Platform

QT is a low-frequency, long-only BTC trading system that buys **only**
during extreme capitulation events (black-swan crashes, liquidation
cascades, long-wick insertions) where **multiple independent factor
groups** simultaneously confirm a high-probability short-horizon mean
reversion, and macro conditions do not contradict.

The design synthesizes practitioner research (Glassnode, CryptoQuant,
Capriole, LookIntoBitcoin) with peer-reviewed studies (Caporale et al.
2018, Ardia et al. 2019, Gkillas & Katsiampa 2018, Shu et al. 2021) and
the practitioner consensus on N-of-K factor voting. See
[`docs/strategy.md`](docs/strategy.md) for citations and full thresholds.

## Core idea: 5 factor groups + macro veto

Entry requires:

1. **Price action** — RSI<20 or BB-Z≤-2.5 or wick≥3× body or DD₃₀ᴅ≥15 %
   or high-volume capitulation down bar
2. **Volatility** — short/long realized-vol ratio ≥ 2
3. **Derivatives** — funding Z≤-2 (or sustained negative) or OI drop ≥10 %
   or crowded-short long/short ratio percentile
4. **On-chain** — any of: MVRV-Z<0.5, aSOPR<0.97, NUPL<0, Puell<0.5,
   Reserve Risk<0.002, exchange-netflow Z≤-2, Pi Cycle Bottom event
5. **Sentiment** — Fear & Greed ≤ 15 sustained 3 days, or social Z ≤ -2

**Macro veto** (must all hold): VIX < 35, DXY 20d-ROC Z < 2.

Score = (groups fired) / (groups with data). Trigger at score ≥ 0.60 and
≥ 4 groups firing. Sizing = min(score-alloc, ¼-Kelly, vol-target, cap
20 % per trade). Exits = ATR(2.5×) stop, ATR(4×) take-profit, 120-bar
time stop.

## Layout

```
src/qt/
  core/          types, config (pydantic), structured logging
  data/          ingestion adapters (ccxt, Binance fapi, Coin Metrics,
                 Glassnode, alternative.me, Santiment, FRED, GDELT)
                 + ParquetStore for replay-deterministic backtests
  indicators/    price / vol / derivatives / on-chain / sentiment / composite
  signal/        SignalEngine — turns composite score into sparse Signals
  risk/          fractional Kelly + vol-targeting, ATR stops, kill-switch
  backtest/      event-driven Backtester, FillModel, metrics
  execution/     Broker interface; PaperBroker (live disabled by default)
  monitoring/    reporting, alerts, durable heartbeat supervisor
  dashboard/     local web UI for data sources, heartbeat, backtests
  cli.py         `qt data fetch-ohlcv`, `qt backtest`, `qt dashboard`,
                 `qt monitor health`, `qt info`
config/          default.yaml, thresholds_research.yaml
docs/            strategy.md, indicators.md, architecture.md
scripts/         fetch_history.py, run_backtest.py, run_paper.py,
                 run_dashboard.py, run_service.py
tests/           ≥ 8 unit/integration tests w/ synthetic fixture crashes
```

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. backfill data into data/parquet/
python scripts/fetch_history.py --days 1095

# 2. run a backtest and export dashboard artifacts
qt --config config/default.yaml backtest

# 3. inspect data sources, freshness, and latest backtest
qt --config config/default.yaml dashboard --port 8765

# 4. paper-trade loop (1h cycles) with durable monitor state
python scripts/run_paper.py --interval 3600
```

## Daily Usage

```bash
# Check effective config with secrets redacted
qt --config config/default.yaml info

# See all configured data sources and local freshness
qt --config config/default.yaml data sources

# Run a reproducible backtest and export dashboard artifacts
qt --config config/default.yaml backtest --output-dir data/backtests

# Serve the local dashboard
qt --config config/default.yaml dashboard --port 8765
```

The dashboard is available at `http://127.0.0.1:8765` and shows:

- data sources, provider, usage, local row count, freshness, and missing keys
- latest backtest metrics plus paths to exported CSV/JSON artifacts
- current paper-loop heartbeat, latest score, equity, and last error

## Unattended Operation

Use the parent watchdog for continuous paper-mode operation:

```bash
python scripts/run_service.py \
  --config config/default.yaml \
  --interval 3600 \
  --state-path data/runtime/monitor_state.json \
  --stale-after-seconds 7200 \
  --startup-grace-seconds 300 \
  --dashboard-port 8765
```

`run_service.py` starts and monitors both the paper loop and dashboard. It
restarts the paper loop if the process exits, the heartbeat is stale, the
heartbeat is missing after startup grace, or the heartbeat reports a failed or
stopped state. It also restarts the dashboard if it exits.

Machine-readable health check:

```bash
qt monitor health \
  --state-path data/runtime/monitor_state.json \
  --stale-after-seconds 7200 \
  --json
```

Exit code is `0` only when the heartbeat is healthy.

Free tier covers OHLCV + derivatives + Coin Metrics on-chain + Fear &
Greed. Glassnode / Santiment / FRED keys unlock more factors; missing
factors silently drop out of the score denominator.

## Safety

- Live trading is **disabled** until `QT_LIVE_TRADING_ENABLED=true` and
  per-venue `LiveBroker.submit` is wired explicitly.
- Drawdown kill-switch at 20 % blocks new entries; manual reset only.
- All decisions are explainable: each `Signal` carries the firing factors
  for audit.
- Long-running paper mode writes `data/runtime/monitor_state.json`; the
  dashboard reads this heartbeat so deployment health is visible.
- Backtests export `summary.json`, `equity.csv`, `trades.csv`, and
  `signals.csv` under `data/backtests/` for reproducible review.

See [`docs/architecture.md`](docs/architecture.md) for the live-trading
enablement checklist and [`docs/strategy.md`](docs/strategy.md) for the
walk-forward validation plan that must be passed before deploying capital.
See [`docs/operations.md`](docs/operations.md) for the full runbook.

## Tests

```bash
pytest -q
ruff check .
mypy src tests scripts
```
