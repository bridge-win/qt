"""Financial semantics exercised unchanged in the pinned rc4 and rc7 images."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, OrderIntent, OrderSide, OrderType
from btc_backtest.strategies.base import StrategyMetadata

from qt.nautilus.models import ExecutionAssumptions
from qt.nautilus.runner import (
    InstrumentDefinition,
    NautilusDataFrameRunner,
    NautilusUnavailableError,
    _build_instrument,
    _fee_model,
    _runtime,
    _validate_frame,
)


class RoundTrip:
    """Fixed quantities isolate fee changes from portfolio allocation changes."""

    metadata = StrategyMetadata(id="fee_round_trip", version="1", description="Fee contract fixture",
                                warmup_bars=0, supported_timeframes=("1h",))
    parameters: ClassVar[dict[str, object]] = {}

    def __init__(self, *, maker_entry: bool = False) -> None:
        self.maker_entry = maker_entry
        self.sent = self.exited = False

    def on_bar(self, context: object) -> tuple[OrderIntent, ...]:
        if not self.sent:
            self.sent = True
            return (OrderIntent(instrument=InstrumentKind.SPOT, side=OrderSide.BUY,
                                order_type=OrderType.LIMIT if self.maker_entry else OrderType.MARKET,
                                base_quantity=Decimal("1"),
                                limit_price=Decimal("95") if self.maker_entry else None,
                                reason="fixed_entry"),)
        if not self.exited and context.portfolio.position(InstrumentKind.SPOT).quantity == 1:
            self.exited = True
            return (OrderIntent(instrument=InstrumentKind.SPOT, side=OrderSide.SELL,
                                order_type=OrderType.MARKET, base_quantity=Decimal("1"), reason="fixed_exit"),)
        return ()


def round_trip_frame() -> pd.DataFrame:
    return pd.DataFrame({"open": [100.0] * 5, "high": [101.0] * 5,
                         "low": [99.0, 94.0, 99.0, 99.0, 99.0],
                         "close": [100.0] * 5, "volume": [100.0] * 5},
                        index=pd.date_range("2020-01-01", periods=5, freq="1h", tz="UTC"))


@pytest.mark.native_engine
@pytest.mark.parametrize(("maker_entry", "expected_buy_fee", "expected_terminal"), [
    (False, Decimal("1"), Decimal("998")),
    (True, Decimal("0.2375"), Decimal("1003.7625")),
])
def test_explicit_maker_and_taker_rates_change_cash_by_exact_commissions(
    maker_entry: bool, expected_buy_fee: Decimal, expected_terminal: Decimal,
) -> None:
    pytest.importorskip("nautilus_trader")
    results = []
    for maker, taker in ((Decimal("0"), Decimal("0")), (Decimal("0.0025"), Decimal("0.01"))):
        results.append(NautilusDataFrameRunner().run(
            frame=round_trip_frame(), strategy=RoundTrip(maker_entry=maker_entry),
            assumptions=ExecutionAssumptions(initial_cash=Decimal("1000"), maker_fee=maker, taker_fee=taker),
            instrument=InstrumentDefinition(timestamp_at_close=True),
        ))
    zero, charged = results
    assert len(charged.fills) == len(zero.fills) == 2
    commissions = [Decimal(str(row.commissions[0]).split()[0]) for row in charged.fills.itertuples()]
    assert commissions == [expected_buy_fee, Decimal("1")]
    assert charged.terminal_equity == expected_terminal
    assert zero.terminal_equity - charged.terminal_equity == sum(commissions)
    assert Decimal(charged.metrics["costs"]["fees"]) == sum(commissions)
    assert charged.metrics["costs"]["fees_by_currency"] == {"USDT": str(sum(commissions).quantize(Decimal("0.00000001")))}
    assert charged.account_invariants["status"] == "verified"
    assert Decimal(charged.account_invariants["actual_base_balance"]) == 0
    assert Decimal(charged.account_invariants["actual_quote_balance"]) == expected_terminal


def test_fee_api_never_silently_assumes_an_unknown_version_is_compatible() -> None:
    with pytest.raises(NautilusUnavailableError, match="fee API"):
        _fee_model(SimpleNamespace(version="2.0.0rc99"), ExecutionAssumptions(initial_cash=Decimal("1000")))


def test_fractional_bar_interval_cannot_make_future_close_available_early() -> None:
    frame = round_trip_frame()
    frame.index = pd.date_range("2020-01-01", periods=5, freq="1500ms", tz="UTC")
    with pytest.raises(ValueError, match="whole-second"):
        _validate_frame(frame, ExecutionAssumptions(initial_cash=Decimal("1000")))


@pytest.mark.native_engine
def test_future_mark_quotes_do_not_change_historical_terminal_value_or_horizon() -> None:
    pytest.importorskip("nautilus_trader")
    frame = round_trip_frame()
    base = frame.index
    quotes = pd.DataFrame({"available_at": base, "bid": [99.0] * 5, "ask": [101.0] * 5,
                           "bid_size": [100.0] * 5, "ask_size": [100.0] * 5}, index=base)
    future_time = base[-1] + pd.Timedelta(days=1)
    future = pd.DataFrame({"available_at": [future_time], "bid": [999999.0], "ask": [1000001.0],
                           "bid_size": [100.0], "ask_size": [100.0]}, index=[future_time])
    outputs = []
    for marks in (quotes, pd.concat([quotes, future])):
        outputs.append(NautilusDataFrameRunner().run(
            frame=frame, strategy=RoundTrip(), assumptions=ExecutionAssumptions(initial_cash=Decimal("1000")),
            instrument=InstrumentDefinition(timestamp_at_close=True), mark_quotes=marks,
        ))
    prefix, poisoned = outputs
    assert prefix.terminal_timestamp == poisoned.terminal_timestamp == frame.index[-1].to_pydatetime()
    assert prefix.terminal_equity == poisoned.terminal_equity
    assert prefix.traces == poisoned.traces
    assert prefix.metrics["mark_data"]["records"] == poisoned.metrics["mark_data"]["records"] == 5


@pytest.mark.native_engine
@pytest.mark.parametrize("probability", [0.0, 1.0])
def test_one_tick_native_slippage_is_adverse_on_both_sides_and_fees_remain_real(probability: float) -> None:
    pytest.importorskip("nautilus_trader")
    outcomes = []
    for slippage in (False, True):
        outcomes.append(NautilusDataFrameRunner().run(
            frame=round_trip_frame(), strategy=RoundTrip(),
            instrument=InstrumentDefinition(timestamp_at_close=True),
            assumptions=ExecutionAssumptions(initial_cash=Decimal("1000"),
                                             one_tick_slippage=slippage,
                                             slippage_probability=probability if slippage else 0.0),
        ))
    plain, slipped = outcomes
    assert [Decimal(str(value)) for value in plain.fills["avg_px"]] == [Decimal("100"), Decimal("100")]
    assert [Decimal(str(value)) for value in slipped.fills["avg_px"]] == [Decimal("100.01"), Decimal("99.99")]
    assert Decimal(plain.metrics["costs"]["fees"]) == Decimal(slipped.metrics["costs"]["fees"]) == Decimal("0.2")
    assert plain.terminal_equity - slipped.terminal_equity == Decimal("0.02")
    assert slipped.metrics["costs"]["slippage_probability"] == 1.0


@pytest.mark.native_engine
def test_seeded_probabilistic_slippage_is_reproducible_and_never_adds_two_ticks() -> None:
    pytest.importorskip("nautilus_trader")
    results = []
    for _ in range(2):
        results.append(NautilusDataFrameRunner().run(
            frame=round_trip_frame(), strategy=RoundTrip(),
            instrument=InstrumentDefinition(timestamp_at_close=True),
            assumptions=ExecutionAssumptions(initial_cash=Decimal("1000"), one_tick_slippage=True,
                                             slippage_probability=0.5, seed=7),
        ))
    left, right = results
    assert list(left.fills["avg_px"]) == list(right.fills["avg_px"])
    assert left.terminal_equity == right.terminal_equity
    buy, sell = [Decimal(str(value)) for value in left.fills["avg_px"]]
    assert buy in {Decimal("100"), Decimal("100.01")}
    assert sell in {Decimal("100"), Decimal("99.99")}
    assert left.metrics["costs"]["slippage_probability"] == 0.5
    assert left.metrics["costs"]["slippage"] == "probabilistic_one_tick"


@pytest.mark.native_engine
@pytest.mark.parametrize("kind", ["spot", "perpetual"])
def test_instrument_and_venue_bind_the_verified_fee_api_for_both_instrument_kinds(kind: str) -> None:
    pytest.importorskip("nautilus_trader")
    runtime = _runtime()
    assumptions = ExecutionAssumptions(initial_cash=Decimal("1000"), maker_fee=Decimal("0.0025"), taker_fee=Decimal("0.01"))
    instrument, _ = _build_instrument(runtime, round_trip_frame(), InstrumentDefinition(kind=kind), assumptions)
    if runtime.version == "2.0.0rc4":
        assert instrument.maker_fee == assumptions.maker_fee
        assert instrument.taker_fee == assumptions.taker_fee
        assert _fee_model(runtime, assumptions) is None
    else:
        assert runtime.version == "2.0.0rc7"
        assert not hasattr(instrument, "maker_fee")
        assert type(_fee_model(runtime, assumptions)).__name__ == "MakerTakerFeeModel"


@pytest.mark.native_engine
def test_v2_candidate_experiments_still_require_separate_release_acceptance(tmp_path) -> None:
    pytest.importorskip("nautilus_trader")
    from qt.contracts import ExperimentSpecV2
    from qt.experiments.native import run
    from tests.test_experiment_native import native_input

    spec, *rest = native_input(tmp_path)
    spec = ExperimentSpecV2.model_validate({**spec.model_dump(mode="python"),
                                           "engine": {**spec.engine.model_dump(mode="python"), "version": "2.0.0rc7"}})
    with pytest.raises(ValueError, match="version_mismatch_or_unaccepted_upgrade"):
        run(spec, *rest)
