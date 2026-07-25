from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.engine.models import (
    BacktestSpec,
    OrderIntent,
    OrderSide,
    OrderType,
)
from pydantic import ValidationError

UTC = timezone.utc


def data_request() -> DataRequest:
    return DataRequest(
        provider="bitstamp",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 2, tzinfo=UTC),
    )


def test_order_intent_requires_exactly_one_positive_size() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        OrderIntent(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            reason="invalid",
        )
    with pytest.raises(ValidationError, match="positive"):
        OrderIntent(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            base_quantity=Decimal("0"),
            reason="invalid",
        )


def test_order_intent_requires_prices_for_conditional_types() -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        OrderIntent(
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            base_quantity=Decimal("1"),
            reason="invalid",
        )
    with pytest.raises(ValidationError, match="stop_price"):
        OrderIntent(
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            base_quantity=Decimal("1"),
            reason="invalid",
        )


def test_atomic_intent_requires_group_id() -> None:
    with pytest.raises(ValidationError, match="group_id"):
        OrderIntent(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            base_quantity=Decimal("1"),
            atomic_group=True,
            reason="invalid",
        )


def test_backtest_spec_copies_and_freezes_strategy_parameters() -> None:
    parameters: dict[str, object] = {"window": 20}
    spec = BacktestSpec(
        strategy="sma_crossover",
        strategy_params=parameters,
        data=data_request(),
    )
    parameters["window"] = 99

    assert spec.strategy_params == {"window": 20}
    assert spec.model_dump(mode="json")["strategy_params"] == {"window": 20}
    with pytest.raises(TypeError):
        cast(dict[str, object], spec.strategy_params)["window"] = 5
    with pytest.raises(ValidationError):
        spec.seed = 8


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_cash", Decimal("0")),
        ("fee_bps", Decimal("-1")),
        ("slippage_bps", Decimal("NaN")),
    ],
)
def test_backtest_spec_rejects_invalid_costs(
    field: str,
    value: Decimal,
) -> None:
    values: dict[str, object] = {
        "strategy": "sma_crossover",
        "data": data_request(),
        field: value,
    }

    with pytest.raises(ValidationError):
        BacktestSpec.model_validate(values)
