from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btc_backtest.data.models import DataManifest, DataRequest
from btc_backtest.engine.models import (
    BacktestResult,
    BacktestSpec,
    InstrumentKind,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.validation.sensitivity import (
    SensitivityAnalyzer,
    multiple_testing,
)

UTC = timezone.utc


class SensitivityRunner:
    def __init__(self) -> None:
        self.calls: list[BacktestSpec] = []

    def run(self, spec: BacktestSpec) -> BacktestResult:
        self.calls.append(spec)
        fast = Decimal(str(spec.strategy_params["fast"]))
        score = Decimal("0.10") + (fast / Decimal("1000"))
        return result_for(spec, total_return=score)


def test_sensitivity_runs_canonical_parameter_grid_order() -> None:
    runner = SensitivityRunner()

    result = SensitivityAnalyzer(runner).run(
        backtest_spec(),
        {"slow": (20,), "fast": (10, 5)},
    )

    assert [item.parameters for item in result.evaluations] == [
        {"fast": 5, "slow": 20},
        {"fast": 10, "slow": 20},
    ]
    assert [call.strategy_params for call in runner.calls] == [
        {"fast": 5, "slow": 20},
        {"fast": 10, "slow": 20},
    ]
    assert result.best.parameters == {"fast": 10, "slow": 20}
    assert result.multiple_testing.attempted_variants == 2


def test_multiple_testing_reports_adjusted_significance() -> None:
    diagnostic = multiple_testing([0.01, 0.02, 0.20], method="holm")

    assert diagnostic.adjusted_p_values[0] >= diagnostic.raw_p_values[0]
    assert diagnostic.adjusted_p_values == (0.03, 0.04, 0.20)
    assert diagnostic.attempted_variants == 3


def backtest_spec() -> BacktestSpec:
    return BacktestSpec(
        strategy="fixture",
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 4, tzinfo=UTC),
            require_real=False,
        ),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


def result_for(
    spec: BacktestSpec,
    *,
    total_return: Decimal,
) -> BacktestResult:
    start = spec.data.start
    final = spec.data.end - timedelta(seconds=1)
    equity = (Decimal("100"), Decimal("100") * (Decimal("1") + total_return))
    snapshots = tuple(
        PortfolioSnapshot(
            timestamp=timestamp,
            cash=value,
            equity=value,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(Position(instrument=InstrumentKind.SPOT),),
        )
        for timestamp, value in zip((start, final), equity, strict=True)
    )
    fingerprint = hashlib.sha256(
        f"{start.isoformat()}:{spec.strategy_params}".encode()
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
