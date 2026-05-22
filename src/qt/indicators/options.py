"""Options-derived capitulation signals.

Built on Deribit public REST (`https://www.deribit.com/api/v2`, ~20 req/s,
no auth required). DVOL is Deribit's BTC implied-vol index; 25-delta
risk-reversal (RR) and put/call OI ratio are computable from the option
book summary.

Empirical thresholds (GVol / Block Scholes / Amberdata Derivatives):

| Signal                       | Capitulation threshold | Cycle prints |
|------------------------------|------------------------|--------------|
| DVOL                         | > 95 (extreme > 100)   | Mar 2020 ~180, May 2021 ~145, Jun 2022 ~108, Nov 2022 ~106, Aug 2024 ~95 |
| 1w 25-delta put skew         | > +15 vol points       | Jun 2022 ~22, Nov 2022 ~19, Aug 2024 ~17  |
| Put/Call OI ratio (7d EWMA)  | > 1.0                  | May 2021 ~1.1, Nov 2022 ~1.05 |
| Front-month basis (CME/perp) | < 0 (backwardation)    | Mar 2020, Jun 2022, Nov 2022 |
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def dvol_extreme(dvol: pd.Series, threshold: float = 95.0) -> pd.Series:
    return (dvol >= threshold).rename("dvol_extreme")


def put_skew_extreme(skew_25d_1w: pd.Series, threshold: float = 15.0) -> pd.Series:
    """25-delta put skew (1-week) in vol points. Positive = puts richer than calls."""

    return (skew_25d_1w >= threshold).rename("put_skew_extreme")


def put_call_oi_extreme(pc_oi: pd.Series, threshold: float = 1.0,
                        smoothing: int = 7) -> pd.Series:
    """Put/call OI ratio (EWMA-smoothed) over `threshold`."""

    s = pc_oi.ewm(span=smoothing, adjust=False).mean()
    return (s >= threshold).rename("pc_oi_extreme")


def backwardation(front_basis_annualized: pd.Series, threshold: float = 0.0) -> pd.Series:
    """Annualized front-month basis below `threshold` (default = 0 = backwardation)."""

    return (front_basis_annualized <= threshold).rename("backwardation")


def gex_short_gamma(
    gex: pd.Series, threshold_z: float = -2.0, window: int = 60
) -> pd.Series:
    """Dealer net gamma exposure deeply short (Z <= -2 in rolling window).

    Dealers being short gamma below spot amplifies downside moves; the
    extreme short-gamma state correlates with reflexive cascades.
    """

    mu = gex.rolling(window).mean()
    sd = gex.rolling(window).std(ddof=0).replace(0, np.nan)
    z = (gex - mu) / sd
    return (z <= threshold_z).rename("gex_short_gamma")
