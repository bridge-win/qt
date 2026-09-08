from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qt.indicators import talib_standard

talib = pytest.importorskip("talib.abstract", reason="requires qt[native-research] on Python 3.12")


def _ohlcv() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=96, freq="h", tz="UTC")
    close = pd.Series(100 + np.cumsum(np.sin(np.arange(96) / 3) + 0.2), index=index)
    return pd.DataFrame(
        {
            "high": close + 1.7,
            "low": close - 1.2,
            "close": close,
        },
        index=index,
    )


def test_standard_talib_indicators_match_native_library_values_and_lookbacks() -> None:
    frame = _ohlcv()
    # ``talib.abstract`` returns ndarrays for these calls; wrap the native
    # values only to retain the input's time index for lookback assertions.
    expected_atr = pd.Series(talib.ATR(frame, timeperiod=14), index=frame.index)
    expected_rsi = pd.Series(talib.RSI(frame["close"], timeperiod=14), index=frame.index)
    expected_adx = pd.Series(talib.ADX(frame, timeperiod=14), index=frame.index)

    actual_atr = talib_standard.atr(frame["high"], frame["low"], frame["close"], period=14)
    actual_rsi = talib_standard.rsi(frame["close"], period=14)
    actual_adx = talib_standard.adx(frame["high"], frame["low"], frame["close"], period=14)

    assert actual_atr.first_valid_index() == expected_atr.first_valid_index()
    assert actual_rsi.first_valid_index() == expected_rsi.first_valid_index()
    assert actual_adx.first_valid_index() == expected_adx.first_valid_index()
    np.testing.assert_allclose(actual_atr.to_numpy(), expected_atr.to_numpy(), equal_nan=True)
    np.testing.assert_allclose(actual_rsi.to_numpy(), expected_rsi.to_numpy(), equal_nan=True)
    np.testing.assert_allclose(actual_adx.to_numpy(), expected_adx.to_numpy(), equal_nan=True)
