"""Daily BTC signals must fail closed and cannot observe unfinished bars."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from qt.btc_agent.signals import DAY_MS, Candle, evaluate


def _candles(prices: list[float], volumes: list[float] | None = None) -> list[Candle]:
    result: list[Candle] = []
    for index, close in enumerate(prices):
        open_price = prices[max(0, index - 1)]
        result.append(
            Candle(
                open_time=index * DAY_MS,
                open=open_price,
                high=max(open_price, close) * 1.01,
                low=min(open_price, close) * 0.99,
                close=close,
                volume=volumes[index] if volumes is not None else 100.0,
            )
        )
    return result


def test_recovery_after_recent_capitulation_is_a_buy_only_when_flat() -> None:
    candles = _candles([100.0] * 208 + [60.0, 61.0])
    signal = evaluate(candles, 210 * DAY_MS)
    assert signal.action == "buy"
    assert signal.bar_time == 209 * DAY_MS
    assert signal.price == 61.0
    assert signal.atr > 0
    assert signal.factors["capitulation_recent"] is True
    assert signal.factors["market_stress"] is False
    assert evaluate(candles, 210 * DAY_MS, has_position=True).action == "hold"


def test_crash_without_completed_recovery_does_not_buy() -> None:
    signal = evaluate(_candles([100.0] * 209 + [60.0]), 210 * DAY_MS)
    assert signal.factors["capitulation_recent"] is True
    assert signal.factors["stabilized"] is False
    assert signal.action == "hold"


def test_small_drawdown_is_not_extreme_even_with_stress_and_recovery() -> None:
    signal = evaluate(_candles([100.0] * 208 + [80.0, 81.0]), 210 * DAY_MS)
    assert signal.factors["rsi14"] < 25
    assert signal.factors["capitulation_recent"] is False
    assert signal.action == "hold"


def test_price_distress_without_stress_is_not_a_buy() -> None:
    signal = evaluate(
        _candles([100.0] * 180 + [float(99 - i) for i in range(28)] + [70.0, 71.0]),
        210 * DAY_MS,
    )
    assert signal.factors["stabilized"] is True
    assert signal.factors["capitulation_recent"] is False
    assert signal.action == "hold"


def test_drawdown_and_stress_without_oversold_rsi_are_not_a_buy() -> None:
    prices = [100.0] * 160 + [40.0] * 20 + [float(40 + i) for i in range(30)]
    signal = evaluate(_candles(prices, [100.0] * 209 + [300.0]), 210 * DAY_MS)
    assert signal.factors["rsi14"] > 25
    assert signal.factors["drawdown90"] < -0.30
    assert signal.factors["market_stress"] is True
    assert signal.action == "hold"


def test_distress_and_stress_must_coincide_on_the_same_bar() -> None:
    prices = [100.0] * 180 + [float(99 - i) for i in range(28)] + [70.0, 71.0]
    volumes = [100.0] * 207 + [200.0, 100.0, 100.0]
    signal = evaluate(_candles(prices, volumes), 210 * DAY_MS)
    assert signal.factors["stabilized"] is True
    assert signal.factors["capitulation_recent"] is False
    assert signal.action == "hold"


@pytest.mark.parametrize(("volume", "expected"), [(199.9, "hold"), (200.0, "buy")])
def test_volume_stress_uses_prior_thirty_days_and_includes_exact_threshold(
    volume: float, expected: str
) -> None:
    prices = [100.0] * 180 + [float(99 - i) for i in range(28)] + [70.0, 71.0]
    volumes = [100.0] * 208 + [volume, 100.0]
    signal = evaluate(_candles(prices, volumes), 210 * DAY_MS)
    assert signal.action == expected


def test_eight_percent_stress_threshold_allows_floating_point_roundoff() -> None:
    prices = [100.0] * 178 + [float(99 - i) for i in range(30)] + [64.4, 65.0]
    signal = evaluate(_candles(prices), 210 * DAY_MS)
    assert signal.action == "buy"


def test_stress_event_expires_after_three_completed_days() -> None:
    candles = _candles([100.0] * 205 + [60.0, 59.0, 58.0, 57.0, 58.0])
    signal = evaluate(candles, 210 * DAY_MS)
    assert signal.factors["stabilized"] is True
    assert signal.factors["capitulation_recent"] is False
    assert signal.action == "hold"


def test_sell_requires_open_position_and_both_overheat_conditions() -> None:
    candles = _candles([float(50 + index) for index in range(210)])
    signal = evaluate(candles, 210 * DAY_MS, has_position=True)
    assert signal.factors["rsi14"] == 100.0
    assert signal.factors["sma200_ratio"] > 1.5
    assert signal.action == "sell"
    assert evaluate(candles, 210 * DAY_MS).action == "hold"
    modest_rise = _candles([100.0] * 208 + [101.0, 102.0])
    assert evaluate(modest_rise, 210 * DAY_MS, has_position=True).action == "hold"
    low_rsi = evaluate(_candles([100.0] * 208 + [1000.0, 700.0]), 210 * DAY_MS, has_position=True)
    assert low_rsi.factors["sma200_ratio"] > 1.5
    assert low_rsi.factors["rsi14"] < 80
    assert low_rsi.action == "hold"


def test_unfinished_bar_cannot_change_signal_until_close() -> None:
    candles = _candles([100.0] * 209 + [60.0, 61.0])
    now = 210 * DAY_MS + DAY_MS // 2
    assert evaluate(candles, now) == evaluate(candles[:-1], now)
    assert evaluate(candles, now).action == "hold"
    assert evaluate(candles, 211 * DAY_MS).action == "buy"


def test_flat_market_and_zero_volume_are_neutral_and_finite() -> None:
    signal = evaluate(_candles([100.0] * 210, [0.0] * 210), 210 * DAY_MS)
    assert signal.action == "hold"
    assert signal.factors["rsi14"] == 50.0
    assert signal.factors["volume_ratio30"] == 0.0


def test_insufficient_completed_history_fails_closed() -> None:
    with pytest.raises(ValueError, match="210 completed"):
        evaluate(_candles([100.0] * 210), 210 * DAY_MS - 1)


def test_stale_data_fails_closed_at_more_than_thirty_six_hours() -> None:
    candles = _candles([100.0] * 210)
    evaluate(candles, 210 * DAY_MS + 36 * 60 * 60 * 1000)
    with pytest.raises(ValueError, match="stale"):
        evaluate(candles, 210 * DAY_MS + 36 * 60 * 60 * 1000 + 1)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_nonfinite_and_negative_market_data_fail_closed(field: str, value: float) -> None:
    candles = _candles([100.0] * 210)
    candles[-1] = replace(candles[-1], **{field: value})
    with pytest.raises(ValueError, match=r"finite|positive"):
        evaluate(candles, 210 * DAY_MS)


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_zero_prices_fail_closed(field: str) -> None:
    candles = _candles([100.0] * 210)
    candles[-1] = replace(candles[-1], **{field: 0.0})
    with pytest.raises(ValueError, match="positive"):
        evaluate(candles, 210 * DAY_MS)


def test_missing_duplicate_and_reversed_bars_fail_closed() -> None:
    candles = _candles([100.0] * 211)
    invalid = [[*candles[:100], *candles[101:]], [*candles, candles[-1]], candles[::-1]]
    for series in invalid:
        with pytest.raises(ValueError, match="contiguous"):
            evaluate(series, 211 * DAY_MS)


def test_bad_candle_boundaries_and_future_timestamps_fail_closed() -> None:
    candles = _candles([100.0] * 210)
    with pytest.raises(ValueError, match="UTC daily boundary"):
        evaluate([replace(candles[0], open_time=1), *candles[1:]], 210 * DAY_MS)
    with pytest.raises(ValueError, match="future"):
        evaluate([*candles, replace(candles[-1], open_time=212 * DAY_MS)], 210 * DAY_MS)
    with pytest.raises(ValueError, match="contain"):
        evaluate([*candles[:-1], replace(candles[-1], high=99.0)], 210 * DAY_MS)
    with pytest.raises(ValueError, match="contain"):
        evaluate([*candles[:-1], replace(candles[-1], low=101.0)], 210 * DAY_MS)


def test_candle_and_signal_are_frozen_dataclasses() -> None:
    candles = _candles([100.0] * 210)
    with pytest.raises(FrozenInstanceError):
        candles[-1].close = 10.0  # type: ignore[misc]
    signal = evaluate(candles, 210 * DAY_MS)
    with pytest.raises(FrozenInstanceError):
        signal.action = "buy"  # type: ignore[misc]
