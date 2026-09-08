from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import cast

import pandas as pd
import pytest

from qt.strategy_ports.btcqt import DataVersion
from qt.strategy_ports.gallery import (
    GALLERY_PORTS,
    GalleryCausalInput,
    GalleryLivePort,
    GallerySimulationPort,
    create_gallery_port,
)
from qt.workbench.legacy_workflows import (
    LegacyWorkflowSafetyError,
    legacy_workflow_catalog,
    legacy_workflow_definition,
    run_legacy_workflow,
)

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _ohlcv(rows: int) -> pd.DataFrame:
    index = pd.date_range(T0, periods=rows, freq="1h", tz="UTC")
    close = pd.Series([100.0 + number * 0.02 for number in range(rows)], index=index)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000.0,
        },
        index=index,
    )


def _versions(data: dict[str, object], at: datetime) -> tuple[DataVersion, ...]:
    return tuple(DataVersion(name, "fixture-v1", at) for name in data)


def _context(data: dict[str, object], at: datetime) -> GalleryCausalInput:
    return GalleryCausalInput(
        data=data,
        decision_at=at,
        available_at=at,
        inputs=_versions(data, at),
    )


@pytest.mark.parametrize(
    "workflow_id",
    [
        "qt5_smart_dca",
        "qt5_capitulation",
        "qt5_weekly_trend",
        "qt5_basis_carry",
        "qt5_wick_catcher",
    ],
)
def test_live_workflows_match_the_exact_gallery_port_and_emit_manual_proposals(
    workflow_id: str,
) -> None:
    ohlcv = _ohlcv(24 * 240)
    data: dict[str, object] = {"ohlcv": ohlcv}
    if workflow_id == "qt5_basis_carry":
        data["funding"] = pd.DataFrame({"funding_rate": 0.0001}, index=ohlcv.index[-48:])
    if workflow_id == "qt5_wick_catcher":
        data = {"ohlcv": ohlcv.tail(2)}
    at = cast(datetime, ohlcv.index[-1].to_pydatetime())
    context = _context(data, at)
    port = create_gallery_port(workflow_id)
    assert isinstance(port, GalleryLivePort)
    expected = port.evaluate(context).evaluation

    payload = run_legacy_workflow(
        workflow_id,
        data=data,
        decision_at=at,
        available_at=at,
        input_versions=_versions(data, at),
    )
    evaluation = payload["evaluation"]
    assert isinstance(evaluation, dict)
    assert evaluation["metrics"] == expected.metrics
    assert evaluation["notes"] == expected.notes
    expected_opportunity = expected.opportunity
    actual_opportunity = evaluation["opportunity"]
    assert (actual_opportunity is None) == (expected_opportunity is None)
    if expected_opportunity is not None:
        assert isinstance(actual_opportunity, dict)
        assert actual_opportunity["action"] == expected_opportunity.action
        assert actual_opportunity["reason"] == expected_opportunity.reason
        assert actual_opportunity["details"] == expected_opportunity.details
        proposal = payload["proposal"]
        assert isinstance(proposal, dict)
        assert proposal["execution"] == "manual_only"
        assert proposal["details"] == expected_opportunity.details
    else:
        assert payload["proposal"] is None
    assert json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize(
    "workflow_id",
    [
        "qt5_sim_smart_dca",
        "qt5_sim_weekly_trend",
        "qt5_sim_basis_carry",
        "qt5_sim_wick_catcher",
    ],
)
def test_replays_match_exact_gallery_results_and_are_json_safe(workflow_id: str) -> None:
    ohlcv = _ohlcv(24 * 90)
    data: dict[str, object] = {"ohlcv": ohlcv}
    if workflow_id == "qt5_sim_basis_carry":
        data["funding"] = pd.Series(0.0001, index=ohlcv.index, name="funding")
    at = cast(datetime, ohlcv.index[-1].to_pydatetime())
    context = _context(data, at)
    port = create_gallery_port(workflow_id)
    assert isinstance(port, GallerySimulationPort)
    expected = port.run(context).result

    payload = run_legacy_workflow(
        workflow_id,
        data=data,
        decision_at=at,
        available_at=at,
        input_versions=_versions(data, at),
    )
    replay = payload["replay"]
    assert isinstance(replay, dict)
    assert len(replay["equity"]) == len(expected.equity)
    assert replay["equity"][-1]["value"] == pytest.approx(float(expected.equity.iloc[-1]))
    assert len(replay["target_weight"]) == len(expected.target_weight)
    assert len(replay["short_weight"]) == len(expected.short_weight)
    assert replay["trades"] == _records(expected.trades)
    assert replay["diagnostics"] == _records(expected.diagnostics)
    assert payload["explanation"] == {
        "native_hook": GALLERY_PORTS[workflow_id].native_hook,
        "execution": "research_replay_only",
    }
    assert json.dumps(payload, allow_nan=False)


def test_catalog_covers_all_source_interfaces_and_declares_input_contracts() -> None:
    catalog = legacy_workflow_catalog()
    assert {item["id"] for item in catalog} == set(GALLERY_PORTS)
    assert {item["interface"] for item in catalog} == {
        "live_evaluation_result",
        "batch_strategy_result",
    }
    wick = legacy_workflow_definition("qt5_wick_catcher")
    schema = wick["input_schema"]
    assert isinstance(schema, dict)
    assert schema["execution_mode"] == {
        "const": "research",
        "description": "live/paper mutation is rejected",
    }
    parameters = schema["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["rung_quote"]["default"] == 100.0
    simulator = legacy_workflow_definition("qt5_sim_basis_carry")
    simulator_schema = simulator["input_schema"]
    assert isinstance(simulator_schema, dict)
    simulator_parameters = simulator_schema["parameters"]
    assert isinstance(simulator_parameters, dict)
    assert "initial_cash" in simulator_parameters
    assert wick["native_hook"] == GALLERY_PORTS["qt5_wick_catcher"].native_hook


def test_live_mutation_is_rejected_and_inputs_are_causally_clipped() -> None:
    ohlcv = _ohlcv(3)
    at = cast(datetime, ohlcv.index[-2].to_pydatetime())
    with pytest.raises(LegacyWorkflowSafetyError, match="historical research only"):
        run_legacy_workflow(
            "qt5_wick_catcher",
            data={"ohlcv": ohlcv},
            decision_at=at,
            available_at=at,
            input_versions=_versions({"ohlcv": ohlcv}, at),
            execution_mode="live",
        )

    historical = _ohlcv(3)
    payload = run_legacy_workflow(
        "qt5_wick_catcher",
        data={"ohlcv": historical},
        decision_at=at,
        available_at=at,
        input_versions=_versions({"ohlcv": historical}, at),
    )
    historical.loc[historical.index[-1], "low"] = 0.0
    repeated = run_legacy_workflow(
        "qt5_wick_catcher",
        data={"ohlcv": _ohlcv(3)},
        decision_at=at,
        available_at=at,
        input_versions=_versions({"ohlcv": _ohlcv(3)}, at),
    )
    payload_evaluation = payload["evaluation"]
    repeated_evaluation = repeated["evaluation"]
    assert isinstance(payload_evaluation, dict)
    assert isinstance(repeated_evaluation, dict)
    assert payload_evaluation["metrics"] == repeated_evaluation["metrics"]
    assert payload_evaluation["opportunity"] == repeated_evaluation["opportunity"]
    assert payload["proposal"] == repeated["proposal"]


def test_missing_versions_and_required_replay_inputs_fail_precisely() -> None:
    ohlcv = _ohlcv(48)
    at = cast(datetime, ohlcv.index[-1].to_pydatetime())
    with pytest.raises(Exception, match="unversioned supplied data"):
        run_legacy_workflow(
            "qt5_wick_catcher",
            data={"ohlcv": ohlcv},
            decision_at=at,
            available_at=at,
            input_versions=(),
        )
    with pytest.raises(Exception, match="requires funding"):
        run_legacy_workflow(
            "qt5_sim_basis_carry",
            data={"ohlcv": ohlcv},
            decision_at=at,
            available_at=at,
            input_versions=_versions({"ohlcv": ohlcv}, at),
        )


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    copied = frame.copy(deep=True).reset_index()
    return [
        {str(key): _value(value) for key, value in item.items()}
        for item in copied.to_dict(orient="records")
    ]


def _value(value: object) -> object:
    if isinstance(value, pd.Timestamp):
        return value.tz_convert("UTC").isoformat().replace("+00:00", "Z")
    if isinstance(value, float) and pd.isna(value):
        return None
    return value
