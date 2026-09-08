"""Real TA-Lib parity checks; run only in the provisioned native ARM environment."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pandas.testing as pdt
import pytest

from qt.legacy.btc_quant_main.freqtrade_strategies import fear_volume_indicators
from qt.strategy_ports.btcqt import DataVersion
from qt.strategy_ports.freqtrade import FreqtradeCausalFrame, create_freqtrade_port

ta = pytest.importorskip("talib.abstract", reason="real TA-Lib runtime is required")

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _candles(rows: int) -> pd.DataFrame:
    index = pd.date_range(T0, periods=rows, freq="4h", tz="UTC")
    close = pd.Series([100.0 + number * 0.1 for number in range(rows)], index=index)
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000.0,
        },
        index=index,
    )


def _decision(strategy_id: str, candles: pd.DataFrame) -> pd.DataFrame:
    timestamp = candles.index[-1].to_pydatetime()
    context = FreqtradeCausalFrame(
        candles,
        timestamp,
        timestamp,
        (DataVersion("ohlcv_4h", "native-talib-fixture", timestamp),),
    )
    return create_freqtrade_port(strategy_id).decide(context, equity=10_000, max_stake=5_000).frame


def test_atr_fear_volume_uses_real_talib_and_original_indicator_signal_formula() -> None:
    candles = _candles(220)
    actual = _decision("btc_atr_fear_volume", candles)
    direct = candles.assign(date=candles.index)
    direct["atr"] = ta.ATR(direct, timeperiod=14)
    direct = fear_volume_indicators.add_fear_volume_features(direct, fear_window=90, volume_window=30)
    expected_entry = fear_volume_indicators.fear_volume_entry_signal(
        direct, min_drawdown=0.12, min_volume_ratio=1.5, min_atr_pct=0.02
    )
    expected_exit = (direct["volume"] > 0) & (direct["fear_drawdown"] <= 0.04)
    pdt.assert_series_equal(actual["atr"], direct["atr"])
    pdt.assert_series_equal(actual["fear_drawdown"], direct["fear_drawdown"])
    pdt.assert_series_equal(actual["enter_long"], expected_entry, check_names=False)
    pdt.assert_series_equal(actual["exit_long"], expected_exit, check_names=False)


def test_donchian_atr_uses_real_talib_and_source_entry_exit_formula() -> None:
    candles = _candles(120)
    actual = _decision("btc_donchian_atr", candles)
    direct = candles.assign(date=candles.index)
    direct["atr"] = ta.ATR(direct, timeperiod=14)
    direct["adx"] = ta.ADX(direct, timeperiod=14)
    direct["donchian_upper"] = direct["high"].rolling(55).max().shift(1)
    direct["donchian_lower"] = direct["low"].rolling(55).min().shift(1)
    direct["donchian_exit"] = direct["low"].rolling(20).min().shift(1)
    direct["donchian_mid"] = (direct["donchian_upper"] + direct["donchian_lower"]) / 2
    direct["atr_pct"] = direct["atr"] / direct["close"]
    expected_entry = (
        (direct["volume"] > 0)
        & (direct["close"] > direct["donchian_upper"])
        & (direct["adx"] >= 25)
        & (direct["atr_pct"] > 0.005)
        & (direct["atr_pct"] < 0.12)
    )
    expected_exit = (direct["volume"] > 0) & (
        (direct["close"] < direct["donchian_exit"]) | (direct["close"] < direct["donchian_mid"])
    )
    for column in ("atr", "adx", "donchian_upper", "donchian_exit", "donchian_mid", "atr_pct"):
        pdt.assert_series_equal(actual[column], direct[column])
    pdt.assert_series_equal(actual["enter_long"], expected_entry, check_names=False)
    pdt.assert_series_equal(actual["exit_long"], expected_exit, check_names=False)


def test_low_frequency_trend_uses_real_talib_and_source_entry_exit_formula() -> None:
    candles = _candles(1500)
    actual = _decision("btc_low_freq_trend", candles)
    direct = candles.assign(date=candles.index)
    direct["atr"] = ta.ATR(direct, timeperiod=14)
    direct["adx"] = ta.ADX(direct, timeperiod=14)
    direct["fast_ma"] = direct["close"].rolling(300).mean()
    direct["slow_ma"] = direct["close"].rolling(1200).mean()
    direct["breakout_high"] = direct["high"].rolling(330).max().shift(1)
    direct["exit_ma"] = direct["close"].rolling(300).mean()
    direct["exit_low"] = direct["low"].rolling(120).min().shift(1)
    direct["atr_pct"] = direct["atr"] / direct["close"]
    direct["volume_mean"] = direct["volume"].rolling(90).mean()
    direct["volume_ratio"] = direct["volume"] / direct["volume_mean"]
    expected_entry = (
        (direct["volume"] > 0)
        & (direct["close"] > direct["slow_ma"])
        & (direct["fast_ma"] > direct["slow_ma"])
        & (direct["close"] > direct["breakout_high"])
        & (direct["volume_ratio"] >= 0.7)
        & (direct["atr_pct"] >= 0.005)
        & (direct["atr_pct"] <= 0.12)
    )
    expected_exit = (direct["volume"] > 0) & (
        (direct["close"] < direct["exit_ma"]) | (direct["close"] < direct["exit_low"])
    )
    for column in ("atr", "adx", "fast_ma", "slow_ma", "breakout_high", "exit_ma", "exit_low", "volume_ratio"):
        pdt.assert_series_equal(actual[column], direct[column])
    pdt.assert_series_equal(actual["enter_long"], expected_entry, check_names=False)
    pdt.assert_series_equal(actual["exit_long"], expected_exit, check_names=False)
