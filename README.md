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
  intel/         research scanners for funding, spread, basis, depeg, wick edges
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

## Easiest way to run (one command)

```bash
./start.sh init     # optional: 5-question wizard (venue, budget, risk, alerts)
./start.sh          # start all five strategies + intel scanner + dashboard
```

On first run it creates the virtualenv, installs everything, seeds `.env`,
then starts **all five strategies + the intel scanner + dashboard** at
`http://127.0.0.1:8765`. Re-running is always safe (idempotent). The
dashboard home opens with a plain-language, bilingual (EN/中文) "This week"
summary and a traffic-light health dot — you don't need to read tables to
know if it's working.

| Command | What it does |
| --- | --- |
| `./start.sh init` | interactive setup wizard → writes `.env` + tunes strategy presets |
| `./start.sh` | setup (if needed) + run everything in the foreground |
| `./start.sh daemon` | same, in the background (log: `data/runtime/qt.log`) |
| `./start.sh status` | is it running + live per-strategy health table |
| `./start.sh stop` | stop the background instance |
| `./start.sh fetch` | backfill 3 years of history into `data/parquet/` |
| `./start.sh backtest` | run the composite-score backtest on local data |
| `./start.sh bt <name>` | backtest a gallery strategy (`dca`/`trend`/`carry`/`wick`) |
| `./start.sh test` | run the test suite |

The wizard's **risk level** (conservative / balanced / aggressive) maps to
pre-tested strategy parameters and live-trading caps, so a beginner never
has to hand-edit YAML. Live trading stays **off** regardless — see
[`docs/live-checklist.md`](docs/live-checklist.md).

To get email/Telegram alerts when a strategy spots an opportunity, edit
`.env` (auto-created from `.env.example`) and fill in the `QT_SMTP_*` /
`QT_TELEGRAM_*` values, then restart.

## Manual Quick Start

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
- every running gallery strategy (see below) with its own status pill,
  last opportunity, and a sub-route `/strategy/<name>` for the full
  per-cycle metrics and configured params
- `/portfolio` — per-strategy paper P&L (equity, realized/unrealized,
  drawdown, positions, recent trades)
- `/intel` — Intelligence Discovery: detected opportunities ranked by net
  edge after fees (funding carry, cross-venue spread, basis, depeg, wick
  regime), each with a plain-language "why" and "what to do"
- ranked intelligence discoveries at `/api/intel`

## Multi-strategy solution gallery

QT ships five independently-configured executable strategies under
`src/qt/strategies/`:

| Name | Class | What it does | Default cadence |
| --- | --- | --- | --- |
| `dca` | `SmartDCA` | Volatility-aware weekly DCA (stress-scaled buy size) | 1 h (alerts on Mon 14:00 UTC) |
| `capitulation` | `Capitulation` | 5-factor composite extreme-event buyer + macro veto | 30 min |
| `trend` | `WeeklyTrend` | Faber/Clenow weekly SMA(20w) crossover | 6 h |
| `carry` | `BasisCarry` | Market-neutral spot+perp funding-rate carry | 1 h |
| `wick` | `WickCatcher` | Deep-limit ladder for liquidation-wick mean reversion | 1 min |

The evidence behind `carry`, `capitulation`, `wick`, and `intel` — what
actually earns at personal scale and what doesn't — is documented with
citations in [`docs/RESEARCH-EARNING.md`](docs/RESEARCH-EARNING.md).

The separate `qt.intel` discovery scanner uses
`config/strategies/intel.yaml`, runs from `scripts/run_all.py`, and writes
ranked net-of-fees opportunities for `/intel`.

Each one has its own YAML at `config/strategies/<name>.yaml`. Signal
params, cadence, on/off flag, and minimum alert severity all live there
— change them without touching code:

```yaml
# config/strategies/dca.yaml
enabled: true
interval_seconds: 3600
min_alert_severity: critical
params:
  base_buy_quote: 100.0
  buy_dow: 0
  buy_hour_utc: 14
  multiplier_k: 2.0
```

### One-line start (all strategies + dashboard)

```bash
python scripts/run_all.py
```

This single command:

1. Loads every strategy `*.yaml` under `config/strategies/` plus the
   `intel.yaml` discovery scanner config.
2. Spawns one daemon thread per **enabled** strategy; each writes its
   heartbeat to `data/runtime/strategies/<name>.json`.
3. Runs the intel scanner, which writes ranked opportunities under
   `data/runtime/intel/opportunities.json`.
4. Serves the dashboard on `127.0.0.1:8765` so each strategy gets its
   own sub-route at `http://127.0.0.1:8765/strategy/<name>` with the
   latest metrics, last opportunity, and the YAML params actually in
   effect.
5. When any strategy or intel scanner emits an opportunity, the existing
   `qt.monitoring.alerts.alert(...)` plumbing sends it to stderr +
   email (`QT_SMTP_*`) + Telegram (`QT_TELEGRAM_*`).

Useful overrides:

```bash
python scripts/run_all.py \
  --strategies-dir config/strategies \
  --runtime-dir data/runtime \
  --dashboard-host 0.0.0.0 \
  --dashboard-port 8765
```

`--no-dashboard` runs only the strategy threads.

## Intelligence Discovery (`qt.intel`)

The `qt.intel` discovery scanner continuously scans for opportunities and ranks them by
**net edge after fees** — it never trades, it only surfaces and alerts:

| Scanner | What it detects | Who acts on it |
| --- | --- | --- |
| `FundingScanner` | per-venue carry APR + cross-venue funding differential | `carry` strategy / you |
| `SpreadScanner` | cross-venue spot spread net of 2× taker + slippage | you (needs inventory on both venues) |
| `BasisScanner` | dated-futures cash-and-carry APR | you |
| `DepegScanner` | stablecoin deviation from \$1 | **you** — verify issuer solvency first (USDC-2023 recovered; UST didn't) |
| `WickScanner` | live liquidation-cascade / wick regime | `wick` + `capitulation` strategies |

Findings land at `http://127.0.0.1:8765/intel` and in
`data/runtime/intel/opportunities.json`, ranked by `net_edge_bps ×
confidence`. Anything whose edge doesn't clear the fee line is never shown.
Thresholds live in `config/strategies/intel.yaml`. See
[`docs/RESEARCH-EARNING.md`](docs/RESEARCH-EARNING.md) for why each scanner
exists and what its real risks are.

> In network-restricted environments (no exchange API access) the scanners
> degrade gracefully and the list stays empty — real scanning needs a host
> that can reach the exchanges.

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

## Deploy to Aliyun Lighthouse (one line)

On a fresh Ubuntu 22.04 Lighthouse / ECS instance:

```bash
curl -fsSL https://raw.githubusercontent.com/bridge-win/qt/main/deploy/aliyun_bootstrap.sh | sudo bash
```

This installs Python, clones the repo to `/opt/qt`, builds the venv,
seeds `.env`, and starts the watchdog as a `systemd` service. Then edit
`/opt/qt/.env` to add SMTP and Telegram credentials and run
`sudo systemctl restart qt`. Full setup notes — including how to obtain
a Telegram bot token / chat id and how to configure Aliyun DirectMail
SMTP — are in [`deploy/README.md`](deploy/README.md).

## Buy-opportunity alerts

Every paper-loop cycle that fires an entry signal sends a `critical`
alert through every configured channel: structured log, email (SMTP),
and Telegram. Failures in any one channel are logged but never crash the
loop. Configure via the `QT_SMTP_*` and `QT_TELEGRAM_*` variables in
`.env` (see [`.env.example`](.env.example)).

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
See [`docs/operations.md`](docs/operations.md) for the full runbook, and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for the evidence-based plan from
signal generation to safe live trading.
See [`docs/RESEARCH-EARNING.md`](docs/RESEARCH-EARNING.md) for the
research-backed opportunity map behind the intel scanners and wick catcher.

## Batch backtests (`qt.strategies.sim`)

`solution2.md` collects the original research; `src/qt/strategies/sim/` ships
the **batch backtest** counterparts of four live strategies (DCA,
weekly trend, basis carry, wick catcher). Unlike the live signal-emitters under
`qt.strategies.*`, these classes consume a full OHLCV history and
return an equity curve + trade list — useful for parameter tuning and
walk-forward analysis.

| ID | Class (in `qt.strategies.sim`) | What it does |
| --- | --- | --- |
| **A** | `SmartDCABacktest` | Vol-aware weekly DCA replayed across history |
| **C** | `WeeklyTrendBacktest` | Faber/Clenow weekly SMA(20w) trend replay |
| **D** | `BasisCarryBacktest` | Spot+perp carry replay on funding history |
| **E** | `WickCatcherBacktest` | Deep wick limit-ladder replay with take-profit/time exits |

### Run a batch backtest

```bash
# Uses local Parquet data if present; otherwise falls back to deterministic
# SYNTHETIC data so it always runs (even with no network / no API keys).
qt strategy run dca              # SmartDCABacktest
qt strategy run trend            # WeeklyTrendBacktest
qt strategy run carry            # BasisCarryBacktest (synthesizes funding if none)
qt strategy run wick             # WickCatcherBacktest

qt strategy run dca --synthetic  # force synthetic data even if local data exists
qt strategy run wick --synthetic # smoke-test wick catcher offline
```

Each prints final equity, x-multiple, CAGR, Sharpe, max drawdown, and trade
count, and exports `equity.csv` / `trades.csv` / `summary.json` under
`data/backtests/strategy_<name>_<ts>/` (plus a `strategy_latest.json`).
Synthetic runs are clearly labeled `(SYNTHETIC data)` so results are never
mistaken for real performance.

The unified entry point is `qt.backtest.strategy_backtest.run_strategy_backtest`,
which any script or notebook can call directly:

```python
from qt.backtest.strategy_backtest import run_strategy_backtest
out = run_strategy_backtest("dca", ohlcv=None)   # None -> synthetic fallback
print(out.summary())                             # metrics dict
```

For the live signal-emitting versions (`qt.strategies.SmartDCA`,
`Capitulation`, `WeeklyTrend`, `BasisCarry`, `WickCatcher`) see the "Multi-strategy
solution gallery" section above and `python scripts/run_all.py`.

> ⚠️ Backtests use *synthetic or local* data. Before deploying capital, run
> them through `qt.backtest.walkforward` and `qt.backtest.montecarlo` (see
> `scripts/walk_forward.py` and `scripts/stress_test.py`).

## Tests

```bash
pytest -q
ruff check .
mypy src tests scripts
```
