# Data sources, research references, and the provenance rule

Rule (2026-09-10): **no bare numbers.** Every threshold, band edge, weight
and constant in the system lives in `qt/core/provenance.py` with a
`method` label and at least one public source. Anything displayed to a user
must show the value *and* its method/source (`qt provenance`,
`GET /api/v3/provenance`, `scripts/cycle_report.py`, and the
`factor_provenance` / `provenance` fields in strategy metrics).

Method labels: `observed` (read from a public dataset) · `computed`
(deterministic formula on public data at run time) · `literature`
(published by the indicator's author / vendor reference page) · `fitted`
(estimated by us, estimator + n + residual stated) · `assumed` (design
choice, uncalibrated — flagged for replacement).

`qt provenance` current census: 54 params → the `assumed` ones are the
work list for walk-forward re-fitting.

---

## 1. Live data feeds (all free unless noted)

| Series | Source | Frequency / depth | Reliability notes |
|---|---|---|---|
| BTC/USD daily OHLCV | Bitstamp via ccxt | daily since 2011-08 | Longest continuous venue series; used for 200W SMA, Mayer, Pi Cycle, cycle extrema |
| BTC/USDT 1h OHLCV | Binance via ccxt | 1h since 2017-08 | Short-horizon composites |
| Market cap, realized cap, hash rate | Coin Metrics Community v4 | daily since 2010 | MVRV-Z and NUPL are **computed** here from `CapMrktCurUSD`/`CapRealUSD`; matches Glassnode definitions |
| Funding rate | Binance fapi | 8h, full history | Single venue; Coinglass OI-weighted aggregate preferred when keyed |
| Open interest, long/short ratio | Binance fapi | 5m–1h, **30 days only** | Self-accumulated by `scripts/record_derivatives.py` |
| Liquidations | Binance `forceOrder` WS (self-recorded) / Coinglass v4 (paid) | 1h bars | **All feeds are sampled**: Binance emits ≤1 order/s per symbol since 2021-04. Only Z-scores are used, never USD levels |
| Fear & Greed | alternative.me | daily since 2018-02-01 | Proprietary composite (vol 25 %, momentum/volume 25 %, social 15 %, surveys 15 %, dominance 10 %, trends 10 %) — collinear with price; confirmation only |
| VIX, DXY | Yahoo chart API (`^VIX`, `DX-Y.NYB`) | daily | Unofficial endpoint; FRED `VIXCLS` fallback for VIX. FRED has no true DXY (`DTWEXBGS` is the broad TWI) |
| Stablecoin supply | DefiLlama | daily | For SSR = BTC cap / stablecoin cap |
| Spot-ETF net flows | Coinglass v4 (paid) | daily since 2024-01 | Adapter only; not yet a factor |

## 2. Observed cycle values (public, cited)

Used as the *literature* fallback and as a sanity check for
`calibrate_bands()`. The system re-derives these from the data at run time.

| Cycle top | Price | MVRV ratio | MVRV-Z | Mayer | Pi Cycle Top fired? | Days after halving |
|---|---|---|---|---|---|---|
| 2013-12 | ~$1,150 | 5.4 | >7 | ~3.2 | yes (2013-12-03) | — |
| 2017-12-16 | $19,641 | 3.9 | 9.4 | 3.4 | yes (2017-12-16) | 525 |
| 2021-04 / 2021-11-08 | $64.8k / $67.5k | 3.7 | 7.3 / 6.4 | 2.0 | Apr yes (04-12), Nov **no** | 546 |
| 2025-10-06 | $124.8k–126.3k | ~2.5 | never red | ~1.18 | **no** | 534 |

| Cycle bottom | MVRV ratio | MVRV-Z | Mayer | vs 200W SMA |
|---|---|---|---|---|
| 2015-01 | <1.0 | <0 | <0.8 | touched |
| 2018-12 | ~0.85 | −0.2 | <0.8 | touched |
| 2020-03 | ~0.85 | <0 | <0.8 | touched |
| 2022-11 | ~0.72 | <0 | <0.8 | touched |
| 2026 (in progress) | ~1.2–1.5 (Mar–Aug) | ~0.2 (Jul) / −2.5 (Amberdata, Oct-25 crash) | 0.83 (Jun) | 200W ≈ $58–61k (Q1-26) |

Sources: Spark glossary (MVRV tables); Glassnode Studio Mayer chart
(2.4 / 0.8); newhedge / bitcoin.com Pi Cycle signal history; Amberdata
"Onchain Valuation … 2026" and bit.com "MVRV Explained" (2025 cycle never
reached euphoria bands); Phemex 200W-MA article (Q1-2026 level);
arXiv 2607.26188 (halving-clock table: tops 525/546/534 d after halving).
Full URLs are in `qt/core/provenance.py::SOURCES`.

**Why this matters for the code:** literature top bands (MVRV-Z > 7,
NUPL > 0.75, Mayer > 2.4, Pi Cycle) all *missed* the October 2025 top.
Peaks have declined every cycle. `calibrate_bands()` therefore projects the
current-cycle top edge with an OLS log-linear decay over the observed
peaks (n = 3–4, residual reported) rather than using the literature
constants, which are kept only as labelled fallbacks.

## 3. Research references

| Topic | Reference | What we take from it |
|---|---|---|
| Momentum as a priced crypto factor | Liu, Tsyvinski, Wu (2022) *Common Risk Factors in Cryptocurrency*, J. Finance 77(2) | Justifies the trend sleeve (20W SMA) as a primary, not auxiliary, strategy |
| Liquidation cascades | *Anatomy of a Crypto Cascade* (SSRN 6579278), minute-level Oct-10-2025 | −12.6 % in ~10 min, volume 22× — sets the flash-crash detector floor; cascade and tail risk are the same event |
| Funding extremes | Kraken Learn, funding-rate strategy | 2021 peak 0.15–0.20 %/8h before 10–30 % corrections; deep negative funding in uptrends precedes rebounds |
| Halving clock | arXiv 2607.26188 | Top phase ≈ 0.36–0.37 of the cycle; bottom ≈ 1 y after top; pre-registered Oct–Nov 2026 bottom window |
| MVRV-Z | Awe & Wonder / Puell / Mahmudov (2018) | Z formula and 7 / 0 bands |
| Mayer Multiple | Trace Mayer (2015); Glassnode reference chart | 2.4 / 0.8 bands from the author's simulation |
| Pi Cycle Top | Philip Swift (2019) | 111D vs 2×350D; four hits, two misses |
| Hash Ribbons | Capriole (2019) | 30/60D hash-rate cross after miner capitulation |
| F&G construction | alternative.me methodology page | Component weights → why it is confirmation-only |

## 4. Still not public / not yet wired

- aSOPR, Reserve Risk, exchange net-flow, Coinbase premium: Glassnode /
  CryptoQuant paid. NUPL and MVRV-Z no longer need them.
- Deribit DVOL / 25Δ skew: adapter exists, not a factor yet.
- ETF flows: adapter exists, not a factor yet.
- All `assumed` thresholds: to be replaced by `qt backtest --walkforward`
  outputs; the experiment registry records every trial for DSR/PBO.
