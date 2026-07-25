from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.engine.models import (
    InstrumentKind,
    OrderIntent,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.accumulation import (
    FixedDCA,
    FixedDCAParams,
    SmartDCA,
    SmartDCAParams,
)
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.registry import default_strategy_registry
from pydantic import ValidationError

UTC = timezone.utc


def _context(
    timestamp: datetime,
    closes: list[float],
    *,
    cash: str = "1000",
    fear_greed: float | None = None,
    valuation_zscore: float | None = None,
) -> StrategyContext:
    start = timestamp - timedelta(days=len(closes) - 1)
    index = pd.date_range(start, periods=len(closes), freq="1D", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [10.0] * len(closes),
        },
        index=index,
    )
    feature_values: dict[str, list[float]] = {}
    if fear_greed is not None:
        feature_values["fear_greed"] = [fear_greed]
    if valuation_zscore is not None:
        feature_values["valuation_zscore"] = [valuation_zscore]
    auxiliary = (
        {
            "features": pd.DataFrame(
                feature_values,
                index=pd.DatetimeIndex([timestamp]),
            )
        }
        if feature_values
        else {}
    )
    return StrategyContext(
        timestamp=timestamp,
        bars=frame,
        auxiliary=auxiliary,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal(cash),
            equity=Decimal(cash),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(instrument=InstrumentKind.SPOT),
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


def _run_daily(
    strategy: FixedDCA | SmartDCA,
    *,
    days: int,
) -> list[OrderIntent]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    intents: list[OrderIntent] = []
    for offset in range(days):
        intents.extend(
            strategy.on_bar(
                _context(
                    start + timedelta(days=offset),
                    [100.0],
                )
            )
        )
    return intents


def test_fixed_dca_buys_once_per_utc_week() -> None:
    strategy = FixedDCA({"quote_amount": 100, "weekday": 0})

    intents = _run_daily(strategy, days=14)

    assert [intent.quote_amount for intent in intents] == [
        Decimal("100"),
        Decimal("100"),
    ]
    assert all(intent.reason == "fixed_dca_schedule" for intent in intents)


def test_fixed_dca_does_not_retry_same_bucket_or_overspend() -> None:
    strategy = FixedDCA(
        {"quote_amount": 100, "weekday": 0, "hour": 0}
    )
    monday = datetime(2024, 1, 1, tzinfo=UTC)

    first = strategy.on_bar(_context(monday, [100.0], cash="50"))
    second = strategy.on_bar(
        _context(monday + timedelta(hours=1), [100.0], cash="1000")
    )

    assert first == ()
    assert second == ()


def test_smart_dca_scales_oversold_and_fear_without_exceeding_cap() -> None:
    strategy = SmartDCA(
        {
            "base_quote": 100,
            "max_multiplier": 2,
            "rsi_window": 3,
            "rsi_oversold": 30,
            "fear_greed_threshold": 25,
            "weekday": 0,
        }
    )
    context = _context(
        datetime(2024, 1, 8, tzinfo=UTC),
        [10.0, 9.0, 8.0, 7.0],
        fear_greed=15,
    )

    intent = strategy.on_bar(context)[0]

    assert intent.quote_amount == Decimal("200")
    assert intent.reason == "smart_dca_scaled"


def test_smart_dca_degrades_to_base_when_optional_features_are_absent() -> None:
    strategy = SmartDCA(
        {
            "base_quote": 100,
            "rsi_window": 3,
            "rsi_oversold": 30,
            "weekday": 0,
        }
    )
    context = _context(
        datetime(2024, 1, 8, tzinfo=UTC),
        [10.0, 10.0],
    )

    intent = strategy.on_bar(context)[0]

    assert intent.quote_amount == Decimal("100")
    assert intent.reason == "smart_dca_base"


def test_smart_dca_can_use_point_in_time_valuation_feature() -> None:
    strategy = SmartDCA(
        {
            "base_quote": 100,
            "max_multiplier": 2,
            "rsi_window": 3,
            "valuation_entry_z": -1,
            "weekday": 0,
        }
    )
    context = _context(
        datetime(2024, 1, 8, tzinfo=UTC),
        [10.0, 10.0],
        valuation_zscore=-2,
    )

    intent = strategy.on_bar(context)[0]

    assert intent.quote_amount == Decimal("150.0")


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (FixedDCAParams, {"quote_amount": 0}),
        (FixedDCAParams, {"weekday": 7}),
        (
            SmartDCAParams,
            {"min_multiplier": 2, "max_multiplier": 1},
        ),
        (
            SmartDCAParams,
            {"rsi_oversold": 80, "rsi_overbought": 70},
        ),
    ],
)
def test_accumulation_parameters_are_bounded(
    model: type[FixedDCAParams] | type[SmartDCAParams],
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_accumulation_strategies_are_registered() -> None:
    registry = default_strategy_registry()

    assert isinstance(registry.create("fixed_dca", {}), FixedDCA)
    assert isinstance(registry.create("smart_dca", {}), SmartDCA)
    assert registry.list()[:2] == ("fixed_dca", "smart_dca")
