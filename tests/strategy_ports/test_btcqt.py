from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from btc_backtest.engine.models import InstrumentKind, OrderSide, OrderType

from qt.legacy.btcqt.config import S0Cfg, S1Cfg, S2Cfg
from qt.legacy.btcqt.models import Candle, Fill, FundingRate, Intent, MarketState, Regime, Side
from qt.legacy.btcqt.strategy.s0_trend import S0Trend
from qt.legacy.btcqt.strategy.s1_wick_catcher import S1WickCatcher
from qt.legacy.btcqt.strategy.s2_crowding_fader import S2CrowdingFader
from qt.strategy_ports.btcqt import (
    BTCQT_PORTS,
    BtcqtIndicatorStateBuilder,
    BtcqtNativeStrategy,
    CausalState,
    CausalStateTimeline,
    DataVersion,
    MissingRequiredDataError,
    UnsupportedExecutionSemanticsError,
    create_btcqt_port,
)

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _state(*, at: datetime = T0, **changes: object) -> MarketState:
    state = MarketState(
        ts=int(at.timestamp() * 1000),
        price=30_000.0,
        ema_anchor=30_100.0,
        atr=60.0,
        atr_tf=600.0,
        sigma_1m=0.0007,
        vel1=0.0,
        vel3=0.0,
        vel5=0.0,
        dev=-1.5,
        funding_pctl=50.0,
        open_interest_pctl=50.0,
        rv_pctl=50.0,
        regime=Regime.RANGE,
        warmed_up=True,
    )
    for key, value in changes.items():
        setattr(state, key, value)
    return state


def _causal(
    state: MarketState,
    dataset_ids: tuple[str, ...],
    *,
    available_at: datetime | None = None,
) -> CausalState:
    observed_at = datetime.fromtimestamp(state.ts / 1000, tz=timezone.utc)
    availability = available_at or observed_at
    return CausalState(
        state=state,
        observed_at=observed_at,
        available_at=availability,
        inputs=tuple(DataVersion(item, "test-v1", observed_at) for item in dataset_ids),
    )


def _timeline(state: MarketState, dataset_ids: tuple[str, ...]) -> CausalStateTimeline:
    return CausalStateTimeline((_causal(state, dataset_ids),))


def _intent_signature(intents: Sequence[Intent]) -> list[tuple[object, ...]]:
    return [
        (intent.action, intent.strategy, intent.side, intent.price, intent.qty, intent.reason)
        for intent in intents
    ]


def test_s0_port_matches_original_trend_and_donchian_decision() -> None:
    state = _state(trend_score=0.8, trend_agree=3, donchian_break=1, atr_1h=300.0)
    direct = S0Trend(S0Cfg()).on_state(state, 10_000.0)

    port = create_btcqt_port("btcqt_s0_trend")
    decision = port.decide(_timeline(state, ("ohlcv_1m",)), timestamp=T0, equity=10_000.0)

    assert _intent_signature(decision.intents) == _intent_signature(direct)
    intent = port.order_intents(decision)[0]
    assert intent.order_type is OrderType.MARKET
    assert intent.side is OrderSide.BUY
    assert intent.instrument is InstrumentKind.PERPETUAL


def test_s0_fill_callback_preserves_stop_and_stopout_cooldown() -> None:
    state = _state(trend_score=0.8, trend_agree=3, donchian_break=1, atr_1h=300.0)
    causal = _causal(state, ("ohlcv_1m",))
    port = create_btcqt_port("btcqt_s0_trend")
    entry = port.decide(CausalStateTimeline((causal,)), timestamp=T0, equity=10_000.0).intents[0]
    fill = Fill(
        ts=state.ts,
        order_id="entry",
        strategy="s0",
        side=Side.BUY,
        price=30_000.0,
        qty=entry.qty or 0.0,
        fee=1.0,
        kind="MARKET",
    )

    follow_up = port.on_fill(fill, causal)

    assert follow_up.intents[0].action == "PLACE_STOP"
    assert follow_up.intents[0].price == pytest.approx(29_250.0)
    stop = port.order_intents(follow_up)[0]
    assert stop.order_type is OrderType.STOP
    assert stop.instrument is InstrumentKind.PERPETUAL
    assert stop.stop_price == pytest.approx(29_250.0)

    stopped = Fill(
        ts=state.ts + 60_000,
        order_id="stop",
        strategy="s0",
        side=Side.SELL,
        price=29_240.0,
        qty=entry.qty or 0.0,
        fee=1.0,
        kind="STOP",
    )
    port.on_fill(stopped, causal)
    assert isinstance(port.strategy, S0Trend)
    assert port.strategy.cooldown_until == state.ts + 60_000 + 120 * 60_000


def test_s1_confirm_port_matches_reject_recovery_and_keeps_ioc_semantics() -> None:
    state = _state(
        event_active=True,
        event_id=7,
        event_dir="down",
        event_extreme=29_000.0,
        event_vwap=30_200.0,
        event_verdict="reject",
    )
    datasets = ("ohlcv_1m", "event_state")
    direct = S1WickCatcher(S1Cfg()).on_state(state, 10_000.0)
    port = create_btcqt_port("btcqt_s1_wick_confirm")
    decision = port.decide(_timeline(state, datasets), timestamp=T0, equity=10_000.0)

    assert _intent_signature(decision.intents) == _intent_signature(direct)
    assert decision.intents[0].action == "LIMIT_IOC_ENTER"
    with pytest.raises(UnsupportedExecutionSemanticsError, match="IOC"):
        port.order_intents(decision)


def test_s1_ladder_preserves_replace_cancel_stop_and_tp_callbacks() -> None:
    state = _state()
    port = create_btcqt_port("btcqt_s1_wick_ladder")
    causal = _causal(state, ("ohlcv_1m",))
    armed = port.decide(CausalStateTimeline((causal,)), timestamp=T0, equity=10_000.0)
    assert {item.action for item in armed.intents} == {"REPLACE_LADDER"}
    with pytest.raises(UnsupportedExecutionSemanticsError, match="REPLACE_LADDER"):
        port.order_intents(armed)

    first_buy = next(item for item in armed.intents if item.side is Side.BUY)
    fill = Fill(
        ts=state.ts,
        order_id="rung",
        strategy="s1",
        side=Side.BUY,
        price=first_buy.prices[0],
        qty=first_buy.qtys[0],
        fee=1.0,
        kind="LIMIT",
    )
    follow_up = port.on_fill(fill, causal)
    assert {item.action for item in follow_up.intents} >= {
        "CANCEL_LADDER",
        "PLACE_STOP",
        "PLACE_TP",
    }


def test_s2_port_matches_persistent_funding_oi_exhaustion_logic() -> None:
    port = create_btcqt_port("btcqt_s2_crowding_fader", {"enabled": True})
    direct = S2CrowdingFader(S2Cfg(enabled=True))
    datasets = ("ohlcv_1m", "funding_settlements", "open_interest")
    states = []
    for offset in (0, 8):
        at = T0 + timedelta(hours=offset)
        states.append(
            _causal(
                _state(at=at, funding_pctl=99.0, open_interest_pctl=90.0, dev=1.0, vel5=-0.2),
                datasets,
            )
        )
    timeline = CausalStateTimeline(states)

    first = port.decide(timeline, timestamp=T0, equity=10_000.0)
    direct_first = direct.on_state(states[0].state, 10_000.0)
    second_at = T0 + timedelta(hours=8)
    second = port.decide(timeline, timestamp=second_at, equity=10_000.0)
    direct_second = direct.on_state(states[1].state, 10_000.0)

    assert _intent_signature(first.intents) == _intent_signature(direct_first)
    assert _intent_signature(second.intents) == _intent_signature(direct_second)
    assert second.intents[0].side is Side.SELL


def test_s2_rejects_missing_funding_or_open_interest_instead_of_zero_filling() -> None:
    state = _state(funding_pctl=99.0, open_interest_pctl=90.0, dev=1.0, vel5=-0.2)
    port = create_btcqt_port("btcqt_s2_crowding_fader", {"enabled": True})

    with pytest.raises(MissingRequiredDataError, match="funding_settlements, open_interest"):
        port.decide(_timeline(state, ("ohlcv_1m",)), timestamp=T0, equity=10_000.0)


def test_future_state_cannot_be_selected_or_affected_by_mutation() -> None:
    current = _causal(_state(trend_score=0.8, trend_agree=3, donchian_break=1, atr_1h=300.0), ("ohlcv_1m",))
    future_at = T0 + timedelta(minutes=1)
    future = _causal(
        _state(at=future_at, trend_score=-0.9, trend_agree=3, donchian_break=-1, atr_1h=300.0),
        ("ohlcv_1m",),
        available_at=future_at,
    )
    timeline = CausalStateTimeline((current, future))
    port = create_btcqt_port("btcqt_s0_trend")

    before = port.decide(timeline, timestamp=T0, equity=10_000.0)
    future.state.trend_score = 999.0
    after = port.decide(timeline, timestamp=T0, equity=10_000.0)

    assert _intent_signature(before.intents) == _intent_signature(after.intents)
    admitted = timeline.at(T0)
    admitted.state.trend_score = -999.0
    assert timeline.at(T0).state.trend_score == 0.8


def test_metadata_exposes_source_identity_and_native_hook() -> None:
    metadata = BTCQT_PORTS["btcqt_s1_wick_confirm"]

    assert metadata.source.commit == "4aa80b4cb09fdbf5a14f749986cba3c9d16535c9"
    assert metadata.source.module == "qt.legacy.btcqt"
    assert metadata.native_hook == "BtcqtStrategyPort.decide/on_fill"


def test_indicator_builder_uses_original_incremental_engine_with_causal_identity() -> None:
    builder = BtcqtIndicatorStateBuilder()
    candle = Candle(
        ts=int(T0.timestamp() * 1000),
        open=30_000.0,
        high=30_010.0,
        low=29_990.0,
        close=30_005.0,
        volume=10.0,
    )
    observed_at = T0 + timedelta(minutes=1)
    state = builder.on_candle(
        candle,
        available_at=observed_at,
        inputs=(DataVersion("ohlcv_1m", "test-v1", observed_at),),
    )

    assert state.state is not builder.engine.last_state
    assert state.state.price == 30_005.0
    assert state.observed_at == observed_at


def test_native_s0_uses_actual_source_entry_stop_and_exit_with_fees() -> None:
    """A native USDT-M run exercises the preserved S0 fill/stop lifecycle."""

    pytest.importorskip("nautilus_trader")
    from qt.nautilus.models import ExecutionAssumptions
    from qt.nautilus.runner import InstrumentDefinition, NautilusDataFrameRunner

    rows = 20_530  # 14d S0 trend horizon plus an adverse bar for its source stop.
    index = pd.date_range("2025-01-01", periods=rows, freq="1min", tz="UTC")
    close = 100 * np.exp(np.arange(rows) * 0.0001)
    close[20_501:] = close[20_500] * 0.8
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": np.full(rows, 10.0),
        },
        index=index,
    )
    strategy = BtcqtNativeStrategy(
        create_btcqt_port("btcqt_s0_trend"),
        dataset_version="sha256:s0-native-fixture",
        leverage=Decimal("2"),
    )

    run = NautilusDataFrameRunner().run(
        frame=frame,
        strategy=strategy,
        instrument=InstrumentDefinition(kind="perpetual"),
        assumptions=ExecutionAssumptions(
            initial_cash=Decimal("10000"),
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
            leverage=Decimal("2"),
        ),
    )

    actions = [dict(trace.observed_values).get("source_actions") for trace in run.traces if trace.intent_count]
    assert ("MARKET_ENTER",) in actions
    assert len(run.fills) >= 2
    assert set(run.fills["side"]) == {"BUY", "SELL"}
    assert run.metrics["costs"]["fees"] != "0"
    assert run.account_invariants["status"] == "unavailable"


def test_indicator_builder_queues_future_funding_until_its_close_and_availability() -> None:
    builder = BtcqtIndicatorStateBuilder()
    funding_at = T0 + timedelta(minutes=2)
    builder.on_funding_settled(
        FundingRate(ts=int(funding_at.timestamp() * 1000), rate=0.001),
        available_at=funding_at,
    )
    first_close = T0 + timedelta(minutes=1)
    first = builder.on_candle(
        Candle(int(T0.timestamp() * 1000), 30_000, 30_001, 29_999, 30_000, 10),
        available_at=first_close,
        inputs=(DataVersion("ohlcv_1m", "test-v1", first_close),),
    )
    second_close = T0 + timedelta(minutes=2)
    second = builder.on_candle(
        Candle(int(first_close.timestamp() * 1000), 30_000, 30_001, 29_999, 30_000, 10),
        available_at=second_close,
        inputs=(
            DataVersion("ohlcv_1m", "test-v1", second_close),
            DataVersion("funding_settlements", "test-v1", second_close),
        ),
    )

    assert first.state.funding_rate is None
    assert second.state.funding_rate == 0.001


@pytest.mark.parametrize("equity", [0.0, -1.0, float("inf"), float("nan")])
def test_port_rejects_invalid_equity(equity: float) -> None:
    state = _state(trend_score=0.8, trend_agree=3, donchian_break=1, atr_1h=300.0)
    port = create_btcqt_port("btcqt_s0_trend")

    with pytest.raises(ValueError, match="finite and positive"):
        port.decide(_timeline(state, ("ohlcv_1m",)), timestamp=T0, equity=equity)


def test_data_versions_require_nonempty_identity_and_aware_availability() -> None:
    with pytest.raises(ValueError, match="dataset_id and version"):
        DataVersion("", "v1", T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        DataVersion("ohlcv_1m", "v1", datetime(2024, 1, 1))

    with pytest.raises(ValueError, match="precedes one of its input versions"):
        CausalState(
            state=_state(),
            observed_at=T0,
            available_at=T0,
            inputs=(DataVersion("ohlcv_1m", "v1", T0 + timedelta(seconds=1)),),
        )
