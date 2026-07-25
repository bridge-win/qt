"""Finite performance, risk, benchmark, and cost metrics."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from btc_backtest.engine.models import BacktestResult

_SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


class PerformanceMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_return: float
    cagr: float
    annualized_volatility: float
    sharpe: float
    sortino: float
    calmar: float
    omega: float
    max_drawdown: float
    average_drawdown: float
    max_drawdown_bars: int = Field(ge=0)
    exposure: float = Field(ge=0, le=1)
    turnover: float = Field(ge=0)
    total_fees: Decimal
    total_slippage: Decimal
    total_funding: Decimal
    trade_count: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=1)
    profit_factor: float = Field(ge=0)
    expectancy: float
    average_holding_period_bars: float = Field(ge=0)
    var_95: float
    cvar_95: float
    periods_per_year: float = Field(ge=0)
    monthly_returns: Mapping[str, float] = Field(default_factory=dict)
    yearly_returns: Mapping[str, float] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @field_validator(
        "total_return",
        "cagr",
        "annualized_volatility",
        "sharpe",
        "sortino",
        "calmar",
        "omega",
        "max_drawdown",
        "average_drawdown",
        "exposure",
        "turnover",
        "win_rate",
        "profit_factor",
        "expectancy",
        "average_holding_period_bars",
        "var_95",
        "cvar_95",
        "periods_per_year",
    )
    @classmethod
    def require_finite_float(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metrics must be finite")
        return value

    @field_validator("total_fees", "total_slippage", "total_funding")
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("cost metrics must be finite")
        return value

    @field_validator("monthly_returns", "yearly_returns")
    @classmethod
    def freeze_return_tables(
        cls,
        value: Mapping[str, float],
    ) -> Mapping[str, float]:
        copied = dict(value)
        if any(not math.isfinite(item) for item in copied.values()):
            raise ValueError("return table values must be finite")
        return MappingProxyType(copied)

    @field_serializer("monthly_returns", "yearly_returns")
    def serialize_return_tables(
        self,
        value: Mapping[str, float],
    ) -> dict[str, float]:
        return dict(value)


class BenchmarkComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    excess_vs_buy_hold: float
    excess_vs_fixed_dca: float
    drawdown_delta_vs_buy_hold: float
    drawdown_delta_vs_fixed_dca: float
    sharpe_delta_vs_buy_hold: float
    sharpe_delta_vs_fixed_dca: float
    turnover_delta_vs_buy_hold: float
    turnover_delta_vs_fixed_dca: float
    incremental_fees_vs_buy_hold: Decimal
    incremental_fees_vs_fixed_dca: Decimal

    @field_validator(
        "excess_vs_buy_hold",
        "excess_vs_fixed_dca",
        "drawdown_delta_vs_buy_hold",
        "drawdown_delta_vs_fixed_dca",
        "sharpe_delta_vs_buy_hold",
        "sharpe_delta_vs_fixed_dca",
        "turnover_delta_vs_buy_hold",
        "turnover_delta_vs_fixed_dca",
    )
    @classmethod
    def require_finite_float(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("benchmark deltas must be finite")
        return value

    @field_validator(
        "incremental_fees_vs_buy_hold",
        "incremental_fees_vs_fixed_dca",
    )
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("benchmark cost deltas must be finite")
        return value


def periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 0.0
    if index.tz is None:
        raise ValueError("metrics index must be timezone-aware")
    normalized = index.tz_convert(timezone.utc)
    elapsed = (
        normalized[-1].to_pydatetime()
        - normalized[0].to_pydatetime()
    ).total_seconds()
    if elapsed <= 0:
        return 0.0
    average_period_seconds = elapsed / (len(index) - 1)
    if average_period_seconds <= 0:
        return 0.0
    return float(_SECONDS_PER_YEAR / average_period_seconds)


def compute_metrics(result: BacktestResult) -> PerformanceMetrics:
    equity = _equity_series(result)
    warnings: list[str] = []
    if equity.empty:
        warnings.append("result has no equity snapshots")
        return _empty_metrics(warnings)

    returns = equity.pct_change().dropna()
    returns = returns[returns.map(math.isfinite)]
    annual_periods = periods_per_year(equity.index)
    total_return = _total_return(equity)
    cagr = _cagr(equity)
    volatility = _annualized_volatility(returns, annual_periods)
    sharpe = _ratio(
        returns.mean() * annual_periods,
        volatility,
    )
    downside = returns[returns < 0]
    sortino = _ratio(
        returns.mean() * annual_periods,
        _annualized_volatility(downside, annual_periods),
    )
    drawdown = _drawdown(equity)
    max_drawdown = _safe_float(drawdown.min()) if not drawdown.empty else 0.0
    average_drawdown = (
        _safe_float(drawdown[drawdown < 0].mean())
        if (drawdown < 0).any()
        else 0.0
    )
    calmar = _ratio(cagr, abs(max_drawdown))
    omega = _omega(returns)
    total_fees = sum(
        (fill.fee for fill in result.fills),
        start=Decimal("0"),
    )
    final_equity = Decimal(str(equity.iloc[-1]))
    turnover = _turnover(result, final_equity)
    trade_returns = tuple(
        _safe_float(trade.realized_pnl)
        for trade in result.trades
    )

    return PerformanceMetrics(
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        omega=omega,
        max_drawdown=max_drawdown,
        average_drawdown=average_drawdown,
        max_drawdown_bars=_max_drawdown_bars(drawdown),
        exposure=_exposure(result),
        turnover=turnover,
        total_fees=total_fees,
        total_slippage=_diagnostic_decimal_sum(
            result.diagnostics,
            "slippage_costs",
            "total_slippage",
        ),
        total_funding=_diagnostic_decimal_sum(
            result.diagnostics,
            "funding_costs",
            "total_funding",
        ),
        trade_count=len(result.trades),
        win_rate=_win_rate(trade_returns),
        profit_factor=_profit_factor(trade_returns),
        expectancy=_expectancy(trade_returns),
        average_holding_period_bars=0.0,
        var_95=_var(returns),
        cvar_95=_cvar(returns),
        periods_per_year=annual_periods,
        monthly_returns=_periodic_returns(equity, "ME"),
        yearly_returns=_periodic_returns(equity, "YE"),
        warnings=tuple(warnings),
    )


def compare_benchmarks(
    primary: PerformanceMetrics,
    buy_hold: PerformanceMetrics,
    fixed_dca: PerformanceMetrics,
) -> BenchmarkComparison:
    return BenchmarkComparison(
        excess_vs_buy_hold=primary.total_return - buy_hold.total_return,
        excess_vs_fixed_dca=primary.total_return - fixed_dca.total_return,
        drawdown_delta_vs_buy_hold=(
            primary.max_drawdown - buy_hold.max_drawdown
        ),
        drawdown_delta_vs_fixed_dca=(
            primary.max_drawdown - fixed_dca.max_drawdown
        ),
        sharpe_delta_vs_buy_hold=primary.sharpe - buy_hold.sharpe,
        sharpe_delta_vs_fixed_dca=primary.sharpe - fixed_dca.sharpe,
        turnover_delta_vs_buy_hold=primary.turnover - buy_hold.turnover,
        turnover_delta_vs_fixed_dca=primary.turnover - fixed_dca.turnover,
        incremental_fees_vs_buy_hold=(
            primary.total_fees - buy_hold.total_fees
        ),
        incremental_fees_vs_fixed_dca=(
            primary.total_fees - fixed_dca.total_fees
        ),
    )


def _equity_series(result: BacktestResult) -> pd.Series:
    values = [
        (pd.Timestamp(snapshot.timestamp), float(snapshot.equity))
        for snapshot in result.snapshots
    ]
    if not values:
        return pd.Series(dtype="float64")
    index = pd.DatetimeIndex([item[0] for item in values])
    index = (
        index.tz_localize(timezone.utc)
        if index.tz is None
        else index.tz_convert(timezone.utc)
    )
    return pd.Series([item[1] for item in values], index=index)


def _empty_metrics(warnings: list[str]) -> PerformanceMetrics:
    return PerformanceMetrics(
        total_return=0.0,
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
        total_fees=Decimal("0"),
        total_slippage=Decimal("0"),
        total_funding=Decimal("0"),
        trade_count=0,
        win_rate=0.0,
        profit_factor=0.0,
        expectancy=0.0,
        average_holding_period_bars=0.0,
        var_95=0.0,
        cvar_95=0.0,
        periods_per_year=0.0,
        monthly_returns={},
        yearly_returns={},
        warnings=tuple(warnings),
    )


def _total_return(equity: pd.Series) -> float:
    first = _safe_float(equity.iloc[0])
    last = _safe_float(equity.iloc[-1])
    if first <= 0:
        return 0.0
    return _finite((last / first) - 1.0)


def _cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    first = _safe_float(equity.iloc[0])
    last = _safe_float(equity.iloc[-1])
    elapsed = (
        equity.index[-1].to_pydatetime()
        - equity.index[0].to_pydatetime()
    ).total_seconds()
    if first <= 0 or last <= 0 or elapsed <= 0:
        return 0.0
    years = elapsed / _SECONDS_PER_YEAR
    try:
        return _finite((last / first) ** (1.0 / years) - 1.0)
    except OverflowError:
        return 0.0


def _annualized_volatility(
    returns: pd.Series,
    annual_periods: float,
) -> float:
    if len(returns) < 2 or annual_periods <= 0:
        return 0.0
    return _finite(float(returns.std(ddof=1)) * math.sqrt(annual_periods))


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return _finite(numerator / denominator)


def _drawdown(equity: pd.Series) -> pd.Series:
    running_max = equity.cummax()
    safe = running_max.where(running_max > 0)
    drawdown = (equity / safe) - 1.0
    return drawdown.fillna(0.0)


def _max_drawdown_bars(drawdown: pd.Series) -> int:
    longest = 0
    current = 0
    for value in drawdown:
        if _safe_float(value) < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _omega(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    gains = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum())
    return _ratio(_safe_float(gains), _safe_float(losses))


def _exposure(result: BacktestResult) -> float:
    if not result.snapshots:
        return 0.0
    exposed = sum(
        1
        for snapshot in result.snapshots
        if any(position.quantity != 0 for position in snapshot.positions)
    )
    return exposed / len(result.snapshots)


def _turnover(result: BacktestResult, final_equity: Decimal) -> float:
    if final_equity <= 0:
        return 0.0
    notional = sum(
        (fill.quantity * fill.price for fill in result.fills),
        start=Decimal("0"),
    )
    return _safe_float(notional / final_equity)


def _diagnostic_decimal_sum(
    diagnostics: Mapping[str, object],
    sequence_key: str,
    scalar_key: str,
) -> Decimal:
    if scalar_key in diagnostics:
        return _decimal(diagnostics[scalar_key])
    raw = diagnostics.get(sequence_key, ())
    if isinstance(raw, (str, bytes)) or not isinstance(raw, tuple | list):
        return Decimal("0")
    return sum((_decimal(item) for item in raw), start=Decimal("0"))


def _win_rate(values: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if value > 0) / len(values)


def _profit_factor(values: tuple[float, ...]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    return _ratio(gains, losses)


def _expectancy(values: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    return _finite(sum(values) / len(values))


def _var(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return _safe_float(returns.quantile(0.05))


def _cvar(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    threshold = returns.quantile(0.05)
    tail = returns[returns <= threshold]
    if tail.empty:
        return 0.0
    return _safe_float(tail.mean())


def _periodic_returns(
    equity: pd.Series,
    frequency: str,
) -> Mapping[str, float]:
    if equity.empty:
        return MappingProxyType({})
    grouped = equity.resample(frequency)
    values: dict[str, float] = {}
    for timestamp, group in grouped:
        if group.empty:
            continue
        first = _safe_float(group.iloc[0])
        if first <= 0:
            values[str(timestamp.date())] = 0.0
            continue
        values[str(timestamp.date())] = _finite(
            (_safe_float(group.iloc[-1]) / first) - 1.0
        )
    return MappingProxyType(values)


def _decimal(value: object) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("diagnostic cost values must be decimal") from error
    if not parsed.is_finite():
        raise ValueError("diagnostic cost values must be finite")
    return parsed


def _safe_float(value: Decimal | float | int | str) -> float:
    return _finite(float(value))


def _finite(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return value
