"""Source-faithful learning documentation for the preserved 50-signal catalog.

This is a human-readable contract for ``qt.workbench.catalog_signals``.  It is
deliberately declarative: it does not infer formulas from labels or inspect an
AST at request time.  A catalog ID without a reviewed entry is rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

from qt.workbench.catalog import legacy_catalog

JsonDict: TypeAlias = dict[str, object]

_SOURCE_ID = "qt.workbench.catalog_signals.evaluate_signal"
_MARKET_AVAILABILITY = {
    "available_at": "derived after the completed market bar",
    "availability_basis": "completed_market_bar",
    "requires_provider_available_at": False,
    "may_be_revised": False,
}


def _detail(
    formula_en: str,
    formula_zh: str,
    condition_en: str,
    condition_zh: str,
    *,
    effective: tuple[str, ...],
    ignored: tuple[str, ...] = (),
    cross: str | None = None,
    warmup: str = "Rolling/EWM inputs use min_periods=1; early values are not a full-window confirmation.",
    nan: str = "Any NaN reaching the boolean evaluator becomes False.",
    caveats: tuple[str, ...] = (),
) -> JsonDict:
    return {
        "formula_en": formula_en,
        "formula_zh": formula_zh,
        "condition_en": condition_en,
        "condition_zh": condition_zh,
        "cross_semantics": cross,
        "effective_parameters": list(effective),
        "ignored_legacy_parameters": list(ignored),
        "warmup": warmup,
        "nan_behavior": nan,
        "caveats": list(caveats),
    }


_CROSS_UP = "cross-up means current left > right and previous left <= previous right"
_CROSS_DOWN = "cross-down means current left < right and previous left >= previous right"
_PRECOMPUTED_14 = "The catalog indicator builder fixes this base indicator at 14 bars; the declared period does not alter it."

# Each expression below is transcribed from evaluate_signal/add_catalog_indicators.
_SIGNALS: dict[str, JsonDict] = {
    "close_above_ema": _detail("close > EMA(close, period)", "收盘价 > EMA(收盘价, period)", "above EMA", "高于 EMA", effective=("period",)),
    "close_below_ema": _detail("close < EMA(close, period)", "收盘价 < EMA(收盘价, period)", "below EMA", "低于 EMA", effective=("period",)),
    "ema_fast_above_slow": _detail("EMA(close, fast_period) > EMA(close, slow_period)", "EMA(收盘价, fast_period) > EMA(收盘价, slow_period)", "fast EMA above slow EMA", "快 EMA 高于慢 EMA", effective=("fast_period", "slow_period")),
    "ema_fast_below_slow": _detail("EMA(close, fast_period) < EMA(close, slow_period)", "EMA(收盘价, fast_period) < EMA(收盘价, slow_period)", "fast EMA below slow EMA", "快 EMA 低于慢 EMA", effective=("fast_period", "slow_period")),
    "ema_cross_up": _detail("cross_up(EMA(close, fast_period), EMA(close, slow_period))", "EMA(收盘价, fast_period) 向上穿越 EMA(收盘价, slow_period)", "fast EMA crosses above slow EMA", "快 EMA 向上穿越慢 EMA", effective=("fast_period", "slow_period"), cross=_CROSS_UP),
    "ema_cross_down": _detail("cross_down(EMA(close, fast_period), EMA(close, slow_period))", "EMA(收盘价, fast_period) 向下穿越 EMA(收盘价, slow_period)", "fast EMA crosses below slow EMA", "快 EMA 向下穿越慢 EMA", effective=("fast_period", "slow_period"), cross=_CROSS_DOWN),
    "adx_trending": _detail("precomputed ADX(14) >= threshold", "预计算 ADX(14) >= threshold", "ADX reaches threshold", "ADX 达到阈值", effective=("threshold",), ignored=("period",), caveats=(_PRECOMPUTED_14,)),
    "adx_weak": _detail("precomputed ADX(14) < threshold", "预计算 ADX(14) < threshold", "ADX below threshold", "ADX 低于阈值", effective=("threshold",), ignored=("period",), caveats=(_PRECOMPUTED_14,)),
    "supertrend_bullish": _detail("close > ((high + low) / 2 - ATR(14) * 2)", "收盘价 > ((最高价 + 最低价) / 2 - ATR(14) * 2)", "close above simplified line", "收盘价高于简化线", effective=(), ignored=("period", "atr_multiple"), caveats=("This is a simplified ATR offset, not standard Supertrend state/band logic.", _PRECOMPUTED_14)),
    "supertrend_bearish": _detail("close < ((high + low) / 2 - ATR(14) * 2)", "收盘价 < ((最高价 + 最低价) / 2 - ATR(14) * 2)", "close below simplified line", "收盘价低于简化线", effective=(), ignored=("period", "atr_multiple"), caveats=("This is a simplified ATR offset, not standard Supertrend state/band logic.", _PRECOMPUTED_14)),
    "rsi_oversold": _detail("legacy RSI(14) <= lower", "旧版 RSI(14) <= lower", "RSI at/below lower", "RSI 低于或等于下限", effective=("lower",), ignored=("period",), caveats=("Legacy RSI uses EWM(alpha=1/14, adjust=False) and fills undefined values with 50; it is not a TA-Lib RSI warm-up/output contract.",)),
    "rsi_overbought": _detail("legacy RSI(14) >= upper", "旧版 RSI(14) >= upper", "RSI at/above upper", "RSI 高于或等于上限", effective=("upper",), ignored=("period",), caveats=("Legacy RSI uses EWM(alpha=1/14, adjust=False) and fills undefined values with 50; it is not a TA-Lib RSI warm-up/output contract.",)),
    "rsi_cross_up": _detail("cross_up(legacy RSI(14), constant(lower))", "旧版 RSI(14) 向上穿越 lower", "RSI crosses upward through lower", "RSI 向上穿越下限", effective=("lower",), ignored=("period",), cross=_CROSS_UP, caveats=("Legacy RSI is EWM-based and filled with 50, not TA-Lib's established warm-up contract.",)),
    "rsi_cross_down": _detail("cross_down(legacy RSI(14), constant(upper))", "旧版 RSI(14) 向下穿越 upper", "RSI 向下穿越 upper", "RSI 向下穿越上限", effective=("upper",), ignored=("period",), cross=_CROSS_DOWN, caveats=("Legacy RSI is EWM-based and filled with 50, not TA-Lib's established warm-up contract.",)),
    "bollinger_lower_reentry": _detail("cross_up(close, SMA(close, lookback) - stddev * rolling_std(close, lookback))", "收盘价向上穿越 SMA(lookback) - stddev * 滚动标准差", "close re-enters above lower band", "收盘价向上回到下轨之上", effective=("lookback", "stddev"), cross=_CROSS_UP),
    "bollinger_upper_reentry": _detail("cross_down(close, SMA(close, lookback) + stddev * rolling_std(close, lookback))", "收盘价向下穿越 SMA(lookback) + stddev * 滚动标准差", "close re-enters below upper band", "收盘价向下回到上轨之下", effective=("lookback", "stddev"), cross=_CROSS_DOWN),
    "zscore_low": _detail("zscore(close, lookback) <= threshold", "zscore(收盘价, lookback) <= threshold", "z-score at/below threshold", "Z 分数低于或等于阈值", effective=("lookback", "threshold"), nan="Zero standard deviation is converted to z-score 0; final boolean NaN is False."),
    "zscore_high": _detail("zscore(close, lookback) >= threshold", "zscore(收盘价, lookback) >= threshold", "z-score at/above threshold", "Z 分数高于或等于阈值", effective=("lookback", "threshold"), nan="Zero standard deviation is converted to z-score 0; final boolean NaN is False."),
    "stoch_oversold_cross": _detail("cross_up(precomputed StochK(14), precomputed StochD(3)) and StochK <= lower", "预计算 StochK(14) 向上穿越 StochD(3), 且 StochK <= lower", "up-cross while K is oversold", "K 向上穿越 D 且 K 处于超卖", effective=("lower",), ignored=("period",), cross=_CROSS_UP, caveats=("StochK/StochD are fixed at 14/3 by the catalog builder; period is ignored.",)),
    "stoch_overbought_cross": _detail("cross_down(precomputed StochK(14), precomputed StochD(3)) and StochK >= upper", "预计算 StochK(14) 向下穿越 StochD(3), 且 StochK >= upper", "down-cross while K is overbought", "K 向下穿越 D 且 K 处于超买", effective=("upper",), ignored=("period",), cross=_CROSS_DOWN, caveats=("StochK/StochD are fixed at 14/3 by the catalog builder; period is ignored.",)),
    "macd_bullish": _detail("precomputed EMA(12)-EMA(26) > precomputed EMA(9) signal", "预计算 EMA(12)-EMA(26) > 预计算 EMA(9) 信号线", "MACD above signal", "MACD 高于信号线", effective=(), ignored=("fast_period", "slow_period", "signal_period"), caveats=("All MACD periods are fixed at 12/26/9 by the catalog builder.",)),
    "macd_bearish": _detail("precomputed EMA(12)-EMA(26) < precomputed EMA(9) signal", "预计算 EMA(12)-EMA(26) < 预计算 EMA(9) 信号线", "MACD below signal", "MACD 低于信号线", effective=(), ignored=("fast_period", "slow_period", "signal_period"), caveats=("All MACD periods are fixed at 12/26/9 by the catalog builder.",)),
    "macd_cross_up": _detail("cross_up(precomputed MACD(12,26), precomputed signal(9))", "预计算 MACD(12,26) 向上穿越信号线(9)", "MACD crosses upward", "MACD 向上穿越信号线", effective=(), ignored=("fast_period", "slow_period", "signal_period"), cross=_CROSS_UP, caveats=("All MACD periods are fixed at 12/26/9 by the catalog builder.",)),
    "macd_cross_down": _detail("cross_down(precomputed MACD(12,26), precomputed signal(9))", "预计算 MACD(12,26) 向下穿越信号线(9)", "MACD crosses downward", "MACD 向下穿越信号线", effective=(), ignored=("fast_period", "slow_period", "signal_period"), cross=_CROSS_DOWN, caveats=("All MACD periods are fixed at 12/26/9 by the catalog builder.",)),
    "roc_positive": _detail("precomputed close.pct_change(12) > threshold", "预计算 close.pct_change(12) > threshold", "ROC above threshold", "ROC 高于阈值", effective=("threshold",), ignored=("period",), caveats=("ROC is fixed at 12 bars by the catalog builder.",)),
    "roc_negative": _detail("precomputed close.pct_change(12) < threshold", "预计算 close.pct_change(12) < threshold", "ROC below threshold", "ROC 低于阈值", effective=("threshold",), ignored=("period",), caveats=("ROC is fixed at 12 bars by the catalog builder.",)),
    "cci_oversold_recovery": _detail("cross_up(precomputed CCI(20), constant(lower))", "预计算 CCI(20) 向上穿越 lower", "CCI crosses upward through lower", "CCI 向上穿越下限", effective=("lower",), ignored=("period",), cross=_CROSS_UP, caveats=("CCI is fixed at 20 bars by the catalog builder.",)),
    "cci_overbought_reversal": _detail("cross_down(precomputed CCI(20), constant(upper))", "预计算 CCI(20) 向下穿越 upper", "CCI crosses downward through upper", "CCI 向下穿越上限", effective=("upper",), ignored=("period",), cross=_CROSS_DOWN, caveats=("CCI is fixed at 20 bars by the catalog builder.",)),
    "mfi_oversold": _detail("precomputed MFI(14) <= lower", "预计算 MFI(14) <= lower", "MFI at/below lower", "MFI 低于或等于下限", effective=("lower",), ignored=("period",), caveats=(_PRECOMPUTED_14,)),
    "mfi_overbought": _detail("precomputed MFI(14) >= upper", "预计算 MFI(14) >= upper", "MFI 高于或等于上限", "MFI 高于或等于上限", effective=("upper",), ignored=("period",), caveats=(_PRECOMPUTED_14,)),
    "atr_pct_above": _detail("precomputed ATR(14) / close >= min_value", "预计算 ATR(14) / 收盘价 >= min_value", "ATR percentage at/above minimum", "ATR 百分比高于或等于下限", effective=("min_value",), ignored=("period",), caveats=(_PRECOMPUTED_14,)),
    "atr_pct_below": _detail("precomputed ATR(14) / close <= max_value", "预计算 ATR(14) / 收盘价 <= max_value", "ATR percentage at/below maximum", "ATR 百分比低于或等于上限", effective=("max_value",), ignored=("period",), caveats=(_PRECOMPUTED_14,)),
    "bb_width_expanding": _detail("rolling_mean(precomputed BB width, lookback) > previous rolling_mean", "滚动均值(预计算布林宽度, lookback) > 前一根滚动均值", "smoothed width increases", "平滑后的布林宽度上升", effective=("lookback",), caveats=("Base Bollinger width is precomputed with 20 bars; lookback only smooths that series.",)),
    "bb_width_contracting": _detail("rolling_mean(precomputed BB width, lookback) < previous rolling_mean", "滚动均值(预计算布林宽度, lookback) < 前一根滚动均值", "smoothed width decreases", "平滑后的布林宽度下降", effective=("lookback",), caveats=("Base Bollinger width is precomputed with 20 bars; lookback only smooths that series.",)),
    "keltner_breakout_up": _detail("close > (EMA(typical, period) + ATR(14) * atr_multiple).shift(1)", "收盘价 > (EMA(典型价, period) + ATR(14) * atr_multiple).shift(1)", "close exceeds prior upper Keltner band", "收盘价突破前一根肯特纳上轨", effective=("period", "atr_multiple"), caveats=(_PRECOMPUTED_14,)),
    "keltner_breakout_down": _detail("close < (EMA(typical, period) - ATR(14) * atr_multiple).shift(1)", "收盘价 < (EMA(典型价, period) - ATR(14) * atr_multiple).shift(1)", "close breaks prior lower Keltner band", "收盘价跌破前一根肯特纳下轨", effective=("period", "atr_multiple"), caveats=(_PRECOMPUTED_14,)),
    "donchian_breakout_up": _detail("close > rolling_max(high, lookback).shift(1)", "收盘价 > rolling_max(最高价, lookback).shift(1)", "close exceeds prior Donchian high", "收盘价突破前一根唐奇安高点", effective=("lookback",)),
    "donchian_breakout_down": _detail("close < rolling_min(low, lookback).shift(1)", "收盘价 < rolling_min(最低价, lookback).shift(1)", "close breaks prior Donchian low", "收盘价跌破前一根唐奇安低点", effective=("lookback",)),
    "squeeze_release_up": _detail("BB_width.shift(1) < rolling_mean(BB_width, lookback).shift(1) and close > precomputed Keltner upper.shift(1)", "前一根布林宽度低于其滚动均值, 且收盘价 > 前一根预计算肯特纳上轨", "prior squeeze and upward release", "上一根挤压且向上释放", effective=("lookback",), caveats=("Keltner comparison uses the precomputed 20-period, ATR(14)*2 band; no atr_multiple parameter is applied here.",)),
    "squeeze_release_down": _detail("BB_width.shift(1) < rolling_mean(BB_width, lookback).shift(1) and close < precomputed Keltner lower.shift(1)", "前一根布林宽度低于其滚动均值, 且收盘价 < 前一根预计算肯特纳下轨", "prior squeeze and downward release", "上一根挤压且向下释放", effective=("lookback",), caveats=("Keltner comparison uses the precomputed 20-period, ATR(14)*2 band; no atr_multiple parameter is applied here.",)),
    "volume_spike": _detail("volume / rolling_mean(volume, 20) >= multiple", "成交量 / rolling_mean(成交量, 20) >= multiple", "volume ratio reaches multiple", "成交量比率达到倍数", effective=("multiple",), ignored=("lookback",), caveats=("Volume moving average is fixed at 20 bars by the catalog builder.",)),
    "volume_dry_up": _detail("volume / rolling_mean(volume, 20) <= 1 / max(multiple, 0.1)", "成交量 / rolling_mean(成交量, 20) <= 1 / max(multiple, 0.1)", "volume ratio is sufficiently low", "成交量比率足够低", effective=("multiple",), ignored=("lookback",), caveats=("Volume moving average is fixed at 20 bars by the catalog builder.",)),
    "obv_rising": _detail("rolling_mean(precomputed OBV, lookback) > previous rolling_mean", "滚动均值(预计算 OBV, lookback) > 前一根滚动均值", "smoothed OBV increases", "平滑后的 OBV 上升", effective=("lookback",)),
    "obv_falling": _detail("rolling_mean(precomputed OBV, lookback) < previous rolling_mean", "滚动均值(预计算 OBV, lookback) < 前一根滚动均值", "smoothed OBV decreases", "平滑后的 OBV 下降", effective=("lookback",)),
    "cmf_positive": _detail("precomputed CMF(20) > threshold", "预计算 CMF(20) > threshold", "CMF above threshold", "CMF 高于阈值", effective=("threshold",), ignored=("period",), caveats=("CMF is fixed at 20 bars by the catalog builder.",)),
    "cmf_negative": _detail("precomputed CMF(20) < threshold", "预计算 CMF(20) < threshold", "CMF below threshold", "CMF 低于阈值", effective=("threshold",), ignored=("period",), caveats=("CMF is fixed at 20 bars by the catalog builder.",)),
    "vwap_above": _detail("close > rolling_sum(typical*volume, period) / rolling_sum(volume, period)", "收盘价 > rolling_sum(典型价*成交量, period) / rolling_sum(成交量, period)", "close above rolling VWAP", "收盘价高于滚动 VWAP", effective=("period",)),
    "vwap_below": _detail("close < rolling_sum(typical*volume, period) / rolling_sum(volume, period)", "收盘价 < rolling_sum(典型价*成交量, period) / rolling_sum(成交量, period)", "close below rolling VWAP", "收盘价低于滚动 VWAP", effective=("period",)),
    "drawdown_fear": _detail("clip((rolling_max(close, lookback).shift(1) - close) / prior_high, lower=0) >= min_value", "clip((前一根滚动最高收盘价 - 收盘价) / 前高, 下限=0) >= min_value", "drawdown reaches fear threshold", "回撤达到恐慌阈值", effective=("lookback", "min_value")),
    "drawdown_recovery": _detail("clip((rolling_max(close, lookback).shift(1) - close) / prior_high, lower=0) <= max_value", "clip((前一根滚动最高收盘价 - 收盘价) / 前高, 下限=0) <= max_value", "drawdown is at/below recovery threshold", "回撤低于或等于恢复阈值", effective=("lookback", "max_value")),
}


def catalog_signal_documentation(signal_id: str) -> JsonDict:
    """Return documentation anchored to an actual preserved evaluator ID."""

    for signal in _catalog_signals():
        if signal["id"] == signal_id:
            detail = _SIGNALS.get(signal_id)
            if detail is None:
                raise RuntimeError(f"learning documentation missing for catalog signal {signal_id}")
            return _document(signal, detail)
    raise KeyError(signal_id)


def list_catalog_signal_documentation() -> list[JsonDict]:
    signals = _catalog_signals()
    missing = {str(signal["id"]) for signal in signals}.difference(_SIGNALS)
    extra = set(_SIGNALS).difference(str(signal["id"]) for signal in signals)
    if missing or extra:
        raise RuntimeError(f"learning catalog drift; missing={sorted(missing)}, extra={sorted(extra)}")
    return [_document(signal, _SIGNALS[str(signal["id"])]) for signal in signals]


def list_indicator_family_documentation() -> list[JsonDict]:
    """Provide user-facing family guidance without pretending provider data is candle-close data."""

    families = {
        "price": ("OHLCV price transforms", "OHLCV 价格变换", "completed_market_bar"),
        "talib_standard": ("TA-Lib-compatible wrappers where installed", "安装时使用 TA-Lib 兼容封装", "completed_market_bar"),
        "volatility": ("rolling realised-volatility transforms", "滚动已实现波动率变换", "completed_market_bar"),
        "regime": ("rolling price-regime classifiers", "滚动价格状态分类器", "completed_market_bar"),
        "events": ("event detectors evaluated on completed market bars", "在完整市场 K 线后评估的事件检测器", "completed_market_bar"),
        "onchain": ("provider-fed on-chain measurements", "供应商提供的链上测量", "provider_recorded_available_at"),
        "options": ("provider-fed options measurements", "供应商提供的期权测量", "provider_recorded_available_at"),
        "derivatives": ("provider-fed derivatives measurements", "供应商提供的衍生品测量", "provider_recorded_available_at"),
        "sentiment": ("provider-fed sentiment measurements", "供应商提供的情绪测量", "provider_recorded_available_at"),
        "smartmoney": ("provider-fed flow and smart-money measurements", "供应商提供的资金流与聪明钱测量", "provider_recorded_available_at"),
    }
    documents: list[JsonDict] = []
    for family, (en, zh, basis) in families.items():
        provider = basis == "provider_recorded_available_at"
        documents.append(
            {
                "id": f"family:{family}",
                "kind": "indicator_family",
                "name_en": en,
                "name_zh": zh,
                "formula_en": "See each concrete indicator; no formula is inferred from the family name.",
                "formula_zh": "请查看具体指标; 不会从家族名称推断公式。",
                "warmup": "Indicator-specific; inspect the concrete documentation.",
                "nan_behavior": "Indicator-specific; missing input must not be interpreted as a true signal.",
                "source_identity": f"qt.indicators.{family}",
                "availability_basis": basis,
                "available_at": (
                    "provider-recorded available_at required; may be revised"
                    if provider
                    else "derived after the completed market bar"
                ),
                "requires_provider_available_at": provider,
                "may_be_revised": provider,
            }
        )
    return documents


def _catalog_signals() -> list[Mapping[str, object]]:
    signals = legacy_catalog()["signals"]
    if not isinstance(signals, list) or not all(isinstance(signal, Mapping) for signal in signals):
        raise RuntimeError("catalog signals are unavailable")
    return signals


def _document(signal: Mapping[str, object], detail: Mapping[str, object]) -> JsonDict:
    catalog = legacy_catalog()
    signal_id = str(signal["id"])
    return {
        "id": signal_id,
        "kind": "catalog_signal",
        "name_en": signal_id.replace("_", " "),
        "name_zh": f"目录信号: {signal_id}",
        "category": signal.get("category"),
        "direction": signal.get("direction"),
        "evaluator": signal.get("evaluator"),
        "parameters": signal.get("parameters", []),
        "required_indicators": signal.get("required_indicators", []),
        **dict(detail),
        **_MARKET_AVAILABILITY,
        "source_identity": _SOURCE_ID,
        "source_version": {
            "catalog_schema_version": catalog.get("schema_version"),
            "catalog_sha256": catalog.get("sha256"),
            "migration_revision": "btc-quant:e0b47ca",
        },
    }
