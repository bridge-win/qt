"""On-chain indicators and bottom-detector thresholds.

Threshold defaults come from public practitioner research (Glassnode,
CryptoQuant, Capriole, LookIntoBitcoin) — see `docs/indicators.md` for
citations. They should be re-tuned via walk-forward backtesting before
live capital is committed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def mvrv_z_from_caps(market_cap: pd.Series, realized_cap: pd.Series,
                     window: int = 365 * 2) -> pd.Series:
    """MVRV Z-Score: (MC - RC) / rolling_std(MC). Window ~2y of daily data."""

    diff = market_cap - realized_cap
    std = market_cap.rolling(window).std(ddof=0).replace(0, np.nan)
    return (diff / std).astype("float64").rename("mvrv_z")


def mvrv_z_extreme(mvrv_z: pd.Series, threshold: float = 0.5) -> pd.Series:
    return (mvrv_z < threshold).rename("mvrv_z_extreme")


def sopr_capitulation(sopr: pd.Series, threshold: float = 0.97) -> pd.Series:
    """aSOPR < threshold = market-wide forced loss realization (capitulation)."""

    return (sopr < threshold).rename("sopr_capitulation")


def lth_sopr_capitulation(lth_sopr: pd.Series, threshold: float = 0.7) -> pd.Series:
    return (lth_sopr < threshold).rename("lth_sopr_capitulation")


def puell_low(puell: pd.Series, threshold: float = 0.5) -> pd.Series:
    return (puell < threshold).rename("puell_low")


def reserve_risk_low(rr: pd.Series, threshold: float = 0.002) -> pd.Series:
    return (rr < threshold).rename("reserve_risk_low")


def netflow_zscore(netflow: pd.Series, window: int = 30) -> pd.Series:
    mu = netflow.rolling(window).mean()
    sd = netflow.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((netflow - mu) / sd).astype("float64").rename("netflow_z")


def hash_ribbon_recovery(hashrate: pd.Series) -> pd.Series:
    """Charles Edwards Hash Ribbons: 30d MA crosses above 60d MA after a death cross.

    This implementation returns True on bars where 30d MA > 60d MA AND the
    previous bar had 30d <= 60d (cross-up event).
    """

    ma30 = hashrate.rolling(30).mean()
    ma60 = hashrate.rolling(60).mean()
    cross = (ma30 > ma60) & (ma30.shift(1) <= ma60.shift(1))
    return cross.rename("hash_ribbon_recovery")


def pi_cycle_bottom(close: pd.Series, fast: int = 150, slow: int = 471,
                    slow_mult: float = 0.745) -> pd.Series:
    """Pi Cycle Bottom: 471d SMA x 0.745 crosses above 150d EMA.

    Fired within 3 days from the actual Mar-2020 low; no false positives 2013-2023
    on daily data per Philip Swift / LookIntoBitcoin.
    """

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    sma_slow = close.rolling(slow).mean() * slow_mult
    cross = (sma_slow > ema_fast) & (sma_slow.shift(1) <= ema_fast.shift(1))
    return cross.rename("pi_cycle_bottom")


def nupl_capitulation(nupl: pd.Series, threshold: float = 0.0) -> pd.Series:
    return (nupl < threshold).rename("nupl_capitulation")
