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
2. **Volatility** — short/long realized-vol ratio ≥ 2
3. **Derivatives** — funding Z≤-2 (or sustained negative) or OI drop ≥10 %
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
  monitoring/    reporting, alerts
  cli.py         `qt data fetch-ohlcv`, `qt backtest`, `qt info`
config/          default.yaml, thresholds_research.yaml
docs/            strategy.md, indicators.md, architecture.md
scripts/         fetch_history.py, run_backtest.py, run_paper.py
tests/           ≥ 8 unit/integration tests w/ synthetic fixture crashes
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. backfill data into data/parquet/
python scripts/fetch_history.py --days 1095

# 2. run a backtest
qt backtest --config config/default.yaml

# 3. paper-trade loop (1h cycles)
python scripts/run_paper.py --interval 3600
```

Free tier covers OHLCV + derivatives + Coin Metrics on-chain + Fear &
Greed. Glassnode / Santiment / FRED keys unlock more factors; missing
factors silently drop out of the score denominator.

## Safety

- Live trading is **disabled** until `QT_LIVE_TRADING_ENABLED=true` and
  per-venue `LiveBroker.submit` is wired explicitly.
- Drawdown kill-switch at 20 % blocks new entries; manual reset only.
- All decisions are explainable: each `Signal` carries the firing factors
  for audit.

See [`docs/architecture.md`](docs/architecture.md) for the live-trading
enablement checklist and [`docs/strategy.md`](docs/strategy.md) for the
walk-forward validation plan that must be passed before deploying capital.

## Solution gallery — four ready-to-pick retail strategies

`solution2.md` collects the research; `src/qt/strategies/` ships four
independently-runnable strategies aimed at the "single-operator, low
budget, low frequency" retail crypto trader. Each is a self-contained
class with a long docstring explaining signal, parameters, references,
and known failure modes. Pick one — or combine — based on your risk
profile.

| ID | Class | Frequency | What it does | When to choose it |
| --- | --- | --- | --- | --- |
| **A** | `SmartDCA`     | 1×/week (52/yr)  | Volatility-aware DCA: weekly buy amount scales 0.25×–3× with a composite stress score built from F&G, MVRV-Z, drawdown, and 200d MA distance. Optional Pi-Cycle-Top trim. | "I just want to accumulate BTC long-term and have the system buy more when everyone else is panicking." |
| **B** | `Capitulation` | 0–5/yr           | Multi-factor extreme-event mean reversion. Wraps `compute_extreme_score`, adds 3-tranche entry, ATR trailing stop, MVRV-Z mean-revert exit, and post-trade cooldown. | "I'll wait six months to a year for one statistically extreme buy and exit when momentum confirms." |
| **C** | `WeeklyTrend`  | 2–8/yr           | Faber/Clenow style: long when weekly close > SMA(20w); inverse-vol sizing to a 15% annual target; ATR(14w)×3 trailing stop; vol-shock filter to skip panic breakouts. | "I want to hold during bull markets and sit in cash during bear markets — robust, no on-chain data needed." |
| **D** | `BasisCarry`   | 8–20/yr (typ.)   | Market-neutral spot+perp carry. Enter when annualized funding ≥ 15%; exit when ≤ 5% or funding flips negative for several periods. Funding accrues to short leg each bar. | "I want a low-volatility yield product that doesn't bet on price direction." |

### Pick one — or blend

| Risk profile | Suggested mix |
| --- | --- |
| Fully passive accumulator | A 80% + B 20% |
| Balanced | A 40% + C 40% + D 20% |
| Active / aggressive | C 60% + B 30% + D 10% |

Carver's *Systematic Trading* framework applies: strategy correlations
empirically run below 0.4, so a blend strictly improves Sharpe vs the
best single strategy.

### Run a strategy

```bash
# (after `qt data fetch-ohlcv`, plus optional fetch-onchain / fetch-fear-greed)
qt strategy run dca           # solution A
qt strategy run capitulation  # solution B
qt strategy run trend         # solution C
qt strategy run carry         # solution D  (requires funding-rate history)
```

Each prints final equity, x-multiple, max drawdown, and trade count.
See `tests/test_strategies_gallery.py` for synthetic-data examples of
each.

> ⚠️ All four backtests use *synthetic or local* data. Before deploying
> capital, run them through `qt.backtest.walkforward` and
> `qt.backtest.montecarlo` (see `scripts/walk_forward.py` and
> `scripts/stress_test.py`).

## Tests

```bash
pytest -q
```
