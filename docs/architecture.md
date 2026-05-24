# Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Data layer                                  │
│  qt.data.market   (ccxt: Binance/OKX/Bybit/Coinbase OHLCV)              │
│  qt.data.derivatives (Binance fapi: funding, OI, LSR)                   │
│  qt.data.onchain  (Coin Metrics free + Glassnode paid + mempool.space)  │
│  qt.data.sentiment (alternative.me F&G + Santiment social)              │
│  qt.data.macro    (FRED: VIX, DXY, 10Y, M2)                             │
│  qt.data.news     (CryptoPanic + GDELT TimelineTone)                    │
│                             ↓                                            │
│                qt.data.store.ParquetStore  (zstd, append-on-merge)      │
└─────────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                          Indicator layer                                 │
│  qt.indicators.price     (RSI, BB, ATR, drawdown, wick)                 │
│  qt.indicators.volatility (RV, RV-ratio, Parkinson)                     │
│  qt.indicators.derivatives (funding Z, OI drop, LSR percentile)         │
│  qt.indicators.onchain   (MVRV-Z, SOPR, NUPL, Puell, Pi Cycle, Ribbons) │
│  qt.indicators.sentiment (F&G sustained, social Z)                      │
│  qt.indicators.composite (5-group N-of-K voting + macro veto)           │
└─────────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────────┐
│   qt.signal.engine.SignalEngine  → list[Signal]  (sparse, explainable) │
└─────────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────────┐
│   qt.risk.engine.RiskEngine                                             │
│     - fractional Kelly                                                   │
│     - vol-target scaler                                                  │
│     - hard cap (max_position_pct)                                        │
│     - ATR stop / TP / time stop                                          │
│     - drawdown kill-switch + cooldown                                    │
└─────────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────┐    ┌────────────────────────────────────┐
│ qt.backtest.engine            │    │ qt.execution                       │
│  Backtester (event-driven)    │    │  base.Broker (interface)           │
│  fills.FillModel              │    │  paper.PaperBroker                 │
│  metrics.compute_metrics      │    │  live.LiveBroker  (disabled)       │
└──────────────────────────────┘    └────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  qt.monitoring (alerts + reporting + supervised heartbeat state)         │
│  qt.dashboard  (local web UI over data sources, heartbeat, backtests)    │
└─────────────────────────────────────────────────────────────────────────┘
```

## Process & data flow

- **Research mode** (`QT_ENV=research`): ingestion adapters write to
  `data/parquet/`. The backtester reads only from Parquet — every signal,
  every trade, every metric is reproducible from a frozen snapshot.

- **Paper mode** (`QT_ENV=paper`): a long-running process polls the same
  adapters on a schedule (e.g. once an hour), recomputes the composite
  score over the trailing window, asks the risk engine for a decision,
  and routes orders to `PaperBroker`. The supervised loop writes
  `data/runtime/monitor_state.json` every cycle so process health,
  failures, next run time, latest score, and equity are visible.
  `scripts/run_service.py` is the parent watchdog for unattended runs: it
  starts the paper loop and dashboard, restarts children that exit, and
  restarts the paper loop when the heartbeat is stale or failed.

- **Live mode** (`QT_ENV=live`, `QT_LIVE_TRADING_ENABLED=true`): same
  loop, `LiveBroker` instead of `PaperBroker`. Live broker requires
  explicit per-venue wiring; kill-switch enforced.

## Determinism & reproducibility

- All time is UTC, every Series uses a `DatetimeIndex(tz='UTC')`.
- All indicators are point-in-time (rolling windows only look back).
- The composite score is a pure function of the input series — no hidden
  state, easy to unit-test.
- Parquet snapshots + a frozen config file = a fully reproducible backtest.
- Each backtest exports `summary.json`, `equity.csv`, `trades.csv`, and
  `signals.csv` under `data/backtests/`; the dashboard reads only these
  artifacts, not in-memory state.

## Failure isolation

- **Adapter failures** never raise into the strategy. Missing data → that
  factor group is silently dropped from the score denominator. Operational
  alerts fire (`qt.monitoring.alerts`) but trading continues with reduced
  factor coverage. The `min_factor_groups` floor prevents trading on a
  small denominator.
- **Loop failures** are caught by `qt.monitoring.supervisor`, persisted to
  the heartbeat file, alerted, and retried with bounded backoff. Repeated
  failures mark the process as `failed` but the loop keeps retrying unless
  the operator stops it.
- **Process death / stale heartbeat** is handled by `scripts/run_service.py`,
  which checks `qt monitor health` semantics and restarts the paper loop if
  the heartbeat exceeds the configured staleness threshold.
- **Broker failures**: `LiveBroker.submit` retries with exponential
  backoff (max 4 attempts). After exhaustion, the kill-switch is armed
  and a critical alert fires.
- **Drawdown kill-switch**: any equity drawdown > `max_drawdown_pct`
  blocks all new entries. Manual reset only.

## Live-trading enablement checklist

1. ≥ 90 days paper-trading with `min_factor_groups ≥ 4` matching the
   simulated factor-fire distribution.
2. Walk-forward Sharpe ≥ 1.2, max DD ≤ 25% on three disjoint windows.
3. Exchange-side: 2-FA, withdrawal whitelist, IP allowlist, trade-only
   API keys (no withdrawal scope), separate research keys.
4. Per-venue: order-rejection handling, partial-fill reconciliation,
   funding-rate accrual reconciliation, websocket reconnect logic.
5. Ops: PagerDuty / Slack alert on kill-switch arm, daily PnL email,
   reconciliation report.
