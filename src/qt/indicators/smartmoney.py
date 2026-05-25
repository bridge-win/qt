"""Smart money / whale-tracking indicators.

Practitioner-validated signals (citations in `docs/indicators.md`):

- **Coinbase Premium Index**: (Coinbase USD price - Binance USDT price) / Coinbase.
  Sustained negative premium during a crash = US institutional capitulation
  and is historically associated with local bottoms (Jun 2022, Nov 2022 FTX,
  Mar 2023 SVB). Cite: CryptoQuant / Ki Young Ju.

- **Stablecoin Supply Ratio (SSR)**: BTC market cap / total stablecoin supply.
  SSR < 5 means stablecoin "dry powder" is abundant relative to BTC cap; the
  SSR Oscillator (z-score) at -1σ has tagged every major bottom 2020-2023.
  Cite: Glassnode (Lopp / Saidler, 2020).

- **Whale Ratio**: top-10 inflow / total inflow on exchanges. CryptoQuant
  practitioner threshold: high ratio (> 0.85) = whale-dominated inflows =
  potential distribution (top); low ratio (< 0.4) = retail-dominated =
  capitulation. Use Z-score for regime-relative.

- **Accumulation Trend proxy**: rolling Spearman correlation between price
  and exchange-balance change. Negative correlation = whales buying into
  drawdown (accumulation). Glassnode's "Accumulation Trend Score" is the
  closed-source version of this idea.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def coinbase_premium_index(
    coinbase_close: pd.Series, binance_close: pd.Series
) -> pd.Series:
    """Returns (Coinbase USD - Binance USDT) / Coinbase as a per-bar fraction.

    Aligns on intersection of indices. Sustained values <= -0.0005 during a
    drawdown are the practitioner signal for US-institutional capitulation.
    """

    cb = coinbase_close.dropna()
    bn = binance_close.reindex(cb.index).ffill()
    return ((cb - bn) / cb).rename("coinbase_premium")


def coinbase_premium_extreme(
    premium: pd.Series, sustained_bars: int = 4, threshold: float = -0.0005
) -> pd.Series:
    """True when premium has been <= threshold for `sustained_bars` consecutive readings."""

    cond = (premium <= threshold).astype(int)
    return (cond.rolling(sustained_bars).sum() >= sustained_bars).rename(
        "coinbase_premium_extreme"
    )


def stablecoin_supply_ratio(
    btc_market_cap: pd.Series, stablecoin_supply: pd.Series
) -> pd.Series:
    """SSR = BTC mcap / stablecoin supply. Lower = more buying power available."""

    sc = stablecoin_supply.reindex(btc_market_cap.index).ffill()
    return (btc_market_cap / sc.replace(0, np.nan)).rename("ssr")


def ssr_oscillator(ssr: pd.Series, window: int = 200) -> pd.Series:
    """Z-score of SSR over `window` days. -1σ practitioner buy-zone threshold."""

    mu = ssr.rolling(window).mean()
    sd = ssr.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((ssr - mu) / sd).rename("ssr_z")


def whale_ratio(top10_inflow: pd.Series, total_inflow: pd.Series) -> pd.Series:
    """Whale inflow share. Low values = retail-dominated flows = bottom-like.

    CryptoQuant convention. Smooth with EWMA before threshold to reduce noise.
    """

    return (top10_inflow / total_inflow.replace(0, np.nan)).clip(0, 1).rename(
        "whale_ratio"
    )


def whale_ratio_z(wr: pd.Series, window: int = 30) -> pd.Series:
    mu = wr.rolling(window).mean()
    sd = wr.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((wr - mu) / sd).rename("whale_ratio_z")


def accumulation_trend(
    price: pd.Series, exchange_balance: pd.Series, window: int = 30
) -> pd.Series:
    """Rolling Spearman correlation between price and exchange-balance change.

    Strong negative correlation = whales accumulating during drawdowns. We
    return the correlation directly; thresholds <= -0.5 over a 30D window
    is the practitioner read for "accumulation regime".
    """

    bal = exchange_balance.reindex(price.index).ffill()
    bal_chg = bal.diff()
    # Spearman via rank → Pearson on ranks
    pr = price.rolling(window).rank(pct=True)
    br = bal_chg.rolling(window).rank(pct=True)
    return pr.rolling(window).corr(br).rename("accum_trend")


def whale_to_exchange_net(
    whales_to_exchange: pd.Series, whales_from_exchange: pd.Series
) -> pd.Series:
    """Net whale flow TO exchanges. Negative = whales withdrawing (bullish).

    Both inputs expected in BTC, daily granularity.
    """

    s = (whales_to_exchange - whales_from_exchange).rename("whale_net_to_exchange")
    return s


def whale_net_z(net_flow: pd.Series, window: int = 30) -> pd.Series:
    """Z-score of net whale flow. Z <= -2 = aggressive whale withdrawals."""

    mu = net_flow.rolling(window).mean()
    sd = net_flow.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((net_flow - mu) / sd).rename("whale_net_z")
