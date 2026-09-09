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
    funding_sustained_positive,
    funding_zscore,
    liquidation_cascade,
    long_short_extreme,
    oi_drop_24h,
    oi_surge_24h,
)
from qt.indicators.onchain import (
    mvrv_z_extreme,
    mvrv_z_overheated,
    netflow_zscore,
    nupl_capitulation,
    nupl_euphoria,
    pi_cycle_bottom,
    pi_cycle_top,
    puell_low,
    reserve_risk_low,
    sopr_capitulation,
)
from qt.indicators.price import (
    atr_displacement,
    bollinger_zscore,
    drawdown_from_high,
    rsi,
    volume_capitulation,
    wick_ratio,
)
from qt.indicators.sentiment import (
    fear_greed_extreme,
    fear_greed_greed_extreme,
    social_sentiment_z,
)
from qt.indicators.volatility import rv_ratio


def bars_per_day(index: pd.Index) -> int:
    """Infer bars/day from a DatetimeIndex (1h→24, 4h→6, 1d→1).

    All rolling windows in this module are expressed in *days* and
    scaled by this so the same thresholds work on hourly and daily bars.
    """

    if len(index) < 3:
        return 24
    deltas = pd.Series(index[1:]).sub(pd.Series(index[:-1])).dt.total_seconds()
    step = float(deltas.median())
    if step <= 0:
        return 24
    return max(1, round(86400.0 / step))


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
    long_short_ratio: pd.Series | None = None,
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
    long_liq_usd: pd.Series | None = None,
    short_liq_usd: pd.Series | None = None,
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
    bpd = bars_per_day(ohlcv.index)

    # --- Price-action group ----------------------------------------------
    rsi14 = rsi(close, 14)
    bbz = bollinger_zscore(close, 20)
    wk = wick_ratio(open_, high, low, close)
    dd = drawdown_from_high(close, window=bpd * 30)
    disp = atr_displacement(close, high, low)
    flags["price_atr_disp"] = disp <= -cfg.atr_disp_extreme
    flags["price_rsi"] = rsi14 < cfg.rsi_oversold
    flags["price_bb"] = bbz <= -cfg.bb_std
    flags["price_wick"] = wk >= cfg.wick_body_ratio_min
    flags["price_dd"] = dd <= -cfg.drawdown_30d_min
    flags["price_volume_cap"] = volume_capitulation(
        close,
        ohlcv["volume"],
        min_volume_z=cfg.volume_z_min,
        min_drop=cfg.volume_drop_min,
    )
    price_group = (
        flags["price_rsi"]
        | flags["price_bb"]
        | flags["price_wick"]
        | flags["price_dd"]
        | flags["price_volume_cap"]
        | flags["price_atr_disp"]
    )

    # --- Volatility group -------------------------------------------------
    rvr = rv_ratio(close, fast=max(bpd, 2), slow=max(bpd * 30, 10))
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
        flags["deriv_oi_drop"] = oi_drop_24h(o, bars_24h=bpd) <= -cfg.oi_drop_24h_min
        deriv_components.append(flags["deriv_oi_drop"])
    if long_short_ratio is not None and not long_short_ratio.empty:
        lsr = long_short_ratio.reindex(ohlcv.index).ffill()
        flags["deriv_lsr_crowded_short"] = (
            long_short_extreme(lsr, window=bpd * 30) <= cfg.lsr_percentile_max
        )
        deriv_components.append(flags["deriv_lsr_crowded_short"])
    if long_liq_usd is not None and not long_liq_usd.empty:
        ll = long_liq_usd.reindex(ohlcv.index).fillna(0.0)
        sl = short_liq_usd.reindex(ohlcv.index).fillna(0.0) if short_liq_usd is not None else None
        flags["deriv_liq_cascade"] = liquidation_cascade(
            ll, sl, z_min=cfg.long_liq_z, window=bpd * 30,
        )
        deriv_components.append(flags["deriv_liq_cascade"])
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
        active = pi_aligned.rolling(bpd * 3, min_periods=1).max().astype(bool)
        flags["oc_pi_cycle"] = active
        onchain_components.append(active)
    onchain_group = _combine_or(onchain_components, default=false_template)

    # --- Sentiment group --------------------------------------------------
    sentiment_components: list[pd.Series] = []
    if fear_greed is not None and not fear_greed.empty:
        fg = fear_greed.reindex(ohlcv.index).ffill()
        # F&G is daily; "sustained N days" must be expressed in bars
        flags["snt_fng"] = fear_greed_extreme(
            fg, cfg.fear_greed_max, sustained_days=cfg.fear_greed_sustained_days * bpd,
        )
        sentiment_components.append(flags["snt_fng"])
    if social_sentiment is not None and not social_sentiment.empty:
        ss = social_sentiment.reindex(ohlcv.index).ffill()
        flags["snt_social_z"] = social_sentiment_z(ss) <= cfg.social_z_max
        sentiment_components.append(flags["snt_social_z"])
    sentiment_group = _combine_or(sentiment_components, default=false_template)

    # --- Macro veto -------------------------------------------------------
    macro_ok = pd.Series(True, index=ohlcv.index)
    if vix is not None and not vix.empty:
        v = vix.reindex(ohlcv.index).ffill()
        macro_ok &= v < cfg.vix_max
    if dxy is not None and not dxy.empty:
        d = dxy.reindex(ohlcv.index).ffill()
        # DXY 20d rate-of-change Z-score; veto if breaking out hard
        roc = d.pct_change(periods=bpd * 20)
        z = (roc - roc.rolling(bpd * 60).mean()) / roc.rolling(bpd * 60).std(ddof=0)
        macro_ok &= z.fillna(0) < cfg.dxy_z_max

    # --- Group aggregation ------------------------------------------------
    group_df = pd.DataFrame(
        {
            "price": price_group,
            "volatility": vol_group,
            "derivatives": deriv_group,
            "onchain": onchain_group,
            "sentiment": sentiment_group,
        }
    ).fillna(False).astype(bool)

    available_groups = {
        "price": True,
        "volatility": True,
        "derivatives": bool(deriv_components),
        "onchain": bool(onchain_components),
        "sentiment": bool(sentiment_components),
    }
    available = [name for name, is_available in available_groups.items() if is_available]
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


def compute_overheat_score(
    ohlcv: pd.DataFrame,
    funding: pd.Series | None = None,
    oi: pd.Series | None = None,
    mvrv_z: pd.Series | None = None,
    nupl: pd.Series | None = None,
    fear_greed: pd.Series | None = None,
    short_liq_usd: pd.Series | None = None,
    cfg: ThresholdConfig | None = None,
) -> ExtremeScore:
    """Mirror image of `compute_extreme_score`: how many independent
    groups say the market is *overheated*. Intended for trimming / not
    adding, not for shorting.

    Groups: price (RSI>80 | BB-Z≥+2.5 | ATR-displacement≥+3 | 30d run-up≥40%),
    derivatives (funding Z≥+2 | funding sustained ≥+0.05% | OI +15%/24h |
    short-liq spike), on-chain (MVRV-Z>7 | NUPL>0.75 | Pi Cycle Top),
    sentiment (F&G ≥80 for 3d). Volatility is deliberately *not* a group:
    tops form on low vol, so an RV spike is not diagnostic here.
    """

    cfg = cfg or ThresholdConfig()
    close, high, low = ohlcv["close"], ohlcv["high"], ohlcv["low"]
    bpd = bars_per_day(ohlcv.index)
    flags: dict[str, pd.Series] = {}
    false_template = pd.Series(False, index=ohlcv.index, dtype=bool)

    rsi14 = rsi(close, 14)
    bbz = bollinger_zscore(close, 20)
    disp = atr_displacement(close, high, low)
    runup = close / close.rolling(bpd * 30).min() - 1.0
    flags["hot_rsi"] = rsi14 > cfg.rsi_overbought
    flags["hot_bb"] = bbz >= cfg.bb_std
    flags["hot_atr_disp"] = disp >= cfg.atr_disp_extreme
    flags["hot_runup_30d"] = runup >= cfg.runup_30d_min
    price_group = flags["hot_rsi"] | flags["hot_bb"] | flags["hot_atr_disp"] | flags["hot_runup_30d"]

    deriv: list[pd.Series] = []
    if funding is not None and not funding.empty:
        f = funding.reindex(ohlcv.index).ffill()
        flags["hot_funding_z"] = funding_zscore(f).fillna(0) >= 2.0
        flags["hot_funding_pos"] = funding_sustained_positive(f, bars=3, threshold=cfg.funding_rate_8h_hot)
        deriv.append(flags["hot_funding_z"] | flags["hot_funding_pos"])
    if oi is not None and not oi.empty:
        o = oi.reindex(ohlcv.index).ffill()
        flags["hot_oi_surge"] = oi_surge_24h(o, bars_24h=bpd, threshold=cfg.oi_surge_24h_min)
        deriv.append(flags["hot_oi_surge"])
    if short_liq_usd is not None and not short_liq_usd.empty:
        sl = short_liq_usd.reindex(ohlcv.index).fillna(0.0)
        flags["hot_short_liq"] = liquidation_cascade(sl, None, z_min=cfg.long_liq_z, window=bpd * 30)
        deriv.append(flags["hot_short_liq"])
    deriv_group = _combine_or(deriv, default=false_template)

    onchain: list[pd.Series] = []
    if mvrv_z is not None and not mvrv_z.empty:
        m = mvrv_z.reindex(ohlcv.index).ffill()
        flags["hot_mvrv_z"] = mvrv_z_overheated(m, cfg.mvrv_z_hot)
        onchain.append(flags["hot_mvrv_z"])
    if nupl is not None and not nupl.empty:
        n = nupl.reindex(ohlcv.index).ffill()
        flags["hot_nupl"] = nupl_euphoria(n, cfg.nupl_hot)
        onchain.append(flags["hot_nupl"])
    daily_close = close.resample("1D").last().dropna()
    if len(daily_close) > 350:
        pt = pi_cycle_top(daily_close)
        pt_al = pt.reindex(ohlcv.index, method="ffill").fillna(False).astype(bool)
        flags["hot_pi_top"] = pt_al.rolling(bpd * 7, min_periods=1).max().astype(bool)
        onchain.append(flags["hot_pi_top"])
    onchain_group = _combine_or(onchain, default=false_template)

    sent: list[pd.Series] = []
    if fear_greed is not None and not fear_greed.empty:
        fg = fear_greed.reindex(ohlcv.index).ffill()
        flags["hot_fng"] = fear_greed_greed_extreme(
            fg, cfg.fear_greed_hot, sustained_days=cfg.fear_greed_sustained_days * bpd,
        )
        sent.append(flags["hot_fng"])
    sent_group = _combine_or(sent, default=false_template)

    group_df = pd.DataFrame({
        "price": price_group, "derivatives": deriv_group,
        "onchain": onchain_group, "sentiment": sent_group,
    }).fillna(False).astype(bool)
    available = ["price"] + [g for g, ok in (
        ("derivatives", bool(deriv)), ("onchain", bool(onchain)), ("sentiment", bool(sent)),
    ) if ok]
    score = (group_df[available].sum(axis=1) / max(len(available), 1)).rename("overheat_score")
    macro_ok = pd.Series(True, index=ohlcv.index)
    factor_df = pd.DataFrame(flags).fillna(False).astype(bool)
    reasons = {ts: [k for k, v in flags.items() if bool(v.get(ts, False))]
               for ts in score[score >= 0.5].index}
    return ExtremeScore(score=score, factor_flags=factor_df, group_flags=group_df,
                        macro_ok=macro_ok, reasons=reasons)
