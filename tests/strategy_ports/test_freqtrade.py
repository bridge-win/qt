from __future__ import annotations

import builtins
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pandas as pd
import pytest

from qt.legacy.btc_quant_evolution.freqtrade_strategies import (
    BtcMultiSourceRegimeStrategy as BtcMultiSourceRegimeStrategySource,
)
from qt.legacy.btc_quant_evolution.freqtrade_strategies import multisource_features, sota_indicators
from qt.legacy.btc_quant_main.freqtrade_strategies import catalog_signals
from qt.strategy_ports.btcqt import DataVersion
from qt.strategy_ports.freqtrade import (
    FREQTRADE_PORTS,
    FreqtradeCausalFrame,
    FreqtradeOrder,
    FreqtradeProtectionState,
    FreqtradeRuntimeUnavailableError,
    MissingFreqtradeDataError,
    create_freqtrade_port,
)
from qt.workbench.catalog import legacy_catalog

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
            "volume": 1000.0,
        },
        index=index,
    )


def _context(frame: pd.DataFrame, *datasets: str) -> FreqtradeCausalFrame:
    timestamp = frame.index[-1].to_pydatetime()
    return FreqtradeCausalFrame(
        frame,
        timestamp,
        timestamp,
        tuple(DataVersion(dataset, "fixture-v1", timestamp) for dataset in datasets),
    )


def _features(candles: pd.DataFrame) -> pd.DataFrame:
    dates = candles.index
    return pd.DataFrame(
        {
            "date": dates,
            "technical_score": 0.8,
            "derivatives_score": 0.2,
            "onchain_score": 0.1,
            "sentiment_score": 0.1,
            "liquidity_score": 0.2,
            "liquidity_bad": False,
            "leverage_crowded": False,
            "available_at": dates,
            "fixture_available_at": dates,
            "fixture_source": "fixture",
            "fixture_dataset": "scores",
            "feature_sources": "fixture",
        }
    )


def test_catalog_factory_covers_all_100_profiles_without_environment_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREQTRADE_PROFILE", raising=False)
    profiles = cast(Sequence[Mapping[str, object]], legacy_catalog()["profiles"])
    ports = [create_freqtrade_port("btc_quant_catalog", profile_id=str(profile["id"])) for profile in profiles]

    assert len(ports) == 100
    assert len({port.fingerprint for port in ports}) == 100
    assert "FREQTRADE_PROFILE" not in os.environ


def test_catalog_port_matches_preserved_signal_and_risk_calculation() -> None:
    candles = _candles(300)
    port = create_freqtrade_port("btc_quant_catalog", profile_id="ema-pullback-v01")
    decision = port.decide(_context(candles, "ohlcv_4h"), equity=10_000, max_stake=5_000)

    assert list(decision.frame.index) == list(candles.index)
    assert decision.protections == FREQTRADE_PORTS["btc_quant_catalog"].protections
    assert pd.api.types.is_bool_dtype(decision.frame["enter_long"])
    assert decision.frame["exit_long"].dtype == bool
    profiles = cast(Sequence[Mapping[str, object]], legacy_catalog()["profiles"])
    profile = next(item for item in profiles if item["id"] == "ema-pullback-v01")
    direct = catalog_signals.add_catalog_indicators(candles.assign(date=candles.index))
    entry_signals = [
        catalog_signals.evaluate_signal(
            str(signal_id),
            direct,
            cast(Mapping[str, object], cast(Mapping[str, object], profile["signal_params"]).get(str(signal_id), {})),
        )
        for signal_id in cast(Sequence[object], profile["entry_signals"])
    ]
    expected_entry = pd.concat(entry_signals, axis=1).all(axis=1)
    assert decision.frame["enter_long"].equals(expected_entry)
    exit_signals = [
        catalog_signals.evaluate_signal(
            str(signal_id),
            direct,
            cast(Mapping[str, object], cast(Mapping[str, object], profile["signal_params"]).get(str(signal_id), {})),
        )
        for signal_id in cast(Sequence[object], profile["exit_signals"])
    ]
    expected_exit = pd.concat(exit_signals, axis=1).any(axis=1)
    assert decision.frame["exit_long"].equals(expected_exit)
    assert FREQTRADE_PORTS["btc_quant_catalog"].hard_stoploss == pytest.approx(-0.25)


def test_future_candle_or_availability_is_rejected_before_source_evaluation() -> None:
    candles = _candles(300)
    timestamp = candles.index[-2].to_pydatetime()
    with pytest.raises(ValueError, match="future rows"):
        FreqtradeCausalFrame(candles, timestamp, timestamp, (DataVersion("ohlcv_4h", "v1", timestamp),))

    with pytest.raises(ValueError, match="not available"):
        FreqtradeCausalFrame(candles.iloc[:-1], timestamp, candles.index[-1].to_pydatetime(), (DataVersion("ohlcv_4h", "v1", timestamp),))


@pytest.mark.parametrize("equity", [0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_nonfinite_equity_is_rejected(equity: float) -> None:
    candles = _candles(260)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=_features(candles))
    with pytest.raises(ValueError, match="equity and max_stake"):
        port.decide(
            _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=equity, max_stake=5_000
        )


def test_missing_required_data_never_turns_into_a_zero_signal() -> None:
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=_features(_candles(260)))
    with pytest.raises(MissingFreqtradeDataError, match="multisource_feature_matrix"):
        port.decide(_context(_candles(260), "ohlcv_4h"), equity=10_000, max_stake=5_000)


def test_external_feature_availability_and_mutation_cannot_leak_future_data() -> None:
    candles = _candles(260)
    features = _features(candles)
    features.loc[features.index[-1], "available_at"] = candles.index[-1] + timedelta(minutes=1)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=features)
    with pytest.raises(MissingFreqtradeDataError, match="available_at is unavailable"):
        port.decide(
            _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
        )

    stable_features = _features(candles)
    stable = create_freqtrade_port("btc_multisource_regime", feature_matrix=stable_features)
    stable_features.loc[stable_features.index[-1], "technical_score"] = -1_000.0
    decision = stable.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
    )
    assert decision.frame["technical_score"].iloc[-1] == pytest.approx(0.8)

    invalid_features = _features(candles)
    invalid_features.loc[invalid_features.index[-1], "technical_score"] = float("nan")
    invalid = create_freqtrade_port("btc_multisource_regime", feature_matrix=invalid_features)
    with pytest.raises(MissingFreqtradeDataError, match="invalid technical_score"):
        invalid.decide(
            _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
        )


def test_multisource_port_delegates_source_atr_and_enters_from_causal_features() -> None:
    candles = _candles(260)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=_features(candles))
    decision = port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"),
        equity=10_000,
        max_stake=5_000,
    )

    assert decision.frame["atr"].iloc[-1] > 0
    assert decision.frame["entry_score"].iloc[-1] == pytest.approx(1.4)
    assert decision.orders[0].action == "enter_long"
    assert decision.input_versions[-1].dataset_id == "multisource_feature_matrix"
    assert decision.orders[0].price is not None
    assert decision.orders[0].stop_distance is not None
    direct = multisource_features.merge_feature_matrix(candles.assign(date=candles.index), _features(candles))
    assert decision.frame["atr"].equals(BtcMultiSourceRegimeStrategySource._atr(direct))

    trade = port.on_fill(
        decision.orders[0],
        order_id=decision.orders[0].order_id,
        fill_price=float(candles["close"].iloc[-1]),
        fill_quantity=decision.orders[0].quantity,
        filled_at=candles.index[-1].to_pydatetime(),
        fees=0.25,
    )
    assert trade is not None
    assert trade.initial_stop == pytest.approx(
        float(candles["close"].iloc[-1]) - decision.orders[0].stop_distance
    )
    assert trade.active_stop >= trade.entry_price * 0.75
    assert not port.roi_exit_reached(trade, trade.entry_price)
    assert port.roi_exit_reached(trade, trade.entry_price * 101)
    managed = port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"),
        equity=10_000,
        max_stake=5_000,
        trade=trade,
    )
    assert not managed.orders
    initial_stop = port.initial_stop_order(trade, submitted_at=candles.index[-1].to_pydatetime())
    assert initial_stop.action == "replace_stop"
    with pytest.raises(ValueError, match="cancel-and-replace"):
        port.order_intents(replace(managed, orders=(initial_stop,)))


def test_multisource_volume_entry_gate_and_exit_signal_precede_trailing_stop() -> None:
    candles = _candles(260)
    features = _features(candles)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=features)
    zero_volume = candles.copy()
    zero_volume.loc[zero_volume.index[-1], "volume"] = 0.0
    assert not port.decide(
        _context(zero_volume, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
    ).orders

    entry = port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
    ).orders[0]
    trade = port.on_fill(
        entry,
        order_id=entry.order_id,
        fill_price=float(candles["close"].iloc[-1]),
        fill_quantity=entry.quantity,
        filled_at=candles.index[-1].to_pydatetime(),
        fees=0.0,
    )
    exit_features = features.copy()
    exit_features.loc[exit_features.index[-1], "technical_score"] = 0.0
    exit_features.loc[exit_features.index[-1], "derivatives_score"] = 0.0
    exit_features.loc[exit_features.index[-1], "onchain_score"] = 0.0
    exit_features.loc[exit_features.index[-1], "sentiment_score"] = 0.0
    exit_features.loc[exit_features.index[-1], "liquidity_score"] = 0.0
    exit_port = create_freqtrade_port("btc_multisource_regime", feature_matrix=exit_features)
    exit_decision = exit_port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000, trade=trade
    )
    assert exit_decision.orders[0].action == "exit_long"
    assert exit_decision.orders[0].reason == "multisource_regime_exit"


def test_partial_fills_fees_and_exit_fills_track_remaining_position() -> None:
    candles = _candles(260)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=_features(candles))
    decision = port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
    )
    entry = decision.orders[0]
    first_quantity = entry.quantity / 2
    first = port.on_fill(
        entry,
        order_id=entry.order_id,
        fill_price=125.0,
        fill_quantity=first_quantity,
        filled_at=candles.index[-1].to_pydatetime(),
        fees=0.10,
    )
    assert first is not None
    second = port.on_fill(
        entry,
        order_id=entry.order_id,
        fill_price=127.0,
        fill_quantity=entry.quantity - first_quantity,
        filled_at=(candles.index[-1] + timedelta(minutes=1)).to_pydatetime(),
        fees=0.20,
        trade=first,
    )
    assert second is not None
    assert second.remaining_quantity == pytest.approx(entry.quantity)
    assert second.entry_price == pytest.approx(126.0)
    assert second.total_fees == pytest.approx(0.30)

    exit_order = FreqtradeOrder("exit_long", second.remaining_quantity, None, "signal_exit", order_id="exit-1")
    partial_exit = port.on_fill(
        exit_order,
        order_id="exit-1",
        fill_price=128.0,
        fill_quantity=second.remaining_quantity / 2,
        filled_at=(candles.index[-1] + timedelta(hours=4)).to_pydatetime(),
        fees=0.15,
        trade=second,
    )
    assert partial_exit is not None
    assert partial_exit.remaining_quantity == pytest.approx(second.remaining_quantity / 2)
    assert partial_exit.total_fees == pytest.approx(0.45)
    closed = port.on_fill(
        exit_order,
        order_id="exit-1",
        fill_price=128.0,
        fill_quantity=partial_exit.remaining_quantity,
        filled_at=(candles.index[-1] + timedelta(hours=4, minutes=1)).to_pydatetime(),
        fees=0.15,
        trade=partial_exit,
    )
    assert closed.is_closed
    assert closed.remaining_quantity == 0
    assert closed.total_fees == pytest.approx(0.60)


def test_native_stop_uses_current_atr_but_never_loosens_an_accepted_stop() -> None:
    candles = _candles(260)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=_features(candles))
    decision = port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000
    )
    trade = port.on_fill(
        decision.orders[0],
        order_id=decision.orders[0].order_id,
        fill_price=float(candles["close"].iloc[-1]),
        fill_quantity=decision.orders[0].quantity,
        filled_at=candles.index[-1].to_pydatetime(),
        fees=0.0,
    )
    assert trade is not None
    rising = candles.copy()
    timestamp = rising.index[-1]
    rising.loc[timestamp, ["open", "high", "low", "close"]] = (134.8, 136.0, 134.0, 135.0)
    rising_decision = port.decide(
        _context(rising, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000, trade=trade
    )
    assert rising_decision.orders[0].action == "replace_stop"
    source_atr = float(rising_decision.frame["atr"].iloc[-1])
    source_candidate = max(135.0 - source_atr * 2.5, trade.entry_price - source_atr * 2.5)
    assert rising_decision.orders[0].price == pytest.approx(max(source_candidate, trade.active_stop))
    raised = port.on_stop_accepted(trade, rising_decision.orders[0])

    volatile = rising.copy()
    volatile.loc[timestamp, ["high", "low", "close"]] = (200.0, 50.0, 136.0)
    volatile_decision = port.decide(
        _context(volatile, "ohlcv_4h", "multisource_feature_matrix"), equity=10_000, max_stake=5_000, trade=raised
    )
    volatile_atr = float(volatile_decision.frame["atr"].iloc[-1])
    source_candidate = max(136.0 - volatile_atr * 2.5, raised.entry_price - volatile_atr * 2.5)
    assert source_candidate < raised.active_stop
    assert not volatile_decision.orders


def test_protection_state_suppresses_entries_for_cooldown_and_drawdown() -> None:
    candles = _candles(260)
    port = create_freqtrade_port("btc_multisource_regime", feature_matrix=_features(candles))
    context = _context(candles, "ohlcv_4h", "multisource_feature_matrix")
    closed_at = candles.index[-1].to_pydatetime()
    cooldown = port.on_trade_closed(FreqtradeProtectionState(), closed_at=closed_at, equity_after=10_000)
    assert cooldown.entry_block_reason(closed_at) == "cooldown"
    assert not port.decide(context, equity=10_000, max_stake=5_000, protection_state=cooldown).orders

    peak_at = closed_at - timedelta(hours=4)
    drawdown = port.on_trade_closed(
        FreqtradeProtectionState(equity_points=()), closed_at=peak_at, equity_after=10_000
    )
    drawdown = port.on_trade_closed(drawdown, closed_at=closed_at, equity_after=8_900)
    shifted = candles.copy()
    shifted.index = shifted.index + timedelta(hours=4)
    shifted_features = _features(shifted)
    later_context = _context(shifted, "ohlcv_4h", "multisource_feature_matrix")
    assert drawdown.entry_block_reason(later_context.decision_at) == "max_drawdown"
    later = create_freqtrade_port("btc_multisource_regime", feature_matrix=shifted_features)
    assert not later.decide(later_context, equity=8_900, max_stake=5_000, protection_state=drawdown).orders


def test_sota_port_uses_source_indicators_and_causal_feature_matrix() -> None:
    candles = _candles(2161)
    port = create_freqtrade_port("btc_sota", feature_matrix=_features(candles))
    decision = port.decide(
        _context(candles, "ohlcv_4h", "multisource_feature_matrix"),
        equity=10_000,
        max_stake=5_000,
    )

    assert decision.frame["feature_row_complete"].iloc[-1]
    assert decision.frame["atr"].iloc[-1] > 0
    assert decision.frame["realized_vol"].iloc[-1] > 0
    assert pd.api.types.is_bool_dtype(decision.frame["enter_long"])
    direct = sota_indicators.add_sota_indicators(
        multisource_features.merge_feature_matrix(candles.assign(date=candles.index), _features(candles))
    )
    assert decision.frame["atr"].equals(direct["atr"])
    assert decision.frame["donchian_entry"].equals(direct["donchian_entry"])


@pytest.mark.parametrize(
    ("strategy_id", "hard_stoploss"),
    [
        ("btc_quant_catalog", -0.25),
        ("btc_atr_fear_volume", -0.25),
        ("btc_donchian_atr", -0.25),
        ("btc_low_freq_trend", -0.35),
        ("btc_multisource_regime", -0.25),
        ("btc_sota", -0.25),
    ],
)
def test_source_hard_stoploss_and_no_synthetic_roi_are_executable_metadata(
    strategy_id: str, hard_stoploss: float
) -> None:
    metadata = FREQTRADE_PORTS[strategy_id]
    assert metadata.hard_stoploss == pytest.approx(hard_stoploss)
    assert metadata.minimal_roi == {"0": 100}


def test_low_freq_passes_through_native_proposed_stake_without_atr_sizing() -> None:
    port = create_freqtrade_port("btc_low_freq_trend")
    row = pd.Series({"close": 100.0, "atr": 1.0})
    with pytest.raises(MissingFreqtradeDataError, match="proposed_stake is required"):
        port._stake_and_stop(row, equity=10_000, max_stake=5_000, min_stake=0.0, proposed_stake=None)
    stake, stop = port._stake_and_stop(row, equity=10_000, max_stake=5_000, min_stake=0.0, proposed_stake=321.0)
    assert stake == pytest.approx(321.0)
    assert stop == pytest.approx(96.8)


@pytest.mark.parametrize("strategy_id", ["btc_atr_fear_volume", "btc_donchian_atr", "btc_low_freq_trend"])
def test_talib_source_families_fail_precisely_when_import_is_unavailable(
    strategy_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__

    def blocked_talib_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "talib.abstract":
            raise ModuleNotFoundError("isolated TA-Lib absence")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_talib_import)
    port = create_freqtrade_port(strategy_id)
    rows = FREQTRADE_PORTS[strategy_id].startup_candles
    with pytest.raises(FreqtradeRuntimeUnavailableError, match="TA-Lib is required"):
        port.decide(_context(_candles(rows), "ohlcv_4h"), equity=10_000, max_stake=5_000)
