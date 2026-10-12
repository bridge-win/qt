"""Chronology, accounting, and risk checks for the rule-only daily replay."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from qt.btc_agent import backtest
from qt.btc_agent.signals import DAY_MS, Candle, Signal


def _candles(prices: list[float]) -> list[Candle]:
    return [
        Candle(
            index * DAY_MS,
            prices[max(0, index - 1)],
            max(prices[max(0, index - 1)], price),
            min(prices[max(0, index - 1)], price),
            price,
            100.0,
        )
        for index, price in enumerate(prices)
    ]


def _report(result: dict[str, object], name: str = "strategy") -> dict[str, object]:
    return cast(dict[str, object], result[name])


def _fills(result: dict[str, object], name: str = "strategy") -> list[dict[str, object]]:
    return cast(list[dict[str, object]], _report(result, name)["fills"])


def _always_buy(candles: list[Candle], now_ms: int, *, has_position: bool = False) -> Signal:
    latest = candles[-1]
    assert now_ms == latest.open_time + DAY_MS
    return Signal("hold" if has_position else "buy", "test", latest.open_time, latest.close, 1, {})


def test_actual_capitulation_signal_fills_only_at_next_open_with_costs() -> None:
    candles = _candles([100.0] * 208 + [60.0, 61.0, 62.0])
    candles[-1] = replace(candles[-1], open=63.0, high=64.0)
    result = backtest.run_backtest(candles)
    fill = _fills(result)[0]
    assert fill["signal_bar_time"] == 209 * DAY_MS
    assert fill["fill_time"] == 210 * DAY_MS
    assert fill["fill_price"] == pytest.approx(63 * 1.001)
    assert fill["notional_quote"] == pytest.approx(25 / 1.001)
    assert fill["fee_quote"] == pytest.approx(25 - 25 / 1.001)
    assert _report(result)["cash_quote"] == 75
    assert _report(result)["closed_trades_count"] == 0
    assert _report(result)["open_quantity_btc"] == pytest.approx(25 / 1.001 / (63 * 1.001))


def test_final_signal_is_recorded_without_an_impossible_fill() -> None:
    result = backtest.run_backtest(_candles([100.0] * 209 + [60.0, 61.0]))
    assert _fills(result) == []
    assert result["pending_unfilled_action"] == "buy"
    assert _report(result)["final_equity_quote"] == 100


def test_flat_market_benchmark_costs_and_no_forced_final_liquidation() -> None:
    result = backtest.run_backtest(_candles([100.0] * 212))
    assert _report(result)["net_return_pct"] == 0
    assert _report(result, "buy_and_hold")["final_equity_quote"] == pytest.approx(100 / 1.001**2)
    assert _report(result, "monthly_dca")["final_equity_quote"] == pytest.approx(75 + 25 / 1.001**2)
    assert len(_fills(result, "buy_and_hold")) == 1
    assert _report(result, "buy_and_hold")["closed_trades_count"] == 0
    assert cast(dict[str, object], result["assumptions"])["codex_reviewer_included"] is False


def test_daily_close_stop_fills_next_open_and_accounts_for_gap_and_both_fees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backtest, "evaluate", _always_buy)
    candles = _candles([100.0] * 210 + [80.0, 70.0])
    candles[-1] = replace(candles[-1], open=72.0)
    result = backtest.run_backtest(candles)
    entry, exit_fill = _fills(result)
    assert exit_fill["reason"] == "daily_close_stop_10pct"
    assert exit_fill["signal_bar_time"] == 210 * DAY_MS
    assert exit_fill["fill_time"] == 211 * DAY_MS
    assert exit_fill["fill_price"] == pytest.approx(72 * 0.999)
    quantity = cast(float, entry["quantity"])
    proceeds = quantity * 72 * 0.999 * 0.999
    assert _report(result)["cash_quote"] == pytest.approx(75 + proceeds)
    trade = cast(list[dict[str, object]], _report(result)["closed_trades"])[0]
    assert trade["net_pnl_quote"] == pytest.approx(proceeds - 25)
    assert cast(float, trade["return_pct"]) < -10


def test_intraday_low_does_not_trigger_daily_close_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest, "evaluate", _always_buy)
    candles = _candles([100.0] * 213)
    candles[210] = replace(candles[210], low=40)
    result = backtest.run_backtest(candles)
    assert len(_fills(result)) == 1
    assert _report(result)["closed_trades_count"] == 0


def test_stop_threshold_uses_fee_adjusted_entry_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest, "evaluate", _always_buy)
    result = backtest.run_backtest(_candles([100.0] * 210 + [90.15, 90.15]))
    assert len(_fills(result)) == 2
    assert _fills(result)[-1]["reason"] == "daily_close_stop_10pct"


@pytest.mark.parametrize(("peak", "expected_fills"), [(115.0, 1), (120.2, 1), (125.0, 2)])
def test_trailing_exit_requires_activation(
    monkeypatch: pytest.MonkeyPatch,
    peak: float,
    expected_fills: int,
) -> None:
    monkeypatch.setattr(backtest, "evaluate", _always_buy)
    result = backtest.run_backtest(_candles([100.0] * 210 + [peak, 100.0, 100.0]))
    assert len(_fills(result)) == expected_fills
    if expected_fills == 2:
        assert _fills(result)[-1]["reason"] == "daily_close_trail_15pct_after_20pct_gain"


def test_time_exit_does_not_fill_before_ninety_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest, "evaluate", _always_buy)
    result = backtest.run_backtest(_candles([100.0] * 301))
    entry, exit_fill = _fills(result)
    assert cast(int, exit_fill["fill_time"]) - cast(int, entry["fill_time"]) == 90 * DAY_MS
    assert exit_fill["reason"] == "maximum_hold_90days"


def test_cooldown_runs_from_last_buy_and_second_entry_uses_its_own_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backtest, "evaluate", _always_buy)
    result = backtest.run_backtest(_candles([100.0] * 210 + [80.0] * 12))
    fills = _fills(result)
    assert [fill["side"] for fill in fills] == ["buy", "sell", "buy"]
    assert cast(int, fills[2]["fill_time"]) - cast(int, fills[0]["fill_time"]) == 7 * DAY_MS
    assert fills[2]["fill_price"] == pytest.approx(80 * 1.001)


def test_evaluator_receives_only_closed_prefixes_and_history_is_future_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, int]] = []

    def review(candles: list[Candle], now_ms: int, *, has_position: bool = False) -> Signal:
        assert len(candles) == now_ms // DAY_MS
        observed.append((len(candles), now_ms))
        return _always_buy(candles, now_ms, has_position=has_position)

    monkeypatch.setattr(backtest, "evaluate", review)
    prefix = _candles([100.0] * 210 + [110.0] * 3)
    first = backtest.run_backtest(prefix)
    full = backtest.run_backtest(_candles([100.0] * 210 + [110.0] * 3 + [20.0, 20.0]))
    assert observed[0] == (210, 210 * DAY_MS)
    assert _fills(first) == _fills(full)[: len(_fills(first))]
    assert _report(first)["equity_curve"] == cast(list[object], _report(full)["equity_curve"])[:3]


def test_monthly_dca_uses_same_initial_cash_and_stops_after_four_purchases() -> None:
    result = backtest.run_backtest(_candles([100.0] * 370))
    assert len(_fills(result, "monthly_dca")) == 4
    assert _report(result, "monthly_dca")["cash_quote"] == 0
    assert _report(result, "monthly_dca")["turnover_quote"] == pytest.approx(100 / 1.001)
    assert _report(result, "monthly_dca")["final_equity_quote"] == pytest.approx(100 / 1.001**2)


def test_buy_hold_drawdown_includes_unrealized_loss() -> None:
    result = backtest.run_backtest(_candles([100.0] * 210 + [200.0, 100.0]))
    assert _report(result, "buy_and_hold")["max_drawdown_pct"] == pytest.approx(50)


def test_missing_data_or_insufficient_history_fails() -> None:
    with pytest.raises(ValueError, match="211"):
        backtest.run_backtest(_candles([100.0] * 210))
    candles = _candles([100.0] * 212)
    with pytest.raises(ValueError, match="contiguous"):
        backtest.run_backtest([*candles[:211], replace(candles[-1], open_time=212 * DAY_MS)])
