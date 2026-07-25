from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.engine.models import (
    BacktestSpec,
    InstrumentKind,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import (
    Strategy,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.strategies.loader import load_strategy
from pydantic import ValidationError

UTC = timezone.utc


def snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        cash=Decimal("10000"),
        equity=Decimal("10000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        positions=(
            Position(instrument=InstrumentKind.SPOT),
            Position(instrument=InstrumentKind.PERPETUAL),
        ),
    )


def spec() -> BacktestSpec:
    return BacktestSpec(
        strategy="custom_sma",
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 3, tzinfo=UTC),
        ),
    )


def bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 11.0],
        },
        index=pd.date_range("2024-01-01", periods=2, freq="1D", tz="UTC"),
    )


def test_strategy_context_copies_history_and_auxiliary_mapping() -> None:
    original = bars()
    auxiliary = {"funding": original}
    context = StrategyContext(
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        bars=original,
        auxiliary=auxiliary,
        portfolio=snapshot(),
        open_orders=(),
        signals=(),
        parameters={"window": 20},
    )
    original.iloc[0, 0] = 999
    auxiliary.clear()

    assert context.bars.iloc[0, 0] == 100
    assert set(context.auxiliary) == {"funding"}
    with pytest.raises(TypeError):
        cast(dict[str, pd.DataFrame], context.auxiliary)["other"] = bars()
    with pytest.raises(TypeError):
        cast(dict[str, object], context.parameters)["window"] = 5


def test_strategy_context_rejects_future_primary_or_auxiliary_rows() -> None:
    with pytest.raises(ValidationError, match="future"):
        StrategyContext(
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            bars=bars(),
            auxiliary={},
            portfolio=snapshot(),
        )
    with pytest.raises(ValidationError, match="future"):
        StrategyContext(
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            bars=bars().iloc[:1],
            auxiliary={"future": bars()},
            portfolio=snapshot(),
        )


def test_strategy_metadata_rejects_unsupported_api_or_duplicate_capabilities() -> None:
    with pytest.raises(ValidationError, match="api_version"):
        StrategyMetadata.model_validate(
            {
                "id": "bad",
                "version": "1",
                "api_version": "2",
                "description": "bad",
                "warmup_bars": 0,
                "supported_timeframes": ("1d",),
            }
        )
    with pytest.raises(ValidationError, match="unique"):
        StrategyMetadata(
            id="bad",
            version="1",
            api_version="1",
            description="bad",
            warmup_bars=0,
            supported_timeframes=("1d", "1d"),
        )


def test_example_satisfies_runtime_strategy_contract() -> None:
    reference = (
        Path(__file__).parents[2]
        / "examples"
        / "custom_strategy.py"
    ).as_posix() + ":CustomStrategy"
    strategy = load_strategy(reference)

    assert isinstance(strategy, Strategy)
    assert strategy.metadata.warmup_bars >= 0
    assert strategy.metadata.supported_timeframes
    assert strategy.metadata.api_version == "1"
