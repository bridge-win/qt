from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pandas as pd
import pandas.testing as pdt
import pytest

from qt.strategies.base import EvaluationResult, StrategyConfig
from qt.strategies.capitulation import Capitulation
from qt.strategies.carry import BasisCarry
from qt.strategies.dca import SmartDCA
from qt.strategies.sim.basis_carry import BasisCarry as SimBasisCarry
from qt.strategies.sim.smart_dca import SmartDCA as SimSmartDCA
from qt.strategies.sim.trend_weekly import WeeklyTrend as SimWeeklyTrend
from qt.strategies.sim.wick_catcher import WickCatcher as SimWickCatcher
from qt.strategies.trend import WeeklyTrend
from qt.strategies.wick_catcher import WickCatcher
from qt.strategy_ports.btcqt import DataVersion
from qt.strategy_ports.gallery import (
    GALLERY_PORTS,
    GalleryCausalInput,
    GalleryLivePort,
    GalleryPortDataError,
    GallerySimulationPort,
    create_gallery_port,
)

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _ohlcv(rows: int) -> pd.DataFrame:
    index = pd.date_range(T0, periods=rows, freq="h", tz="UTC")
    close = pd.Series([100.0 + number * 0.02 for number in range(rows)], index=index)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def _context(data: dict[str, object], *datasets: str, decision_at: datetime | None = None) -> GalleryCausalInput:
    timestamp = decision_at or _ohlcv_timestamp(data)
    return GalleryCausalInput(
        data,
        timestamp,
        timestamp,
        tuple(DataVersion(dataset, "fixture-v1", timestamp) for dataset in datasets),
    )


def _ohlcv_timestamp(data: dict[str, object]) -> datetime:
    ohlcv = data.get("ohlcv")
    if not isinstance(ohlcv, pd.DataFrame):
        return T0
    return cast(datetime, ohlcv.index[-1].to_pydatetime())


def _assert_evaluation_equal(actual: EvaluationResult, expected: EvaluationResult) -> None:
    assert (actual.opportunity is None) == (expected.opportunity is None)
    assert actual.metrics == expected.metrics
    assert actual.notes == expected.notes
    if actual.opportunity is not None and expected.opportunity is not None:
        assert actual.opportunity.action == expected.opportunity.action
        assert actual.opportunity.confidence == pytest.approx(expected.opportunity.confidence)
        assert actual.opportunity.reason == expected.opportunity.reason
        assert actual.opportunity.details == expected.opportunity.details


@pytest.mark.parametrize(
    ("strategy_id", "source"),
    [
        ("qt5_smart_dca", SmartDCA),
        ("qt5_capitulation", Capitulation),
        ("qt5_weekly_trend", WeeklyTrend),
        ("qt5_basis_carry", BasisCarry),
        ("qt5_wick_catcher", WickCatcher),
    ],
)
def test_live_ports_delegate_the_original_evaluation_interface(
    strategy_id: str,
    source: type[SmartDCA] | type[Capitulation] | type[WeeklyTrend] | type[BasisCarry] | type[WickCatcher],
) -> None:
    ohlcv = _ohlcv(24 * 24)
    funding = pd.DataFrame({"funding_rate": 0.0001}, index=ohlcv.index[-48:])
    data: dict[str, object] = {"ohlcv": ohlcv, "funding": funding}
    if strategy_id == "qt5_wick_catcher":
        data = {"ohlcv": ohlcv.tail(2)}
    context = _context(data, *data.keys())
    port = create_gallery_port(strategy_id)
    assert isinstance(port, GalleryLivePort)
    expected = source(StrategyConfig(name=strategy_id, params={})).evaluate(context.visible_data())
    actual = port.evaluate(context).evaluation
    _assert_evaluation_equal(actual, expected)
    assert port.metadata.interface == "live_evaluation_result"


@pytest.mark.parametrize(
    ("strategy_id", "source"),
    [
        ("qt5_sim_smart_dca", SimSmartDCA),
        ("qt5_sim_weekly_trend", SimWeeklyTrend),
        ("qt5_sim_basis_carry", SimBasisCarry),
        ("qt5_sim_wick_catcher", SimWickCatcher),
    ],
)
def test_simulation_ports_delegate_the_original_batch_interface(
    strategy_id: str,
    source: type[SimSmartDCA] | type[SimWeeklyTrend] | type[SimBasisCarry] | type[SimWickCatcher],
) -> None:
    ohlcv = _ohlcv(24 * 30)
    funding = pd.Series(0.0001, index=ohlcv.index, name="funding")
    data: dict[str, object] = {"ohlcv": ohlcv}
    if strategy_id == "qt5_sim_basis_carry":
        data["funding"] = funding
    context = _context(data, *data.keys())
    port = create_gallery_port(strategy_id)
    assert isinstance(port, GallerySimulationPort)
    if strategy_id == "qt5_sim_smart_dca":
        expected = cast(type[SimSmartDCA], source)().run(ohlcv)
    elif strategy_id == "qt5_sim_weekly_trend":
        expected = cast(type[SimWeeklyTrend], source)().run(ohlcv)
    elif strategy_id == "qt5_sim_basis_carry":
        expected = cast(type[SimBasisCarry], source)().run(ohlcv, funding=funding)
    else:
        expected = cast(type[SimWickCatcher], source)().run(ohlcv)
    actual = port.run(context).result
    pdt.assert_series_equal(actual.equity, expected.equity)
    pdt.assert_series_equal(actual.target_weight, expected.target_weight)
    pdt.assert_series_equal(actual.short_weight, expected.short_weight)
    pdt.assert_frame_equal(actual.trades, expected.trades)
    pdt.assert_frame_equal(actual.diagnostics, expected.diagnostics)
    assert port.metadata.interface == "batch_strategy_result"


def test_live_source_missing_data_preserves_watch_behavior() -> None:
    port = create_gallery_port("qt5_basis_carry")
    assert isinstance(port, GalleryLivePort)
    decision = port.evaluate(_context({}, decision_at=T0))
    assert decision.evaluation.opportunity is None
    assert decision.evaluation.metrics == {"reason": "no funding data"}


def test_causal_clipping_and_snapshot_copy_exclude_future_candle_mutation() -> None:
    ohlcv = _ohlcv(3)
    decision_at = ohlcv.index[-2].to_pydatetime()
    context = _context({"ohlcv": ohlcv}, "ohlcv", decision_at=decision_at)
    ohlcv.loc[ohlcv.index[-2], "low"] = 0.0
    ohlcv.loc[ohlcv.index[-1], "low"] = 0.0
    port = create_gallery_port("qt5_wick_catcher")
    assert isinstance(port, GalleryLivePort)
    decision = port.evaluate(context)
    expected_input = _ohlcv(3).iloc[:2].copy()
    expected = WickCatcher(StrategyConfig(name="qt5_wick_catcher", params={})).evaluate({"ohlcv": expected_input})
    _assert_evaluation_equal(decision.evaluation, expected)


def test_versions_are_required_for_supplied_live_data_and_batch_inputs() -> None:
    ohlcv = _ohlcv(24)
    live = create_gallery_port("qt5_wick_catcher")
    assert isinstance(live, GalleryLivePort)
    with pytest.raises(GalleryPortDataError, match="unversioned supplied data"):
        live.evaluate(_context({"ohlcv": ohlcv}, decision_at=ohlcv.index[-1].to_pydatetime()))

    sim = create_gallery_port("qt5_sim_basis_carry")
    assert isinstance(sim, GallerySimulationPort)
    with pytest.raises(GalleryPortDataError, match="requires funding"):
        sim.run(_context({"ohlcv": ohlcv}, "ohlcv"))


def test_metadata_covers_five_live_and_four_distinct_simulation_ports() -> None:
    assert len(GALLERY_PORTS) == 9
    assert {item.interface for item in GALLERY_PORTS.values()} == {"live_evaluation_result", "batch_strategy_result"}
    assert GALLERY_PORTS["qt5_basis_carry"].native_hook.endswith("paired spot/perp proposal")
    assert GALLERY_PORTS["qt5_sim_wick_catcher"].native_hook.startswith("research-only")
