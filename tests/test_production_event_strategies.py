"""Simple rules consume only the causal prefix and emit desired positions."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from qt.contracts import MarketEvent, StrategyState
from qt.strategies.event_driven import (
    EventStrategyParameters,
    MeanReversionStrategy,
    MomentumStrategy,
    WickOrderFlowStrategy,
)


def parameters(**changes) -> EventStrategyParameters:
    return EventStrategyParameters(**{
        "deployment_id": "d1", "venue": "fixture", "instrument": "BTCUSDT",
        "position_size": "0.01", "lookback": 2, "threshold_bps": "100", **changes,
    })


def event(index, close="100", **features) -> MarketEvent:
    return MarketEvent(
        venue="fixture", instrument="BTCUSDT", event_type="bar",
        exchange_ts_ns=(index + 1) * 10, received_ts_ns=(index + 1) * 10 + 1,
        available_at_ns=(index + 1) * 10,
        payload={"open_ts_ns": index * 10, "close_ts_ns": (index + 1) * 10,
                 "open": "100", "high": "200", "low": "50", "close": close,
                 "volume": "1", **features},
    )


def run(strategy, events):
    signals = []
    for item in events:
        signals.extend(strategy.on_event(item, StrategyState()))
    return signals


@pytest.mark.parametrize("strategy_type,closes", [
    (MomentumStrategy, ["100", "100", "102", "99"]),
    (MeanReversionStrategy, ["100", "100", "98", "102"]),
])
def test_momentum_and_reversion_reference_rules_enter_then_exit(strategy_type, closes) -> None:
    strategy = strategy_type(parameters())
    signals = run(strategy, [event(index, close) for index, close in enumerate(closes)])
    assert [signal.target_position for signal in signals] == [Decimal("0.01"), Decimal("0")]
    assert all(signal.deployment_id == "d1" for signal in signals)
    assert signals[0].produced_at_ns == 31


def test_strategy_replay_identity_and_future_mutations_preserve_prefix() -> None:
    prefix = [event(0), event(1), event(2, "102")]
    expected = run(MomentumStrategy(parameters()), prefix)
    assert expected == run(MomentumStrategy(parameters()), [*prefix, event(3, "103")])
    changed = run(MomentumStrategy(parameters()), [*prefix, event(3, "99")])
    assert changed[:len(expected)] == expected


def test_resume_restores_strategy_prefix_and_rejects_other_parameters() -> None:
    first = MomentumStrategy(parameters())
    run(first, [event(0), event(1)])
    snapshot = first.snapshot_state()
    restored = MomentumStrategy(parameters())
    assert restored.on_event(event(2, "102"), snapshot) == first.on_event(event(2, "102"), StrategyState())
    with pytest.raises(ValueError, match="different"):
        MomentumStrategy(parameters(threshold_bps="200")).restore_state(snapshot)


def test_state_survives_canonical_parameter_roundtrip_without_decimal_rounding() -> None:
    original_parameters = parameters(position_size="0.0100")
    strategy = MomentumStrategy(original_parameters)
    run(strategy, [event(0), event(1), event(2, "102")])
    restored_parameters = EventStrategyParameters.model_validate_json(original_parameters.model_dump_json())
    resumed = MomentumStrategy(restored_parameters)
    resumed.restore_state(strategy.snapshot_state())
    assert resumed.on_event(event(3, "99"), StrategyState())[0].target_position == 0


def test_forming_bar_or_gap_never_generates_an_order() -> None:
    strategy = MomentumStrategy(parameters())
    run(strategy, [event(0), event(1)])
    future = event(2, "102").model_copy(update={"exchange_ts_ns": 20, "received_ts_ns": 21})
    with pytest.raises(ValueError, match="future"):
        strategy.on_event(future, StrategyState())
    with pytest.raises(ValueError, match="gap"):
        strategy.on_event(event(3, "102"), StrategyState())
    assert strategy.on_event(event(2, "102"), StrategyState())


def test_duplicate_bar_cannot_enter_twice() -> None:
    strategy = MomentumStrategy(parameters())
    run(strategy, [event(0), event(1)])
    signals = strategy.on_event(event(2, "102"), StrategyState())
    assert signals
    assert strategy.on_event(event(2, "102"), StrategyState()) == []


def test_wick_requires_flow_and_valid_depth_confirmation() -> None:
    strategy = WickOrderFlowStrategy(parameters())
    assert strategy.on_event(event(0, "110", high="115", low="50"), StrategyState()) == []
    signals = strategy.on_event(event(1, "110", high="115", low="50", book_valid=True,
                                      buy_volume="3", sell_volume="1", depth_recovery_ratio="1.1",
                                      features_available_at_ns=20), StrategyState())
    assert len(signals) == 1 and signals[0].target_position == Decimal("0.01")


def test_wick_cannot_use_future_feature_values() -> None:
    strategy = WickOrderFlowStrategy(parameters())
    with pytest.raises(ValueError, match="causal"):
        strategy.on_event(event(0, "110", high="115", low="50", book_valid=True,
                                buy_volume="3", sell_volume="1", depth_recovery_ratio="1.1",
                                features_available_at_ns=12), StrategyState())


def test_position_size_is_decimal_and_untrusted_market_events_are_ignored() -> None:
    with pytest.raises(ValidationError):
        parameters(position_size=0.01)
    strategy = MomentumStrategy(parameters())
    unsafe = event(0).model_copy(update={"quality_flags": ("BOOK_INVALID",)})
    assert strategy.on_event(unsafe, StrategyState()) == []
