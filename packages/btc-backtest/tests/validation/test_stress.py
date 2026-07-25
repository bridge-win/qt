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
from btc_backtest.validation.stress import (
    CostStress,
    ProviderOutageStress,
    StressRunner,
)

UTC = timezone.utc


class CostSensitiveRunner:
    def __init__(self) -> None:
        self.calls: list[BacktestSpec] = []

    def run(self, spec: BacktestSpec) -> BacktestResult:
        self.calls.append(spec)
        cost_drag = (spec.fee_bps + spec.slippage_bps) / Decimal("10000")
        return result_for(spec, final_equity=Decimal("100") * (Decimal("1") - cost_drag))


def test_cost_stress_cannot_improve_equity() -> None:
    evaluation = StressRunner(CostSensitiveRunner()).run(
        backtest_spec(fee_bps=Decimal("10"), slippage_bps=Decimal("5")),
        (CostStress(fee_multiplier=Decimal("2"), slippage_multiplier=Decimal("3")),),
    )

    assert evaluation.scenario_metrics[0].final_equity <= evaluation.base_metrics.final_equity
    assert evaluation.scenario_metrics[0].scenario_id == "cost"


def test_provider_outage_records_declared_interval() -> None:
    start = datetime(2024, 1, 2, tzinfo=UTC)
    end = datetime(2024, 1, 3, tzinfo=UTC)

    evaluation = StressRunner(CostSensitiveRunner()).run(
        backtest_spec(),
        (ProviderOutageStress(provider="fixture", start=start, end=end),),
    )

    assert evaluation.scenarios[0].id == "provider_outage"
    assert evaluation.scenarios[0].parameters["provider"] == "fixture"
    assert evaluation.scenarios[0].parameters["start"] == start.isoformat()
    assert evaluation.scenarios[0].parameters["end"] == end.isoformat()


def backtest_spec(
    *,
    fee_bps: Decimal = Decimal("0"),
    slippage_bps: Decimal = Decimal("0"),
) -> BacktestSpec:
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
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )


def result_for(
    spec: BacktestSpec,
    *,
    final_equity: Decimal,
) -> BacktestResult:
    start = spec.data.start
    final = spec.data.end - timedelta(seconds=1)
    snapshots = (
        PortfolioSnapshot(
            timestamp=start,
            cash=Decimal("100"),
            equity=Decimal("100"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(Position(instrument=InstrumentKind.SPOT),),
        ),
        PortfolioSnapshot(
            timestamp=final,
            cash=final_equity,
            equity=final_equity,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(Position(instrument=InstrumentKind.SPOT),),
        ),
    )
    fingerprint = hashlib.sha256(
        f"{spec.data.start.isoformat()}:{spec.fee_bps}:{spec.slippage_bps}".encode()
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
