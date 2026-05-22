"""Composite extreme-event score.

Combines five factor groups — price action, volatility, derivatives,
on-chain, sentiment — plus a macro veto. Each group contributes a 0/1
condition; the final score is the share of groups that fired, gated by
the macro filter.

Why grouped boolean voting instead of weighted-sum z-scores?

1. Robust to missing data: any group whose data isn't available is dropped
   from the denominator rather than zero-padded into the score.
2. Practitioner consensus (Glassnode "Recovering from a Bitcoin Bear",
   LookIntoBitcoin composite, Pi Cycle Bottom) all use "N-of-K voting"
   rather than continuous weights — easier to reason about, harder to
   over-fit, and explainable post-hoc.
3. Individual factor weights are unstable across regimes; group identity
   is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from qt.core.config import ThresholdConfig
from qt.indicators.derivatives import (
    funding_sustained_negative,
    funding_zscore,
    oi_drop_24h,
)
from qt.indicators.events import (
    flash_crash,
    liquidation_cascade,
    wick_cluster,
)
from qt.indicators.onchain import (
    mvrv_z_extreme,
    netflow_zscore,
    nupl_capitulation,
    pi_cycle_bottom,
    puell_low,
    reserve_risk_low,
    sopr_capitulation,
)
from qt.indicators.price import bollinger_zscore, drawdown_from_high, rsi, wick_ratio
from qt.indicators.sentiment import fear_greed_extreme, social_sentiment_z
from qt.indicators.smartmoney import (
    coinbase_premium_extreme,
    coinbase_premium_index,
    ssr_oscillator,
    stablecoin_supply_ratio,
    whale_net_z,
    whale_ratio_z,
)
from qt.indicators.volatility import rv_ratio


@dataclass
class ExtremeScore:
    """Output of `compute_extreme_score`.

    Attributes:
        score: Series in [0, 1] = fraction of factor groups that fired.
        factor_flags: DataFrame of per-condition booleans (audit/debug).
        group_flags: DataFrame with one column per factor group.
        macro_ok: Series of True where macro filter allows entry.
        reasons: Human-readable summary per timestamp (only populated for
            bars above the entry threshold).
    """

    score: pd.Series
    factor_flags: pd.DataFrame
    group_flags: pd.DataFrame
    macro_ok: pd.Series
    reasons: dict[pd.Timestamp, list[str]] = field(default_factory=dict)


def compute_extreme_score(
    ohlcv: pd.DataFrame,
    funding: pd.Series | None = None,
    oi: pd.Series | None = None,
    long_liquidations_usd: pd.Series | None = None,
    sopr: pd.Series | None = None,
    mvrv_z: pd.Series | None = None,
    nupl: pd.Series | None = None,
    puell: pd.Series | None = None,
    reserve_risk: pd.Series | None = None,
    exchange_netflow: pd.Series | None = None,
    fear_greed: pd.Series | None = None,
    social_sentiment: pd.Series | None = None,
    vix: pd.Series | None = None,
    dxy: pd.Series | None = None,
    coinbase_close: pd.Series | None = None,
    btc_market_cap: pd.Series | None = None,
    stablecoin_supply: pd.Series | None = None,
    whale_ratio: pd.Series | None = None,
    whales_to_exchange_net: pd.Series | None = None,
    cfg: ThresholdConfig | None = None,
) -> ExtremeScore:
    """Compute the composite extreme-event score from heterogenous inputs.

    All optional inputs must be aligned to `ohlcv.index` (typically 1h or 1d).
    Missing inputs are simply ignored — the corresponding group is removed
    from the denominator rather than treated as 0.
    """

    cfg = cfg or ThresholdConfig()
    close = ohlcv["close"]
    open_ = ohlcv["open"]
    high = ohlcv["high"]
    low = ohlcv["low"]

    flags: dict[str, pd.Series] = {}

    # --- Price-action group ----------------------------------------------
    rsi14 = rsi(close, 14)
    bbz = bollinger_zscore(close, 20)
    wk = wick_ratio(open_, high, low, close)
    dd = drawdown_from_high(close, window=24 * 30)
    flags["price_rsi"] = rsi14 < cfg.rsi_oversold
    flags["price_bb"] = bbz <= -cfg.bb_std
    flags["price_wick"] = wk >= cfg.wick_body_ratio_min
    flags["price_dd"] = dd <= -cfg.drawdown_30d_min
    price_group = flags["price_rsi"] | flags["price_bb"] | flags["price_wick"] | flags["price_dd"]

    # --- Volatility group -------------------------------------------------
    rvr = rv_ratio(close, fast=24, slow=24 * 30)
    flags["vol_spike"] = rvr >= cfg.rv_ratio_min
    vol_group = flags["vol_spike"]

    # Default-False template aligned to the OHLCV index — used for groups
    # whose underlying data is missing entirely so the group columns still
    # have the correct DatetimeIndex (and DataFrame alignment works).
    false_template = pd.Series(False, index=ohlcv.index, dtype=bool)

    # --- Derivatives group ------------------------------------------------
    deriv_components: list[pd.Series] = []
    if funding is not None and not funding.empty:
        f = funding.reindex(ohlcv.index).ffill()
        flags["deriv_funding_z"] = funding_zscore(f).fillna(0) <= -2.0
        flags["deriv_funding_neg"] = funding_sustained_negative(
            f, bars=3, threshold=cfg.funding_rate_8h_max
        )
        deriv_components.append(flags["deriv_funding_z"] | flags["deriv_funding_neg"])
    if oi is not None and not oi.empty:
        o = oi.reindex(ohlcv.index).ffill()
        flags["deriv_oi_drop"] = oi_drop_24h(o) <= -cfg.oi_drop_24h_min
        deriv_components.append(flags["deriv_oi_drop"])
    if long_liquidations_usd is not None and not long_liquidations_usd.empty:
        ll = long_liquidations_usd.reindex(ohlcv.index).fillna(0)
        flags["deriv_liq_cascade"] = liquidation_cascade(
            ll, notional_threshold=cfg.long_liq_24h_usd, z_threshold=cfg.long_liq_z,
        )
        deriv_components.append(flags["deriv_liq_cascade"])
    # Flash-crash event always available (just OHLCV)
    flags["deriv_flash_crash"] = flash_crash(close, cfg.flash_crash_pct, cfg.flash_crash_bars)
    deriv_components.append(flags["deriv_flash_crash"])
    # Wick cluster (>=2 long lower wicks in last 6h)
    wk_series = wick_ratio(open_, high, low, close)
    flags["deriv_wick_cluster"] = wick_cluster(
        wk_series, window=6, count=2, ratio_min=cfg.wick_body_ratio_min,
    )
    deriv_components.append(flags["deriv_wick_cluster"])
    deriv_group = _combine_or(deriv_components, default=false_template)

    # --- On-chain group ---------------------------------------------------
    onchain_components: list[pd.Series] = []
    if sopr is not None and not sopr.empty:
        s = sopr.reindex(ohlcv.index).ffill()
        flags["oc_sopr_cap"] = sopr_capitulation(s, cfg.asopr_max)
        onchain_components.append(flags["oc_sopr_cap"])
    if mvrv_z is not None and not mvrv_z.empty:
        m = mvrv_z.reindex(ohlcv.index).ffill()
        flags["oc_mvrv_z"] = mvrv_z_extreme(m, cfg.mvrv_z_max)
        onchain_components.append(flags["oc_mvrv_z"])
    if nupl is not None and not nupl.empty:
        n = nupl.reindex(ohlcv.index).ffill()
        flags["oc_nupl"] = nupl_capitulation(n)
        onchain_components.append(flags["oc_nupl"])
    if puell is not None and not puell.empty:
        p = puell.reindex(ohlcv.index).ffill()
        flags["oc_puell"] = puell_low(p)
        onchain_components.append(flags["oc_puell"])
    if reserve_risk is not None and not reserve_risk.empty:
        rr = reserve_risk.reindex(ohlcv.index).ffill()
        flags["oc_reserve_risk"] = reserve_risk_low(rr)
        onchain_components.append(flags["oc_reserve_risk"])
    if exchange_netflow is not None and not exchange_netflow.empty:
        ef = exchange_netflow.reindex(ohlcv.index).ffill()
        flags["oc_netflow_z"] = netflow_zscore(ef).fillna(0) <= -2.0
        onchain_components.append(flags["oc_netflow_z"])
    # Pi Cycle Bottom event on close (daily-resampled)
    daily_close = close.resample("1D").last().dropna()
    if len(daily_close) > 471:
        pi = pi_cycle_bottom(daily_close)
        pi_aligned = pi.reindex(ohlcv.index, method="ffill").fillna(False).astype(bool)
        # Active for 3 days after fire
        active = pi_aligned.rolling(72, min_periods=1).max().astype(bool)
        flags["oc_pi_cycle"] = active
        onchain_components.append(active)
    onchain_group = _combine_or(onchain_components, default=false_template)

    # --- Sentiment group --------------------------------------------------
    sentiment_components: list[pd.Series] = []
    if fear_greed is not None and not fear_greed.empty:
        fg = fear_greed.reindex(ohlcv.index).ffill()
        flags["snt_fng"] = fear_greed_extreme(fg, cfg.fear_greed_max)
        sentiment_components.append(flags["snt_fng"])
    if social_sentiment is not None and not social_sentiment.empty:
        ss = social_sentiment.reindex(ohlcv.index).ffill()
        flags["snt_social_z"] = social_sentiment_z(ss) <= cfg.social_z_max
        sentiment_components.append(flags["snt_social_z"])
    sentiment_group = _combine_or(sentiment_components, default=false_template)

    # --- Smart-money group ------------------------------------------------
    sm_components: list[pd.Series] = []
    if coinbase_close is not None and not coinbase_close.empty:
        prem = coinbase_premium_index(coinbase_close, close)
        flags["sm_coinbase_premium"] = coinbase_premium_extreme(
            prem, sustained_bars=cfg.coinbase_premium_bars,
            threshold=cfg.coinbase_premium_extreme,
        ).reindex(ohlcv.index).fillna(False).astype(bool)
        sm_components.append(flags["sm_coinbase_premium"])
    if btc_market_cap is not None and stablecoin_supply is not None \
            and not btc_market_cap.empty and not stablecoin_supply.empty:
        ssr = stablecoin_supply_ratio(btc_market_cap, stablecoin_supply)
        ssr_z = ssr_oscillator(ssr).reindex(ohlcv.index).ffill()
        flags["sm_ssr_z"] = ssr_z <= cfg.ssr_z_max
        sm_components.append(flags["sm_ssr_z"])
    if whale_ratio is not None and not whale_ratio.empty:
        wr = whale_ratio.reindex(ohlcv.index).ffill()
        wrz = whale_ratio_z(wr)
        flags["sm_whale_ratio_z"] = wrz <= cfg.whale_ratio_z_max
        sm_components.append(flags["sm_whale_ratio_z"])
    if whales_to_exchange_net is not None and not whales_to_exchange_net.empty:
        wn = whales_to_exchange_net.reindex(ohlcv.index).ffill()
        wnz = whale_net_z(wn)
        flags["sm_whale_net_z"] = wnz <= cfg.whale_net_z_max
        sm_components.append(flags["sm_whale_net_z"])
    smart_money_group = _combine_or(sm_components, default=false_template)

    # --- Macro veto -------------------------------------------------------
    macro_ok = pd.Series(True, index=ohlcv.index)
    if vix is not None and not vix.empty:
        v = vix.reindex(ohlcv.index).ffill()
        macro_ok &= v < cfg.vix_max
    if dxy is not None and not dxy.empty:
        d = dxy.reindex(ohlcv.index).ffill()
        # DXY 20d rate-of-change Z-score; veto if breaking out hard
        roc = d.pct_change(periods=24 * 20)
        z = (roc - roc.rolling(24 * 60).mean()) / roc.rolling(24 * 60).std(ddof=0)
        macro_ok &= z.fillna(0) < cfg.dxy_z_max

    # --- Group aggregation ------------------------------------------------
    group_df = pd.DataFrame(
        {
            "price": price_group,
            "volatility": vol_group,
            "derivatives": deriv_group,
            "onchain": onchain_group,
            "sentiment": sentiment_group,
            "smart_money": smart_money_group,
        }
    ).fillna(False).astype(bool)

    # Drop columns that are *entirely* False because their data was missing.
    available = [c for c in group_df.columns if group_df[c].any()]
    denom = max(len(available), 1)
    fired = group_df[available].sum(axis=1).astype(float)
    score = (fired / denom).where(macro_ok, 0.0).rename("extreme_score")

    factor_df = pd.DataFrame(flags).fillna(False).astype(bool)

    # Reasons for bars above entry threshold (sparse — only for inspection)
    reasons: dict[pd.Timestamp, list[str]] = {}
    hi = score[score >= 0.5]
    for ts in hi.index:
        reasons[ts] = [k for k, v in flags.items() if bool(v.get(ts, False))]

    return ExtremeScore(
        score=score,
        factor_flags=factor_df,
        group_flags=group_df,
        macro_ok=macro_ok,
        reasons=reasons,
    )


def _combine_or(components: list[pd.Series], default: pd.Series | None = None) -> pd.Series:
    if not components:
        if default is not None:
            return default.copy()
        return pd.Series(dtype="bool")
    out = components[0].copy().astype(bool)
    for c in components[1:]:
        out |= c.astype(bool)
    return out
