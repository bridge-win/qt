from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from btc_backtest.strategies.indicators import (
    adx,
    atr,
    bollinger,
    donchian,
    ema,
    keltner,
    macd,
    roc,
    rolling_vwap,
    rsi,
    sma,
    stochastic,
)


def test_sma_and_ema_have_point_in_time_values() -> None:
    values = pd.Series([1.0, 2.0, 3.0, 4.0])

    assert sma(values, 3).tolist()[-2:] == [2.0, 3.0]
    assert ema(values, 3).iloc[-1] == pytest.approx(3.125)
    assert sma(values, 3).iloc[:2].isna().all()
    assert ema(values, 3).iloc[:2].isna().all()


def test_rsi_and_stochastic_have_bounded_hand_calculated_values() -> None:
    rising = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    high = pd.Series([3.0, 4.0, 5.0, 6.0])
    low = pd.Series([1.0, 2.0, 3.0, 4.0])
    close = pd.Series([2.0, 3.0, 4.0, 5.0])

    assert rsi(rising, 3).iloc[-1] == pytest.approx(100.0)
    percent_k, percent_d = stochastic(high, low, close, 3, 2)
    assert percent_k.iloc[-1] == pytest.approx(75.0)
    assert percent_d.iloc[-1] == pytest.approx(75.0)
    assert percent_k.dropna().between(0, 100).all()


def test_macd_returns_line_signal_and_histogram() -> None:
    values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 4.0])

    line, signal, histogram = macd(values, fast=2, slow=3, signal=2)

    pd.testing.assert_series_equal(histogram, line - signal)
    assert line.iloc[:2].isna().all()
    assert signal.iloc[:2].isna().all()


def test_atr_and_adx_use_wilder_point_in_time_smoothing() -> None:
    high = pd.Series([11.0, 12.0, 13.0, 14.0, 15.0])
    low = pd.Series([9.0, 10.0, 11.0, 12.0, 13.0])
    close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])

    average_range = atr(high, low, close, 3)
    strength, plus_di, minus_di = adx(high, low, close, 3)

    assert average_range.iloc[-1] == pytest.approx(2.0)
    assert strength.iloc[-1] == pytest.approx(100.0)
    assert plus_di.iloc[-1] > 0
    assert minus_di.iloc[-1] == pytest.approx(0.0)


def test_bands_channels_and_keltner_have_expected_ordering() -> None:
    close = pd.Series([10.0, 10.0, 10.0, 13.0])
    high = close + 1
    low = close - 1

    lower, middle, upper = bollinger(close, 3, 1.0)
    channel_low, channel_high = donchian(high, low, 3)
    keltner_low, keltner_middle, keltner_high = keltner(
        high,
        low,
        close,
        ema_window=3,
        atr_window=3,
        multiplier=1.0,
    )

    assert middle.iloc[-1] == pytest.approx(11.0)
    assert lower.iloc[-1] < middle.iloc[-1] < upper.iloc[-1]
    assert channel_low.iloc[-1] == pytest.approx(9.0)
    assert channel_high.iloc[-1] == pytest.approx(14.0)
    assert (
        keltner_low.iloc[-1]
        < keltner_middle.iloc[-1]
        < keltner_high.iloc[-1]
    )


def test_rolling_vwap_and_roc_match_hand_calculation() -> None:
    high = pd.Series([11.0, 13.0, 15.0])
    low = pd.Series([9.0, 11.0, 13.0])
    close = pd.Series([10.0, 12.0, 14.0])
    volume = pd.Series([1.0, 2.0, 1.0])

    result = rolling_vwap(high, low, close, volume, 3)

    assert result.iloc[-1] == pytest.approx(12.0)
    assert roc(close, 2).iloc[-1] == pytest.approx(0.4)


def test_zero_volume_vwap_is_nan() -> None:
    values = pd.Series([1.0, 2.0, 3.0])
    result = rolling_vwap(values, values, values, pd.Series([0.0] * 3), 3)

    assert np.isnan(result.iloc[-1])


@pytest.mark.parametrize(
    "call",
    [
        lambda: sma(pd.Series([1.0]), 0),
        lambda: ema(pd.Series([1.0]), -1),
        lambda: macd(pd.Series([1.0]), fast=3, slow=2, signal=1),
        lambda: stochastic(
            pd.Series([1.0]),
            pd.Series([1.0]),
            pd.Series([1.0]),
            1,
            0,
        ),
    ],
)
def test_indicator_parameters_are_validated(call: object) -> None:
    with pytest.raises(ValueError):
        assert callable(call)
        call()


def test_future_mutation_cannot_change_indicator_prefix() -> None:
    original = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    mutated = original.copy()
    mutated.iloc[3:] = [400.0, 500.0]

    pd.testing.assert_series_equal(
        ema(original, 3).iloc[:3],
        ema(mutated, 3).iloc[:3],
    )
