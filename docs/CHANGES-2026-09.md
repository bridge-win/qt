# 2026-09 review & optimisation pass

Scope: make the three horizons (weekly / monthly / cycle) actually
operable, fix the bugs that silently disabled factor groups, and close the
data gaps that matter most, using free sources wherever possible.

## Bugs fixed

| # | Where | Problem | Fix |
|---|---|---|---|
| 1 | `strategies/capitulation.py`, `scripts/run_paper.py` | Raw Coin Metrics MVRV *ratio* (bottoms ~0.7) was passed as MVRV-**Z** (threshold <0.5) → on-chain group could never fire in live mode; backtests read Glassnode (empty without key) → live and backtest had different denominators | New `data.onchain.fetch_coinmetrics_mvrv_z()` computes the real Z-score + NUPL from free `CapMrktCurUSD`/`CapRealUSD`. Backtest readers fall back to `onchain/coinmetrics_derived` when Glassnode is absent (`ParquetStore.read_column`). |
| 2 | `strategies/capitulation.py` | Macro veto (VIX/DXY) never fetched in live mode → `macro_ok` always True | `data.macro.fetch_yahoo_daily()` (keyless): `^VIX` and true ICE DXY `DX-Y.NYB`; FRED kept as VIX fallback. FRED `DTWEXBGS` is *not* DXY. |
| 3 | `indicators/composite.py` | All rolling windows hard-coded for 1h bars (`24*30`, `24*20`, `72`) → wrong on 4h/1d | `bars_per_day(index)` infers the timeframe; every window is now expressed in days x bpd. Verified on 1h and 1d. |
| 4 | derivatives group | Liquidations adapter existed but nothing consumed it | `long_liq_usd`/`short_liq_usd` are composite inputs; `liquidation_cascade()` = long-liq Z >= 3 **and** long-dominance >= 70 %. Coinglass key -> aggregated feed; otherwise the local WebSocket recorder file. |
| 5 | F&G | "<=15 for 3 days" measured in *bars* on hourly data (=3 hours) | `fear_greed_sustained_days x bpd` |

## New capabilities

- **`compute_overheat_score()`** — symmetric top-side composite: price
  (RSI>80 / BB-Z>=2.5 / ATR-displacement>=3 / 30d run-up>=40 %),
  derivatives (funding Z>=2, sustained +0.05 %, OI +15 %/24h, short-liq spike),
  on-chain (MVRV-Z>7, NUPL>0.75, Pi Cycle Top), sentiment (F&G>=80 x 3d).
  No volatility group on purpose: tops are low-vol processes.
- **`overheat` strategy** (weekly cadence, *sell/trim* side; never shorts).
  When the on-chain group is among the firing groups it is flagged
  cycle-top class.
- **`indicators/cycle.py` + `cycle` strategy** — 0-100 cycle position from
  MVRV-Z, NUPL, Mayer Multiple, 200-week multiple, halving clock, with Pi
  Cycle Top/Bottom events and a piece-wise target allocation
  (100 % at <=15 -> 10 % at 100). Fixed bands, not fitted.
  `scripts/cycle_report.py` prints it with a per-horizon action sheet.
- **`trend` strategy** — Mayer Multiple and Capriole Hash Ribbons as
  monthly confirmation (confidence 0.95 after a ribbon buy, 0.6 when
  Mayer>2.4); vol-shock filter is timeframe-aware; history 2y.
- **`atr_displacement()`** — `(close-SMA20)/ATR14`, regime-stable
  overshoot gauge used on both sides.
- **Data**: DefiLlama stablecoin supply (for SSR), Coinglass spot-ETF
  flows, `scripts/record_derivatives.py` (hourly OI/LSR/funding
  accumulation - Binance only keeps 30 d), `scripts/record_liquidations_ws.py`
  (Binance `forceOrder` -> hourly long/short liquidation bars).

## What is still missing (priority order)

1. **ETF net flows in a factor** - adapter exists (`fetch_etf_flows`),
   not yet a composite input; since 2024 this is the marginal buyer.
2. **aSOPR / Reserve Risk / exchange net-flow** still need Glassnode or
   CryptoQuant. NUPL and MVRV-Z no longer do.
3. **Deribit DVOL + 25d skew** adapters exist; a "low DVOL + low put
   skew + high funding" group belongs in `compute_overheat_score`.
4. **Walk-forward re-validation** of `capitulation` now that the on-chain
   group and liquidations are live - thresholds were tuned with the
   group dead.
5. `legacy/` (32 k lines) should leave the main package; the old
   `dashboard/server.py` and the v3 workbench should converge.

## Data reliability notes

- Liquidation feeds (all vendors) are sampled: Binance pushes <=1
  forceOrder/s per symbol since 2021. Use Z-scores, never USD levels.
- Binance funding is single-venue; Coinglass OI-weighted aggregate is
  better when a key exists (`coinglass_BTC_funding_agg`).
- F&G is a proprietary composite that already contains price and vol;
  treat as confirmation, not a primary factor.
- Yahoo chart endpoint is unofficial; if it breaks, set `QT_FRED_API_KEY`
  for VIX (DXY has no FRED equivalent).
