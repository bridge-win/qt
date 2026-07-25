from __future__ import annotations

import math

import pandas as pd
from btc_backtest.reporting.metrics import compute_metrics
from hypothesis import given, settings
from hypothesis import strategies as st

from .test_metrics import result_with_equity


@given(
    st.lists(
        st.floats(min_value=1.0, max_value=1_000_000.0),
        min_size=1,
        max_size=40,
    )
)
@settings(max_examples=50)
def test_metrics_never_emit_nan_or_infinity(
    values: list[float],
) -> None:
    equity = pd.Series(
        values,
        index=pd.date_range("2024-01-01", periods=len(values), freq="1D", tz="UTC"),
    )

    metrics = compute_metrics(result_with_equity(equity))
    payload = metrics.model_dump(mode="json")

    assert all(
        math.isfinite(value)
        for value in payload.values()
        if isinstance(value, float)
    )
