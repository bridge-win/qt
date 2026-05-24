# Strategy: Extreme-Event Mean Reversion (BTC, Long-Only, Low Frequency)

## Thesis

Bitcoin spot exhibits **strong short-horizon mean reversion after extreme,
liquidation-driven drawdowns** while behaving close to a momentum/random-
walk asset in normal regimes. We deploy capital only when multiple
*independent* factor groups all signal capitulation, and only when macro
conditions do not contradict. This is intentionally a low-frequency
strategy (target: 2–10 trades / year) — opportunity is rare; quality is
the moat.

## Empirical support

| Source | Finding | Threshold used |
|---|---|---|
| Caporale, Gil-Alana & Plastun (2018), *Research in Int. Business & Finance* | Statistically significant short-horizon reversal after BTC one-day declines > 5%. | Drawdown filter |
| Ardia, Bluteau & Rüede (2019), *Finance Research Letters* | Markov-switching GARCH on BTC: low-vol regime entry probability > 0.7 follows crashes. | Vol regime confirmation |
| Gkillas & Katsiampa (2018), *Economics Letters* | EVT/GPD: extreme-tail BTC moves cluster and revert. | RV-ratio, drawdown |
| Shu, Song & Zhu (2021), *Physica A* | LPPL confidence indicator timed the Mar-2020 trough. | Implied in price-action group |
| Renato Shirakashi / Glassnode | aSOPR < 0.97 → forced loss realization (capitulation). | On-chain group |
| Awe & Mahmudov (2018) | MVRV Z-Score < 0.5 → cycle-bottom zone (4/4 in 2015–2023). | On-chain group |
| David Puell (2019) | Puell Multiple < 0.5 → miner capitulation (4/4 cycles). | On-chain group |
| Hans Hauge (2019) | Reserve Risk < 0.002 → only ~3% of all days. | On-chain group |
| Edwards (2019), Capriole | Hash Ribbons cross-up after miner capitulation → high-quality bottom signal. | On-chain group |
| Philip Swift, LookIntoBitcoin | Pi Cycle Bottom indicator: 0 false positives 2013–2023. | On-chain group |
| alternative.me Fear & Greed | F&G ≤ 15 sustained 3d → forward-return mean positive 85% historically. | Sentiment group |
| Coinglass / Skew | OI single-day drop ≥ 15–20% → 1–3 day reversal hit rate ~75%. | Derivatives group |

We deliberately do not use any of these signals in isolation. The N-of-K
voting design (see *Aggregation* below) means each factor is a noisy vote
and only their joint occurrence triggers a trade.

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
- Each factor in isolation has high false-positive rate; their conjunction
  doesn't.
- Mirrors practitioner consensus (Glassnode "Recovering from a Bitcoin
  Bear", LookIntoBitcoin composite, Pi Cycle Bottom).
- Resistant to over-fitting individual factor weights across regimes.

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
