from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, OrderIntent, OrderSide, OrderType
from btc_backtest.strategies.base import StrategyMetadata

from qt.nautilus.executor import (
    NautilusResearchExecutor,
    _canonical_metrics,
    _equity_returns,
    _equity_series,
    _execution_window,
    _instrument,
    _monthly_returns,
)
from qt.nautilus.models import ExecutionAssumptions
from qt.nautilus.runner import (
    InstrumentDefinition,
    NativeLedgerError,
    NativeRunCancelledError,
    NautilusDataFrameRunner,
    _assert_spot_ledger,
    _native_costs,
    _quantize_currency,
    _row_fees,
    _validate_frame,
)


def _require_native_engine() -> None:
    try:
        import nautilus_trader  # noqa: F401
    except ImportError:
        pytest.skip("requires the isolated ARM64 NautilusTrader test environment")


class _OneShotBuy:
    metadata = StrategyMetadata(
        id="native_one_shot_buy",
        version="1",
        description="Places one native market order.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )
    parameters: ClassVar[dict[str, object]] = {}

    def __init__(self) -> None:
        self._sent = False

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        if self._sent:
            return ()
        self._sent = True
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("100"),
                reason="native_smoke_buy",
            ),
        )


class _OneShotPerpetualShort(_OneShotBuy):
    metadata = StrategyMetadata(
        id="native_one_shot_perpetual_short",
        version="1",
        description="Places one native perpetual short order.",
        warmup_bars=0,
        supported_timeframes=("1h",),
        supported_instruments=(InstrumentKind.PERPETUAL,),
    )

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        if self._sent:
            return ()
        self._sent = True
        return (
            OrderIntent(
                instrument=InstrumentKind.PERPETUAL,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                base_quantity=Decimal("1"),
                reason="native_perpetual_short",
            ),
        )


class _AllInSpotBuy(_OneShotBuy):
    metadata = StrategyMetadata(
        id="native_all_in_spot_buy",
        version="1",
        description="Exercises fee-reserved native cash sizing.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        if self._sent:
            return ()
        self._sent = True
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("1000"),
                reason="all_in_fee_reserved",
            ),
        )


class _NoOpNativeStrategy:
    metadata = StrategyMetadata(
        id="native_performance_noop",
        version="1",
        description="Exercises native event dispatch without strategy computation.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )
    parameters: ClassVar[dict[str, object]] = {}

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        del context
        return ()


class _LastBarEntry:
    metadata = StrategyMetadata(
        id="native_last_bar_entry",
        version="1",
        description="Submits only at the final decision point.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )
    parameters: ClassVar[dict[str, object]] = {}

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        if len(context.bars) != 5:
            return ()
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quote_amount=Decimal("100"),
                reason="last_bar_entry",
            ),
        )


class _EnterThenLastBarExit:
    metadata = StrategyMetadata(
        id="native_last_bar_exit",
        version="1",
        description="Enters early then exits at the final decision point.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )
    parameters: ClassVar[dict[str, object]] = {}

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        if len(context.bars) == 1:
            return (
                OrderIntent(
                    instrument=InstrumentKind.SPOT,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quote_amount=Decimal("100"),
                    reason="entry",
                ),
            )
        if len(context.bars) == 5:
            return (
                OrderIntent(
                    instrument=InstrumentKind.SPOT,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    base_quantity=Decimal("1"),
                    reason="last_bar_exit",
                ),
            )
        return ()


def _bars() -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=5, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [10.0, 10.0, 10.0, 10.0, 10.0],
        },
        index=index,
    )


def _multi_year_bars(*, periods: int, frequency: str) -> pd.DataFrame:
    index = pd.date_range("2015-01-01", periods=periods, freq=frequency, tz="UTC")
    close = np.arange(periods, dtype=float) * 0.01 + 100
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.ones(periods),
        },
        index=index,
    )


@pytest.mark.native_engine
def test_real_native_engine_fills_and_reports_actual_fee() -> None:
    _require_native_engine()
    run = NautilusDataFrameRunner().run(
        frame=_bars(),
        strategy=_OneShotBuy(),
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("1000"),
            taker_fee=Decimal("0.01"),
        ),
    )

    assert run.engine_version == "2.0.0rc4"
    assert len(run.orders) == 1
    assert len(run.fills) == 1
    assert run.fills.iloc[0]["status"] == "FILLED"
    fill = run.fills.iloc[0]
    commission = Decimal(str(fill["commissions"][0]).split()[0])
    assert run.metrics["costs"]["fees"] == str(commission)
    assert run.metrics["costs"]["fees_by_currency"] == {"USDT": str(commission)}
    assert run.metrics["costs"]["spread"] is None
    assert len(run.traces) == len(_bars())
    assert run.traces[0].observed_rows == 1
    equity = _equity_series(run.traces, native=run)
    metrics = _canonical_metrics(equity)
    quantity = Decimal(str(fill["filled_qty"]))
    execution_price = Decimal(str(fill["avg_px"]))
    expected_terminal = Decimal("1000") - quantity * execution_price - commission + quantity * Decimal(
        "104",
    )
    assert run.terminal_equity == expected_terminal
    assert equity[list(equity)[-1]] == float(expected_terminal)
    assert metrics["net_return"] == pytest.approx(float(expected_terminal / Decimal("1000") - 1))
    assert metrics["annualization_periods"] == pytest.approx(8766.0)


@pytest.mark.native_engine
def test_real_native_all_in_spot_order_reserves_fee_and_reconciles_cash_ledger() -> None:
    _require_native_engine()
    run = NautilusDataFrameRunner().run(
        frame=_bars(),
        strategy=_AllInSpotBuy(),
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("1000"),
            taker_fee=Decimal("0.01"),
        ),
    )

    invariant = run.account_invariants
    assert invariant["status"] == "verified"
    assert invariant["violations"] == []
    expected_quote = Decimal("1000")
    expected_base = Decimal("0")
    for fill in run.fills.itertuples(index=False):
        quantity = Decimal(str(fill.filled_qty))
        price = Decimal(str(fill.avg_px))
        fee = sum(
            (
                Decimal(str(commission).split()[0])
                for commission in fill.commissions
                if str(commission).endswith(" USDT")
            ),
            Decimal("0"),
        )
        expected_quote -= quantity * price + fee
        expected_base += quantity
    expected_quote = expected_quote.quantize(Decimal("0.00000001"))
    actual_quote = Decimal(str(invariant["actual_quote_balance"]))
    actual_base = Decimal(str(invariant["actual_base_balance"]))
    assert actual_quote == expected_quote
    assert actual_base == expected_base
    assert actual_quote >= 0


def test_commission_currency_breakdown_never_sums_incompatible_amounts() -> None:
    fills = pd.DataFrame({"commissions": [("1.25 USDT", "0.0005 BTC")]})
    costs = _native_costs(
        fills,
        ExecutionAssumptions(initial_cash=Decimal("1000")),
        quote_currency="USDT",
    )

    assert costs["fees"] is None
    assert costs["fees_by_currency"] == {"BTC": "0.0005", "USDT": "1.25"}
    assert costs["fee_conversion_status"] == "unavailable"
    assert _row_fees(
        ("1.25 USDT", "0.0005 BTC"),
        quote_currency="USDT",
        base_currency="BTC",
    ) == (Decimal("1.25"), Decimal("0.0005"))
    with pytest.raises(NativeLedgerError, match="fill-time FX"):
        _row_fees(
            ("2.0 BNB",),
            quote_currency="USDT",
            base_currency="BTC",
        )


def test_spot_ledger_refuses_aggregated_fills_without_raw_native_transactions() -> None:
    class Money:
        def as_decimal(self) -> Decimal:
            return Decimal("100")

    class Account:
        def balance_total(self, currency: object) -> Money:
            del currency
            return Money()

    class Portfolio:
        def account(self, *, venue: object) -> Account:
            del venue
            return Account()

    class Instrument:
        base_currency = "BTC"

    with pytest.raises(NativeLedgerError, match="raw fill transactions"):
        _assert_spot_ledger(
            Portfolio(),
            venue="QT",
            instrument=Instrument(),
            quote_currency="USDT",
            initial_cash=Decimal("100"),
            fills=pd.DataFrame(
                {
                    "side": ["BUY"],
                    "filled_qty": ["1"],
                    "avg_px": ["100"],
                    "commissions": [("0 USDT",)],
                }
            ),
            fill_transactions=(),
            quote_precision=8,
            base_precision=8,
            kind="spot",
        )


@pytest.mark.native_engine
def test_currency_quantization_matches_native_money_half_even_rounding() -> None:
    _require_native_engine()
    from nautilus_trader.model import Money

    value = Decimal("1.234567845")
    expected = _quantize_currency(value, 8)

    assert expected == Decimal("1.23456784")
    assert str(Money.from_str(f"{value} USDT")) == f"{expected} USDT"


def test_ohlcv_validation_rejects_nan_inf_and_gaps_but_allows_zero_volume() -> None:
    frame = _bars()
    _validate_frame(frame.assign(volume=0), ExecutionAssumptions(initial_cash=Decimal("100")))

    for column, value in (("close", np.nan), ("high", np.inf)):
        invalid = frame.copy()
        invalid.iloc[1, invalid.columns.get_loc(column)] = value
        with pytest.raises(ValueError, match="finite"):
            _validate_frame(invalid, ExecutionAssumptions(initial_cash=Decimal("100")))

    gapped = frame.drop(frame.index[2])
    with pytest.raises(ValueError, match="gaps"):
        _validate_frame(gapped, ExecutionAssumptions(initial_cash=Decimal("100")))


@pytest.mark.native_engine
def test_real_native_perpetual_allows_bounded_research_leverage_and_short_entry() -> None:
    _require_native_engine()
    run = NautilusDataFrameRunner().run(
        frame=_bars(),
        strategy=_OneShotPerpetualShort(),
        instrument=InstrumentDefinition(kind="perpetual"),
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("1000"),
            leverage=Decimal("2"),
        ),
    )

    assert len(run.fills) == 1
    assert run.fills.iloc[0]["side"] == "SELL"
    assert not bool(run.fills.iloc[0]["is_reduce_only"])


@pytest.mark.native_engine
def test_real_native_event_hook_stops_a_cancelled_run_before_report_publication() -> None:
    _require_native_engine()
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    with pytest.raises(NativeRunCancelledError, match="during event dispatch"):
        NautilusDataFrameRunner().run(
            frame=_bars(),
            strategy=_NoOpNativeStrategy(),
            assumptions=ExecutionAssumptions(initial_cash=Decimal("1000")),
            cancelled=cancelled,
        )
    assert calls == 3


@pytest.mark.native_engine
@pytest.mark.parametrize(
    ("strategy", "expected_fills"),
    ((_LastBarEntry, 1), (_EnterThenLastBarExit, 2)),
)
def test_terminal_account_equity_includes_last_bar_fills_without_mutating_traces(
    strategy: type[object],
    expected_fills: int,
) -> None:
    _require_native_engine()
    run = NautilusDataFrameRunner().run(
        frame=_bars(),
        strategy=strategy(),
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("1000"),
            taker_fee=Decimal("0.01"),
        ),
    )

    series = _equity_series(run.traces, native=run)
    causal_terminal = Decimal(dict(run.traces[-1].observed_values)["native_equity"])
    assert len(run.fills) == expected_fills
    assert run.initial_equity == Decimal("1000")
    assert run.terminal_equity != causal_terminal
    assert series[run.terminal_timestamp.isoformat()] == float(run.terminal_equity)
    assert Decimal(str(run.metrics["costs"]["fees"])) > 0


@pytest.mark.native_engine
@pytest.mark.performance
def test_real_native_multi_year_bar_dispatch_stays_within_research_budget() -> None:
    """Regression guard for engine dispatch and causal-context allocation cost.

    This is intentionally a no-op strategy: it measures framework overhead,
    rather than claiming that arbitrary user strategy calculations are cheap.
    """
    _require_native_engine()
    started = perf_counter()
    observed = 0
    for periods, frequency in ((365 * 5 * 24, "1h"), (365 * 10 * 6, "4h")):
        run = NautilusDataFrameRunner().run(
            frame=_multi_year_bars(periods=periods, frequency=frequency),
            strategy=_NoOpNativeStrategy(),
            assumptions=ExecutionAssumptions(initial_cash=Decimal("1000")),
        )
        observed += len(run.traces)
    assert observed == 365 * 5 * 24 + 365 * 10 * 6
    # Conservative for the 2-vCPU remote runner; measured ARM M1 Pro time was
    # 11.6 seconds for both fixtures after eliminating the per-bar deep copy.
    assert perf_counter() - started < 75


def test_execution_window_preserves_warmup_without_pre_window_orders() -> None:
    frame = _bars()
    selected, decision_start = _execution_window(
        frame,
        {"from": frame.index[2].isoformat(), "to": frame.index[3].isoformat()},
    )

    assert selected.index.tolist() == frame.index[:4].tolist()
    assert decision_start == frame.index[2]


def test_mark_to_market_return_series_monthly_returns_and_cagr_calmar_are_canonical() -> None:
    equity = {
        "2025-01-01T00:00:00+00:00": 100.0,
        "2025-01-15T00:00:00+00:00": 90.0,
        "2025-02-01T00:00:00+00:00": 110.0,
    }

    metrics = _canonical_metrics(equity)

    returns = _equity_returns(equity)
    assert returns["2025-01-15T00:00:00+00:00"] == pytest.approx(-0.1)
    assert returns["2025-02-01T00:00:00+00:00"] == pytest.approx(110 / 90 - 1)
    monthly = _monthly_returns(equity)
    assert monthly["2025-01"] == pytest.approx(-0.1)
    assert monthly["2025-02"] == pytest.approx(110 / 90 - 1)
    assert pytest.approx(110 / 100) == (1 - 0.1) * (1 + 110 / 90 - 1)
    assert metrics["max_drawdown"] == pytest.approx(-0.1)
    assert metrics["cagr"] > metrics["net_return"]
    assert metrics["calmar"] == pytest.approx(metrics["cagr"] / 0.1)

    flat = _canonical_metrics(
        {
            "2025-01-01T00:00:00+00:00": 100.0,
            "2025-01-02T00:00:00+00:00": 100.0,
        }
    )
    assert flat["calmar"] is None


def test_instrument_identity_preserves_btcusd_quote_and_dataset_market() -> None:
    definition = _instrument(
        {"symbol": "BTC/USD", "provider": "bitstamp", "market": "spot"},
        {},
    )

    assert definition.symbol == "BTCUSD"
    assert definition.venue == "BITSTAMP"
    assert definition.base_currency == "BTC"
    assert definition.quote_currency == "USD"

    with pytest.raises(ValueError, match="does not match"):
        _instrument(
            {"symbol": "BTC/USD", "provider": "bitstamp", "market": "spot"},
            {"market": "perpetual"},
        )


def test_lab_plugin_version_uses_only_injected_isolated_runtime(tmp_path: Path) -> None:
    calls: list[object] = []

    class _Runtime:
        def execute_plugin(
            self,
            strategy_version: object,
            experiment: object,
            parameter_overrides: object,
            progress: object,
            cancelled: object,
        ) -> dict[str, object]:
            calls.extend((strategy_version, experiment, parameter_overrides, progress, cancelled))
            return {"engine": "isolated_plugin"}

    executor = NautilusResearchExecutor(
        tmp_path / "parquet",
        tmp_path / "artifacts",
        plugin_runtime=_Runtime(),
    )
    version = {"version_id": "v1", "content": {"mode": "plugin", "source_code": "ignored"}}
    result = executor.execute(
        {
            "mode": "lab_strategy_version",
            "lab_strategy_version": version,
            "parameter_overrides": {"period": 14},
        },
        lambda stage, percent: None,
        lambda: False,
    )

    assert result == {"engine": "isolated_plugin"}
    assert calls[0] is version
    assert calls[2] == {"period": 14}
