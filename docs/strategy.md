# Strategy: Extreme-Event Mean Reversion (BTC, Long-Only, Low Frequency)

## Thesis

This document records a historical research hypothesis, not a validated live
strategy. Extreme selling may be followed by recovery, continued decline, or
illiquidity. Factor groups can be correlated, and missing data cannot be treated
as independent confirmation. Trade frequency and profitability have not been
established by the text below.

## Evidence status

Previous revisions included precise hit-rate, zero-false-positive and cycle-count
claims without reproducible datasets, horizons, costs or held-out evaluation.
Those claims are withdrawn. The citation to Caporale, Gil-Alana and Plastun
(2018) concerns persistence and does not substantiate the previous specific
one-day decline greater than 5% implies reversal claim.

- [Liu and Tsyvinski](https://www.nber.org/papers/w24877) study historical
  cryptocurrency returns and momentum; they do not validate these thresholds.
- [Intraday momentum and reversal research](https://www.sciencedirect.com/science/article/pii/S1062940822000833)
  motivates testing regimes separately.
- [Backtest overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
  motivates retained trials and independent evaluation.

All numerical thresholds and assumed sizing statistics below are unvalidated
engineering hypotheses. The deployed notification pipeline has its own
[versioned rules, promotion gates and operations guide](btc-alerts.md).

## Signal aggregation: 5 groups + macro veto

Five factor groups, each contributing a single 0/1 condition:

1. **Price action** — RSI(14) < 20 *or* BB-Z ≤ −2.5σ *or* lower-wick:body
   ≥ 3 *or* 30d drawdown ≥ 15% *or* high-volume capitulation down bar.
2. **Volatility** — short/long realized-vol ratio ≥ 2 (acute regime shock).
3. **Derivatives** — funding-rate Z ≤ −2 *or* funding ≤ −0.05%/8h
   sustained 3 prints *or* 24h OI drop ≥ 10% *or* global long/short ratio
   in the lowest 10th percentile.
4. **On-chain** — aSOPR < 0.97 *or* MVRV-Z < 0.5 *or* NUPL < 0 *or* Puell
   < 0.5 *or* Reserve Risk < 0.002 *or* Exchange-netflow Z ≤ −2 *or* Pi
   Cycle Bottom fired in last 72h.
5. **Sentiment** — F&G ≤ 15 sustained 3 days *or* social-sentiment Z ≤ −2.

Macro veto (must all hold to enter):

- VIX < 35
- DXY 20d ROC Z-score < 2.0

**Composite score** = (groups fired) / (groups with available data). A
configured-but-quiet group remains in the denominator; only genuinely
missing optional data is dropped.
**Entry trigger**: score ≥ `entry_score_min` (default 0.60) *and*
≥ `min_factor_groups` (default 4) groups firing *and* macro_ok.

Why **N-of-K boolean voting** instead of weighted-sum Z-scores?

- Robust to missing data — drop the group from the denominator instead of
  zero-padding.
- Measure the conjunction's false-positive rate; combining correlated factors
  does not itself establish accuracy.
- Mirrors practitioner consensus (Glassnode "Recovering from a Bitcoin
  Bear", LookIntoBitcoin composite, Pi Cycle Bottom).
- Fixed votes reduce parameter choices, but still require out-of-sample testing.

## Position sizing

Final size is the **minimum** of:

- Signal-suggested allocation (linear ramp of composite score).
- Fractional Kelly (quarter-Kelly default; Thorp 2006, MacLean/Thorp/Ziemba
  2010) using `empirical_win_rate=0.6`, `win_loss_ratio=1.5` (tunable from
  walk-forward).
- Inverse-volatility scalar to a 40% annualised vol target.
- Hard cap `max_position_pct = 0.20` (20% of equity per trade).

## Exit

- ATR-stop @ 2.5× ATR(14) below entry.
- ATR-target @ 4.0× ATR(14) above entry (reward:risk ≈ 1.6).
- Time stop after 120 bars (5 days on 1h) — extreme reversion that
  doesn't snap back within a week is no longer the trade we sized for.
- Single position; no pyramiding.

## Risk controls

- Equity-peak drawdown kill-switch at 20% — disables all new entries until
  manually reset.
- 24-bar cooldown after a losing exit.
- Live trading **disabled by default** (`QT_LIVE_TRADING_ENABLED=false`).
- Live broker requires explicit per-venue wiring (see `execution/live.py`).

## Walk-forward / validation plan

1. Train (threshold sweep) on 2017–2020; test on 2021–2022 (Luna/3AC/FTX);
   then re-test on 2023–2025.
2. Walk-forward windows: 2y train / 6m test, stepping every 3m.
3. Out-of-sample acceptance bar (minimum):
   - Sharpe ≥ 1.2, profit factor ≥ 1.6, max DD ≤ 25%.
   - ≥ 8 OOS trades across the full window (not from one regime).
   - Bootstrapped 95% CI for total return excluding zero.
4. Stress: ablate each factor group; drop any group whose removal does
   not materially worsen OOS metrics.
5. Paper-trade for ≥ 90 days *after* live data confirms the historical
   simulation distribution (compare realized vs simulated factor flags).

## Known failure modes

- **Slow-bleed bear** (e.g., mid-2022) — gradual drawdown without acute
  insertion, factors fire piecemeal but not jointly. The strategy will
  miss it; that is by design.
- **Regime change** — if macro stays risk-off (VIX > 35 for months) the
  veto blocks all entries. Correct behaviour, but means the strategy can
  go > 12 months idle.
- **Data gaps in on-chain feeds** — if Glassnode/CryptoQuant keys lapse,
  the on-chain group drops out and the score denominator shrinks, biasing
  toward looser triggers. The `min_factor_groups` floor partially mitigates
  this.
