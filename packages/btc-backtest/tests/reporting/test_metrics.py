from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.data.models import DataManifest
from btc_backtest.engine.models import (
    BacktestResult,
    Fill,
    InstrumentKind,
    OrderSide,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.reporting.metrics import (
    PerformanceMetrics,
    compare_benchmarks,
    compute_metrics,
)

UTC = timezone.utc


def result_with_equity(
    equity: pd.Series,
    *,
    fills: tuple[Fill, ...] = (),
    diagnostics: dict[str, object] | None = None,
) -> BacktestResult:
    snapshots = tuple(
        PortfolioSnapshot(
            timestamp=timestamp.to_pydatetime(),
            cash=Decimal(str(value)),
            equity=Decimal(str(value)),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(Position(instrument=InstrumentKind.SPOT),),
        )
        for timestamp, value in equity.items()
    )
    return BacktestResult(
        run_id="run",
        strategy_id="strategy",
        data_manifests=(
            DataManifest(
                provider="fixture",
                market="spot",
                symbol="BTC/USD",
                timeframe="1d",
                requested_start=snapshots[0].timestamp,
                requested_end=snapshots[-1].timestamp + pd.Timedelta(days=1),
                delivered_start=snapshots[0].timestamp,
                delivered_end=snapshots[-1].timestamp + pd.Timedelta(days=1),
                retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
                real_data=False,
                raw_sha256=("0" * 64,),
                normalized_sha256="1" * 64,
            ),
        ),
        orders=(),
        fills=fills,
        positions=snapshots[-1].positions,
        snapshots=snapshots,
        trades=(),
        diagnostics=diagnostics or {},
    )


def fill(
    timestamp: datetime,
    *,
    fee: str,
) -> Fill:
    return Fill(
        id=f"fill:{timestamp.isoformat()}",
        order_id="order",
        order_created_at=timestamp,
        timestamp=timestamp,
        instrument=InstrumentKind.SPOT,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal(fee),
        reason="market",
    )


def metrics(
    *,
    total_return: float,
    total_fees: Decimal,
) -> PerformanceMetrics:
    return PerformanceMetrics(
        total_return=total_return,
        cagr=0.0,
        annualized_volatility=0.0,
        sharpe=0.0,
        sortino=0.0,
        calmar=0.0,
        omega=0.0,
        max_drawdown=0.0,
        average_drawdown=0.0,
        max_drawdown_bars=0,
        exposure=0.0,
        turnover=0.0,
        total_fees=total_fees,
        total_slippage=Decimal("0"),
        total_funding=Decimal("0"),
        trade_count=0,
        win_rate=0.0,
        profit_factor=0.0,
        expectancy=0.0,
        average_holding_period_bars=0.0,
        var_95=0.0,
        cvar_95=0.0,
        periods_per_year=365.25,
        monthly_returns={},
        yearly_returns={},
        warnings=(),
    )


def test_annualization_uses_actual_daily_spacing() -> None:
    equity = pd.Series(
        [100.0, 101.0, 102.01],
        index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
    )

    result = compute_metrics(result_with_equity(equity))

    assert result.total_return == pytest.approx(0.0201)
    assert result.periods_per_year == pytest.approx(365.25)


def test_drawdown_duration_and_cost_attribution() -> None:
    index = pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")
    result = compute_metrics(
        result_with_equity(
            pd.Series([100, 120, 90, 95, 121], index=index),
            fills=(
                fill(index[1].to_pydatetime(), fee="1"),
                fill(index[2].to_pydatetime(), fee="2"),
            ),
            diagnostics={
                "slippage_costs": ("0.5", "0.25"),
                "funding_costs": ("3",),
            },
        )
    )

    assert result.max_drawdown == pytest.approx(-0.25)
    assert result.max_drawdown_bars == 2
    assert result.total_fees == Decimal("3")
    assert result.total_slippage == Decimal("0.75")
    assert result.total_funding == Decimal("3")


def test_benchmark_comparison_reports_excess_return_and_cost() -> None:
    comparison = compare_benchmarks(
        metrics(total_return=0.20, total_fees=Decimal("20")),
        metrics(total_return=0.25, total_fees=Decimal("5")),
        metrics(total_return=0.15, total_fees=Decimal("10")),
    )

    assert comparison.excess_vs_buy_hold == pytest.approx(-0.05)
    assert comparison.excess_vs_fixed_dca == pytest.approx(0.05)
    assert comparison.incremental_fees_vs_buy_hold == Decimal("15")
