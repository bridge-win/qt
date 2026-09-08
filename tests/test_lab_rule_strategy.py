from __future__ import annotations

import pandas as pd
import pytest

from qt.research.strategies import (
    _bind_lab_parameters,
    _lab_rule_diagnostic,
    _lab_rule_matches,
    _lab_warmup,
)


def test_unknown_rule_state_never_becomes_true_through_not_or_sustained() -> None:
    frame = pd.DataFrame({"close": [100.0], "high": [101.0], "low": [99.0]})
    missing_sma = {
        "kind": "comparison",
        "left": {"indicator": "sma", "timeframe": "current", "parameters": {"window": 14}},
        "comparator": "<",
        "right": 30,
    }
    assert _lab_rule_matches(frame, {"kind": "not", "children": [missing_sma]}) is None
    assert _lab_rule_matches(
        frame, {"kind": "sustained_for", "bars": 2, "child": missing_sma}
    ) is None


def test_lab_rule_rejects_unavailable_multitimeframe_instead_of_reusing_current() -> None:
    frame = pd.DataFrame({"close": [100.0, 101.0], "high": [101.0, 102.0], "low": [99.0, 100.0]})
    with pytest.raises(ValueError, match="multi-timeframe"):
        _lab_rule_matches(
            frame,
            {
                "kind": "comparison",
                "left": {"indicator": "sma", "timeframe": "4h", "parameters": {"window": 2}},
                "comparator": ">",
                "right": 0,
            },
        )


def test_lab_rule_diagnostic_records_only_completed_bar_operands() -> None:
    frame = pd.DataFrame(
        {"close": [10.0, 11.0, 12.0]}, index=pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC")
    )
    diagnostic = _lab_rule_diagnostic(
        frame,
        {
            "kind": "comparison",
            "left": {"indicator": "sma", "parameters": {"window": 2}},
            "comparator": ">",
            "right": 11,
        },
    )
    assert diagnostic["outcome"] is True
    assert diagnostic["left"] == {
        "kind": "indicator",
        "indicator": "sma",
        "timeframe": "current",
        "parameters": {"window": 2},
        "value": 11.5,
    }
    assert diagnostic["right"] == {"kind": "literal", "value": 11.0}


def test_lab_parameter_overrides_require_explicit_rule_references_and_drive_warmup() -> None:
    content = {
        "mode": "rules",
        "parameters": [{"name": "period", "value": 14}],
        "entry_rule": {
            "kind": "comparison",
            "left": {
                "indicator": "sma",
                "timeframe": "current",
                "parameters": {"window": "${period}"},
            },
            "comparator": ">",
            "right": 0,
        },
    }
    bound = _bind_lab_parameters(content, {"period": 20})
    assert bound["entry_rule"]["left"]["parameters"]["window"] == 20
    assert _lab_warmup(bound) == 21
    with pytest.raises(ValueError, match="unknown lab parameter"):
        _bind_lab_parameters(content, {"period": 14, "unused": 3})
