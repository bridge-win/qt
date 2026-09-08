from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qt.lab.learning import list_catalog_signal_documentation, list_indicator_family_documentation
from qt.lab.persistence import LabRepository
from qt.lab.router import LabSettings, build_lab_router
from qt.lab.service import LabService
from qt.workbench.catalog import legacy_catalog
from qt.workbench.catalog_signals import add_catalog_indicators, evaluate_signal


def _frame() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=320, freq="h", tz="UTC")
    close = pd.Series(
        100 + np.linspace(0, 30, len(index)) + np.sin(np.linspace(0, 40, len(index))) * 5,
        index=index,
    )
    return pd.DataFrame(
        {
            "open": close - 0.3,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 100 + np.arange(len(index)) % 17,
        },
        index=index,
    )


def test_learning_documents_all_actual_catalog_evaluators_and_parameter_limitations() -> None:
    documents = list_catalog_signal_documentation()
    catalog_signals = legacy_catalog()["signals"]
    assert isinstance(catalog_signals, list)
    assert {document["id"] for document in documents} == {
        signal["id"] for signal in catalog_signals if isinstance(signal, Mapping)
    }
    frame = add_catalog_indicators(_frame())
    by_id = {str(document["id"]): document for document in documents}
    for signal in catalog_signals:
        assert isinstance(signal, Mapping)
        signal_id = str(signal["id"])
        document = by_id[signal_id]
        assert document["evaluator"] == signal_id
        assert isinstance(document["formula_en"], str) and document["formula_en"]
        assert isinstance(document["formula_zh"], str) and document["formula_zh"]
        assert document["source_identity"] == "qt.workbench.catalog_signals.evaluate_signal"
        assert document["source_version"]
        params = {
            str(item["name"]): item["default"]
            for item in signal["parameters"]
            if isinstance(item, Mapping)
        }
        baseline = evaluate_signal(signal_id, frame, params)
        assert baseline.dtype == bool
        for name in document["ignored_legacy_parameters"]:
            assert isinstance(name, str)
            definition = next(
                item
                for item in signal["parameters"]
                if isinstance(item, Mapping) and item.get("name") == name
            )
            assert isinstance(definition, Mapping)
            changed = dict(params)
            changed[name] = definition["maximum"]
            assert evaluate_signal(signal_id, frame, changed).equals(baseline)


def test_learning_endpoint_exposes_signal_family_and_concrete_indicator_docs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = LabService(LabRepository(tmp_path / "research.sqlite3"))
    app = FastAPI()
    app.include_router(
        build_lab_router(
            LabSettings(state_root=tmp_path),
            service=service,
        ),
        prefix="/api/v3",
    )
    response = TestClient(app).get("/api/v3/learning-docs")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) > 60
    rsi = next(item for item in items if item["id"] == "rsi_oversold")
    assert rsi["ignored_legacy_parameters"] == ["period"]
    assert "TA-Lib" in rsi["caveats"][0]
    derivatives = next(item for item in items if item["id"] == "family:derivatives")
    assert derivatives["availability_basis"] == "provider_recorded_available_at"
    assert derivatives["may_be_revised"] is True
    onchain = next(item for item in items if item["id"] == "onchain.mvrv_z_from_caps")
    assert onchain["source"] == "qt.indicators.onchain.mvrv_z_from_caps"
    assert onchain["requires_provider_available_at"] is True
    assert len(list_indicator_family_documentation()) == 10
