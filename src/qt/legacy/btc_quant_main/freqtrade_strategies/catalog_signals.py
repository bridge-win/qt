# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from pandas import DataFrame, Series


def add_catalog_indicators(dataframe: DataFrame) -> DataFrame:
    frame = dataframe.copy()
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)

    frame["ema_20"] = close.ewm(span=20, adjust=False).mean()
    frame["ema_50"] = close.ewm(span=50, adjust=False).mean()
    frame["ema_200"] = close.ewm(span=200, adjust=False).mean()
    frame["atr"] = _atr(frame, 14)
    frame["atr_pct"] = frame["atr"] / close.replace(0, np.nan)
    frame["adx"] = _adx(frame, 14)
    frame["supertrend_line"] = ((high + low) / 2) - (frame["atr"] * 2.0)
    frame["rsi"] = _rsi(close, 14)

    bb_mid = close.rolling(20, min_periods=1).mean()
    bb_std = close.rolling(20, min_periods=1).std(ddof=0).fillna(0)
    frame["bb_mid"] = bb_mid
    frame["bb_upper"] = bb_mid + bb_std * 2
    frame["bb_lower"] = bb_mid - bb_std * 2
    frame["bb_width"] = (frame["bb_upper"] - frame["bb_lower"]) / bb_mid.replace(0, np.nan)
    frame["zscore"] = (close - bb_mid) / bb_std.replace(0, np.nan)

    lowest = low.rolling(14, min_periods=1).min()
    highest = high.rolling(14, min_periods=1).max()
    frame["stoch_k"] = ((close - lowest) / (highest - lowest).replace(0, np.nan) * 100).fillna(50)
    frame["stoch_d"] = frame["stoch_k"].rolling(3, min_periods=1).mean()

    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    frame["macd"] = ema_fast - ema_slow
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    frame["roc"] = close.pct_change(12).fillna(0)
    frame["cci"] = _cci(frame, 20)
    frame["mfi"] = _mfi(frame, 14)

    typical = (high + low + close) / 3
    frame["keltner_mid"] = typical.ewm(span=20, adjust=False).mean()
    frame["keltner_upper"] = frame["keltner_mid"] + frame["atr"] * 2
    frame["keltner_lower"] = frame["keltner_mid"] - frame["atr"] * 2
    frame["donchian_upper"] = high.rolling(20, min_periods=1).max().shift(1)
    frame["donchian_lower"] = low.rolling(20, min_periods=1).min().shift(1)
    frame["volume_ma"] = volume.rolling(20, min_periods=1).mean()
    frame["volume_ratio"] = volume / frame["volume_ma"].replace(0, np.nan)
    direction = np.sign(close.diff().fillna(0))
    frame["obv"] = (direction * volume).cumsum()
    frame["cmf"] = _cmf(frame, 20)
    frame["vwap"] = (typical * volume).rolling(20, min_periods=1).sum() / volume.rolling(20, min_periods=1).sum()
    frame["fear_drawdown"] = (close.rolling(90, min_periods=1).max().shift(1) - close) / close.rolling(
        90, min_periods=1
    ).max().shift(1)

    return frame.replace([np.inf, -np.inf], np.nan)


def evaluate_signal(signal_id: str, dataframe: DataFrame, params: Mapping[str, object]) -> Series:
    evaluators = {
        "close_above_ema": lambda: dataframe["close"] > _ema(dataframe, params, default=20),
        "close_below_ema": lambda: dataframe["close"] < _ema(dataframe, params, default=20),
        "ema_fast_above_slow": lambda: _ema(dataframe, params, key="fast_period", default=12) > _ema(dataframe, params, key="slow_period", default=26),
        "ema_fast_below_slow": lambda: _ema(dataframe, params, key="fast_period", default=12) < _ema(dataframe, params, key="slow_period", default=26),
        "ema_cross_up": lambda: _cross_up(_ema(dataframe, params, key="fast_period", default=12), _ema(dataframe, params, key="slow_period", default=26)),
        "ema_cross_down": lambda: _cross_down(_ema(dataframe, params, key="fast_period", default=12), _ema(dataframe, params, key="slow_period", default=26)),
        "adx_trending": lambda: dataframe["adx"] >= _number(params, "threshold", 20),
        "adx_weak": lambda: dataframe["adx"] < _number(params, "threshold", 20),
        "supertrend_bullish": lambda: dataframe["close"] > dataframe["supertrend_line"],
        "supertrend_bearish": lambda: dataframe["close"] < dataframe["supertrend_line"],
        "rsi_oversold": lambda: dataframe["rsi"] <= _number(params, "lower", 30),
        "rsi_overbought": lambda: dataframe["rsi"] >= _number(params, "upper", 70),
        "rsi_cross_up": lambda: _cross_up(dataframe["rsi"], _constant(dataframe, _number(params, "lower", 30))),
        "rsi_cross_down": lambda: _cross_down(dataframe["rsi"], _constant(dataframe, _number(params, "upper", 70))),
        "bollinger_lower_reentry": lambda: _cross_up(dataframe["close"], _bollinger(dataframe, params, "lower")),
        "bollinger_upper_reentry": lambda: _cross_down(dataframe["close"], _bollinger(dataframe, params, "upper")),
        "zscore_low": lambda: _zscore(dataframe, params) <= _number(params, "threshold", -1.5),
        "zscore_high": lambda: _zscore(dataframe, params) >= _number(params, "threshold", 1.5),
        "stoch_oversold_cross": lambda: _cross_up(dataframe["stoch_k"], dataframe["stoch_d"]) & (dataframe["stoch_k"] <= _number(params, "lower", 30)),
        "stoch_overbought_cross": lambda: _cross_down(dataframe["stoch_k"], dataframe["stoch_d"]) & (dataframe["stoch_k"] >= _number(params, "upper", 70)),
        "macd_bullish": lambda: dataframe["macd"] > dataframe["macd_signal"],
        "macd_bearish": lambda: dataframe["macd"] < dataframe["macd_signal"],
        "macd_cross_up": lambda: _cross_up(dataframe["macd"], dataframe["macd_signal"]),
        "macd_cross_down": lambda: _cross_down(dataframe["macd"], dataframe["macd_signal"]),
        "roc_positive": lambda: dataframe["roc"] > _number(params, "threshold", 0),
        "roc_negative": lambda: dataframe["roc"] < _number(params, "threshold", 0),
        "cci_oversold_recovery": lambda: _cross_up(dataframe["cci"], _constant(dataframe, _number(params, "lower", -100))),
        "cci_overbought_reversal": lambda: _cross_down(dataframe["cci"], _constant(dataframe, _number(params, "upper", 100))),
        "mfi_oversold": lambda: dataframe["mfi"] <= _number(params, "lower", 30),
        "mfi_overbought": lambda: dataframe["mfi"] >= _number(params, "upper", 70),
        "atr_pct_above": lambda: dataframe["atr_pct"] >= _number(params, "min_value", 0.01),
        "atr_pct_below": lambda: dataframe["atr_pct"] <= _number(params, "max_value", 0.12),
        "bb_width_expanding": lambda: _rolling_value(dataframe["bb_width"], params) > _rolling_value(dataframe["bb_width"], params).shift(1),
        "bb_width_contracting": lambda: _rolling_value(dataframe["bb_width"], params) < _rolling_value(dataframe["bb_width"], params).shift(1),
        "keltner_breakout_up": lambda: dataframe["close"] > _keltner(dataframe, params, "upper").shift(1),
        "keltner_breakout_down": lambda: dataframe["close"] < _keltner(dataframe, params, "lower").shift(1),
        "donchian_breakout_up": lambda: dataframe["close"] > dataframe["high"].rolling(_period(params, "lookback", 20), min_periods=1).max().shift(1),
        "donchian_breakout_down": lambda: dataframe["close"] < dataframe["low"].rolling(_period(params, "lookback", 20), min_periods=1).min().shift(1),
        "squeeze_release_up": lambda: _squeeze(dataframe, params) & (dataframe["close"] > dataframe["keltner_upper"].shift(1)),
        "squeeze_release_down": lambda: _squeeze(dataframe, params) & (dataframe["close"] < dataframe["keltner_lower"].shift(1)),
        "volume_spike": lambda: dataframe["volume_ratio"] >= _number(params, "multiple", 1.5),
        "volume_dry_up": lambda: dataframe["volume_ratio"] <= (1 / max(_number(params, "multiple", 1.5), 0.1)),
        "obv_rising": lambda: _rolling_value(dataframe["obv"], params) > _rolling_value(dataframe["obv"], params).shift(1),
        "obv_falling": lambda: _rolling_value(dataframe["obv"], params) < _rolling_value(dataframe["obv"], params).shift(1),
        "cmf_positive": lambda: dataframe["cmf"] > _number(params, "threshold", 0),
        "cmf_negative": lambda: dataframe["cmf"] < _number(params, "threshold", 0),
        "vwap_above": lambda: dataframe["close"] > _vwap(dataframe, params),
        "vwap_below": lambda: dataframe["close"] < _vwap(dataframe, params),
        "drawdown_fear": lambda: _fear_drawdown(dataframe, params) >= _number(params, "min_value", 0.08),
        "drawdown_recovery": lambda: _fear_drawdown(dataframe, params) <= _number(params, "max_value", 0.04),
    }
    evaluator = evaluators.get(signal_id)
    if evaluator is None:
        raise ValueError(f"unknown catalog signal: {signal_id}")
    return evaluator().fillna(False).astype(bool)


def _period(params: Mapping[str, object], key: str, default: int) -> int:
    return max(1, int(float(params.get(key, default))))


def _number(params: Mapping[str, object], key: str, default: float) -> float:
    return float(params.get(key, default))


def _constant(dataframe: DataFrame, value: float) -> Series:
    return pd.Series(value, index=dataframe.index)


def _cross_up(left: Series, right: Series) -> Series:
    return (left > right) & (left.shift(1) <= right.shift(1))


def _cross_down(left: Series, right: Series) -> Series:
    return (left < right) & (left.shift(1) >= right.shift(1))


def _ema(dataframe: DataFrame, params: Mapping[str, object], *, key: str = "period", default: int) -> Series:
    return dataframe["close"].ewm(span=_period(params, key, default), adjust=False).mean()


def _bollinger(dataframe: DataFrame, params: Mapping[str, object], band: str) -> Series:
    period = _period(params, "lookback", 20)
    stddev = _number(params, "stddev", 2)
    close = dataframe["close"]
    mid = close.rolling(period, min_periods=1).mean()
    std = close.rolling(period, min_periods=1).std(ddof=0).fillna(0)
    return mid + std * stddev if band == "upper" else mid - std * stddev


def _zscore(dataframe: DataFrame, params: Mapping[str, object]) -> Series:
    period = _period(params, "lookback", 20)
    close = dataframe["close"]
    mid = close.rolling(period, min_periods=1).mean()
    std = close.rolling(period, min_periods=1).std(ddof=0)
    return ((close - mid) / std.replace(0, np.nan)).fillna(0)


def _rolling_value(series: Series, params: Mapping[str, object]) -> Series:
    return series.rolling(_period(params, "lookback", 20), min_periods=1).mean()


def _keltner(dataframe: DataFrame, params: Mapping[str, object], band: str) -> Series:
    period = _period(params, "period", 20)
    multiple = _number(params, "atr_multiple", 2)
    typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
    mid = typical.ewm(span=period, adjust=False).mean()
    return mid + dataframe["atr"] * multiple if band == "upper" else mid - dataframe["atr"] * multiple


def _squeeze(dataframe: DataFrame, params: Mapping[str, object]) -> Series:
    width = dataframe["bb_width"]
    rolling_width = width.rolling(_period(params, "lookback", 20), min_periods=1).mean()
    return width.shift(1) < rolling_width.shift(1)


def _vwap(dataframe: DataFrame, params: Mapping[str, object]) -> Series:
    period = _period(params, "period", 20)
    typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
    volume = dataframe["volume"]
    return (typical * volume).rolling(period, min_periods=1).sum() / volume.rolling(period, min_periods=1).sum()


def _fear_drawdown(dataframe: DataFrame, params: Mapping[str, object]) -> Series:
    lookback = _period(params, "lookback", 90)
    prior_high = dataframe["close"].rolling(lookback, min_periods=1).max().shift(1)
    return ((prior_high - dataframe["close"]) / prior_high.replace(0, np.nan)).clip(lower=0).fillna(0)


def _atr(dataframe: DataFrame, period: int) -> Series:
    high = dataframe["high"]
    low = dataframe["low"]
    close = dataframe["close"]
    true_range = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=1).mean()


def _adx(dataframe: DataFrame, period: int) -> Series:
    high = dataframe["high"]
    low = dataframe["low"]
    close = dataframe["close"]
    plus_dm = (high.diff()).clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    atr = _atr(dataframe, period).replace(0, np.nan)
    plus_di = 100 * plus_dm.rolling(period, min_periods=1).mean() / atr
    minus_di = 100 * minus_dm.rolling(period, min_periods=1).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return dx.rolling(period, min_periods=1).mean().fillna(0)


def _rsi(close: Series, period: int) -> Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _cci(dataframe: DataFrame, period: int) -> Series:
    typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
    mean = typical.rolling(period, min_periods=1).mean()
    mean_deviation = (typical - mean).abs().rolling(period, min_periods=1).mean()
    return ((typical - mean) / (0.015 * mean_deviation.replace(0, np.nan))).fillna(0)


def _mfi(dataframe: DataFrame, period: int) -> Series:
    typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
    money_flow = typical * dataframe["volume"]
    positive = money_flow.where(typical.diff() > 0, 0.0).rolling(period, min_periods=1).sum()
    negative = money_flow.where(typical.diff() < 0, 0.0).abs().rolling(period, min_periods=1).sum()
    ratio = positive / negative.replace(0, np.nan)
    return (100 - (100 / (1 + ratio))).fillna(50)


def _cmf(dataframe: DataFrame, period: int) -> Series:
    high = dataframe["high"]
    low = dataframe["low"]
    close = dataframe["close"]
    volume = dataframe["volume"]
    multiplier = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    money_volume = multiplier.fillna(0) * volume
    return money_volume.rolling(period, min_periods=1).sum() / volume.rolling(period, min_periods=1).sum()

