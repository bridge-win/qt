from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from btc_backtest.data.models import DataManifest, DataRequest
from btc_backtest.engine.models import (
    BacktestResult,
    BacktestSpec,
    InstrumentKind,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.validation.models import ValidationSpec
from btc_backtest.validation.splits import rolling_splits
from btc_backtest.validation.walk_forward import (
    ParameterCandidate,
    WalkForwardValidator,
)

UTC = timezone.utc


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[BacktestSpec] = []

    def run(self, spec: BacktestSpec) -> BacktestResult:
        self.calls.append(spec)
        candidate = str(spec.strategy_params["candidate"])
        score = {
            "alpha": Decimal("0.20"),
            "beta": Decimal("0.10"),
        }[candidate]
        return result_for(spec, total_return=score, max_drawdown=Decimal("0"))


def test_each_window_selects_on_train_and_scores_on_next_test() -> None:
    runner = FakeRunner()
    base = backtest_spec()
    index = pd.date_range("2024-01-01", periods=8, freq="1D", tz="UTC")
    splits = rolling_splits(index, train_bars=2, test_bars=2)

    result = WalkForwardValidator(
        runner,
        validation_spec(),
        splits=splits,
    ).run(
        base,
        (
            ParameterCandidate(parameters={"candidate": "beta"}),
            ParameterCandidate(parameters={"candidate": "alpha"}),
        ),
    )

    assert all(item.selected_on == item.split.train for item in result.windows)
    assert all(item.scored_on == item.split.test for item in result.windows)
    assert [item.selected_candidate.parameters["candidate"] for item in result.windows] == [
        "alpha",
        "alpha",
        "alpha",
    ]
    assert [
        (call.data.start, call.data.end, call.strategy_params["candidate"])
        for call in runner.calls[:2]
    ] == [
        (splits[0].train.start, splits[0].train.end, "alpha"),
        (splits[0].train.start, splits[0].train.end, "beta"),
    ]


def test_tie_breaker_uses_drawdown_turnover_then_canonical_params() -> None:
    class TieRunner(FakeRunner):
        def run(self, spec: BacktestSpec) -> BacktestResult:
            self.calls.append(spec)
            candidate = str(spec.strategy_params["candidate"])
            drawdown = {
                "alpha": Decimal("-0.20"),
                "beta": Decimal("-0.05"),
                "gamma": Decimal("-0.05"),
            }[candidate]
            return result_for(
                spec,
                total_return=Decimal("0.10"),
                max_drawdown=drawdown,
            )

    index = pd.date_range("2024-01-01", periods=4, freq="1D", tz="UTC")
    split = rolling_splits(index, train_bars=2, test_bars=2)
    result = WalkForwardValidator(
        TieRunner(),
        validation_spec(),
        splits=split,
    ).run(
        backtest_spec(),
        (
            ParameterCandidate(parameters={"candidate": "gamma"}),
            ParameterCandidate(parameters={"candidate": "beta"}),
            ParameterCandidate(parameters={"candidate": "alpha"}),
        ),
    )

    assert result.windows[0].selected_candidate.parameters["candidate"] == "beta"


def validation_spec() -> ValidationSpec:
    return ValidationSpec(
        selection_end=datetime(2024, 1, 8, tzinfo=UTC),
        final_test_start=datetime(2024, 1, 9, tzinfo=UTC),
        final_test_end=datetime(2024, 1, 11, tzinfo=UTC),
        train_bars=2,
        test_bars=2,
        objective="total_return",
    )


def backtest_spec() -> BacktestSpec:
    return BacktestSpec(
        strategy="fixture",
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 11, tzinfo=UTC),
            require_real=False,
        ),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def result_for(
    spec: BacktestSpec,
    *,
    total_return: Decimal,
    max_drawdown: Decimal,
) -> BacktestResult:
    start = spec.data.start
    end = spec.data.end
    mid = start + ((end - start) / 2)
    final = end - timedelta(seconds=1)
    equity = (
        Decimal("100"),
        Decimal("100") * (Decimal("1") + max_drawdown),
        Decimal("100") * (Decimal("1") + total_return),
    )
    snapshots = tuple(
        PortfolioSnapshot(
            timestamp=timestamp,
            cash=value,
            equity=value,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(Position(instrument=InstrumentKind.SPOT),),
        )
        for timestamp, value in zip((start, mid, final), equity, strict=True)
    )
    fingerprint = hashlib.sha256(
        f"{start.isoformat()}:{end.isoformat()}:{spec.strategy_params}".encode()
    ).hexdigest()
    return BacktestResult(
        run_id=f"run:{fingerprint}",
        strategy_id=spec.strategy,
        data_manifests=(
            DataManifest(
                provider=spec.data.provider,
                market=spec.data.market,
                symbol=spec.data.symbol,
                timeframe=spec.data.timeframe,
                requested_start=spec.data.start,
                requested_end=spec.data.end,
                delivered_start=spec.data.start,
                delivered_end=spec.data.end,
                retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
                real_data=False,
                raw_sha256=(fingerprint,),
                normalized_sha256=fingerprint,
            ),
        ),
        orders=(),
        fills=(),
        positions=snapshots[-1].positions,
        snapshots=snapshots,
        trades=(),
    )
