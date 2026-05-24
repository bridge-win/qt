# Indicator catalog

Each indicator below ships as a pure function in `qt.indicators.*`. All
thresholds are configurable via `ThresholdConfig` (see `config/default.yaml`).

## Price action (`qt.indicators.price`)

| Indicator | Computation | Extreme threshold | Notes |
|---|---|---|---|
| RSI(14) | Wilder, EWMA alpha=1/14 | < 20 | Classical Wilder oversold. |
| Bollinger Bands Z | (close − 20D MA) / 20D σ | ≤ −2.5 | 20D for daily / 480-bar for 1h. |
| ATR(14) | EWMA of True Range | — | Stop sizing only. |
| 30d Drawdown | (close − 30D high) / 30D high | ≤ −15% | Caporale et al. 2018 reversal threshold. |
| Wick / body ratio | lower_wick / abs(close−open) | ≥ 3 | Hammer / 插针 (insertion) detector. |
| Volume capitulation | volume Z-score plus same-bar down move | Z ≥ 2 and return ≤ −5% | Filters for forced selling rather than quiet drift. |

## Volatility (`qt.indicators.volatility`)

| Indicator | Computation | Extreme threshold | Notes |
|---|---|---|---|
| Realized vol | std(log returns) × √(ann factor) | — | Window 24h / 30d. |
| RV ratio | RV(fast) / RV(slow) | ≥ 2 | Acute regime spike detector. |
| Parkinson vol | high-low estimator | — | Less noisy than C2C. |

## Derivatives (`qt.indicators.derivatives`)

| Indicator | Source | Extreme threshold | Notes |
|---|---|---|---|
| Funding rate Z | Binance fapi `/fundingRate` | Z ≤ −2 | 90-print rolling window. |
| Funding sustained negative | same | ≤ −0.05%/8h for 3 prints | Forced short squeeze setup. |
| 24h OI change | `/openInterestHist` | ≤ −10% | Liquidation cascade flag. |
| Long/Short ratio percentile | `/globalLongShortAccountRatio` | ≤ 10th percentile | Crowded-short contrarian read. |

## On-chain (`qt.indicators.onchain`)

| Indicator | Free source | Paid source | Threshold | Citation |
|---|---|---|---|---|
| MVRV Z-score | Coin Metrics (compute) | Glassnode | < 0.5 | Awe & Mahmudov, 2018 |
| aSOPR | — | Glassnode | < 0.97 | Shirakashi / Glassnode |
| LTH-SOPR | — | Glassnode | < 0.7 | Glassnode |
| NUPL | — | Glassnode | < 0 | Glassnode |
| Puell Multiple | Coin Metrics (compute) | Glassnode | < 0.5 | Puell, 2019 |
| Reserve Risk | — | Glassnode | < 0.002 | Hauge, 2019 |
| Exchange Netflow Z | CryptoQuant | Glassnode | Z ≤ −2 | CryptoQuant |
| Pi Cycle Bottom | OHLCV only | — | crossing event | Swift / LookIntoBitcoin |
| Hash Ribbons recovery | Coin Metrics (HashRate) | Glassnode | 30D × 60D cross-up | Edwards / Capriole, 2019 |

## Sentiment (`qt.indicators.sentiment`)

| Indicator | Source | Threshold | Notes |
|---|---|---|---|
| Fear & Greed | alternative.me (free, daily since 2018) | ≤ 15 sustained 3d | F&G classic. |
| Social sentiment Z | Santiment GraphQL | Z ≤ −2 | `sentiment_weighted_total_btc`. |
| Social volume Z | Santiment | — | Divergence pattern with above. |

## Macro veto

| Indicator | Source | Threshold | Notes |
|---|---|---|---|
| VIX | FRED `VIXCLS` / Yahoo `^VIX` | < 35 | Risk-off regime block. |
| DXY 20d ROC Z | FRED `DTWEXBGS` | Z < 2 | Dollar breakout block. |
| US 10Y | FRED `DGS10` | — | Observed but not vetoed. |

## Composite

`qt.indicators.composite.compute_extreme_score` produces:

- **score** ∈ [0,1] — fraction of factor groups that fired.
- **group_flags** — boolean DataFrame, one column per group.
- **factor_flags** — boolean DataFrame, one column per atomic factor.
- **macro_ok** — boolean Series.
- **reasons** — dict[ts → list[str]] for inspection of high-score bars.

Groups with available data stay in the denominator even when they do not
fire. Only truly missing optional groups are dropped. This prevents a
quiet non-confirming feed from accidentally inflating the score.

The signal engine (`qt.signal.engine.SignalEngine`) converts this into
sparse `Signal` objects with explainable factor lists for audit and live
operations.
