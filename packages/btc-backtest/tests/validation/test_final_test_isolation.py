from __future__ import annotations

from decimal import Decimal

import pandas as pd
from btc_backtest.engine.models import BacktestResult, BacktestSpec
from btc_backtest.validation.splits import rolling_splits
from btc_backtest.validation.walk_forward import (
    ParameterCandidate,
    WalkForwardValidator,
)

from .test_walk_forward import backtest_spec, result_for, validation_spec


class FinalSensitiveRunner:
    def __init__(self, *, final_alpha_return: Decimal) -> None:
        self.final_alpha_return = final_alpha_return
        self.calls: list[BacktestSpec] = []

    def run(self, spec: BacktestSpec) -> BacktestResult:
        self.calls.append(spec)
        candidate = str(spec.strategy_params["candidate"])
        final_start = validation_spec().final_test_start
        if spec.data.start == final_start and candidate == "alpha":
            return result_for(
                spec,
                total_return=self.final_alpha_return,
                max_drawdown=Decimal("0"),
            )
        score = Decimal("0.20") if candidate == "alpha" else Decimal("0.10")
        return result_for(
            spec,
            total_return=score,
            max_drawdown=Decimal("0"),
        )


def test_mutating_final_test_cannot_change_selected_parameters() -> None:
    base = backtest_spec()
    index = pd.date_range("2024-01-01", periods=8, freq="1D", tz="UTC")
    splits = rolling_splits(index, train_bars=2, test_bars=2)
    candidates = (
        ParameterCandidate(parameters={"candidate": "alpha"}),
        ParameterCandidate(parameters={"candidate": "beta"}),
    )

    first = WalkForwardValidator(
        FinalSensitiveRunner(final_alpha_return=Decimal("0.99")),
        validation_spec(),
        splits=splits,
    ).run(base, candidates)
    mutated = WalkForwardValidator(
        FinalSensitiveRunner(final_alpha_return=Decimal("-0.99")),
        validation_spec(),
        splits=splits,
    ).run(base, candidates)

    assert first.selected_parameters == mutated.selected_parameters
    assert first.final_evaluation is not None
    assert mutated.final_evaluation is not None
    assert first.final_evaluation.scored_on.start == validation_spec().final_test_start
    assert mutated.final_evaluation.scored_on.start == validation_spec().final_test_start
