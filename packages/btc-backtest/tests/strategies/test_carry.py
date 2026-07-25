from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from btc_backtest.engine.models import (
    BacktestSpec,
    InstrumentKind,
    OrderSide,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.carry import (
    FundingBasisCarry,
    FundingBasisCarryParams,
)
from btc_backtest.strategies.registry import default_strategy_registry
from pydantic import ValidationError

UTC = timezone.utc


def _context(
    *,
    funding_rate: str | None = "0.0002",
    equity: str = "10000",
    timestamp: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    spot_close: str = "100",
    perpetual_close: str = "100",
    spot_quantity: str = "0",
    perpetual_quantity: str = "0",
    funding_timestamp: datetime | None = None,
    include_perpetual: bool = True,
) -> StrategyContext:
    spot_price = float(spot_close)
    bars = pd.DataFrame(
        {
            "open": [spot_price],
            "high": [spot_price],
            "low": [spot_price],
            "close": [spot_price],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex([timestamp]),
    )
    auxiliary: dict[str, pd.DataFrame] = {}
    if include_perpetual:
        perpetual_price = float(perpetual_close)
        auxiliary["perpetual"] = pd.DataFrame(
            {
                "open": [perpetual_price],
                "high": [perpetual_price],
                "low": [perpetual_price],
                "close": [perpetual_price],
                "volume": [20.0],
            },
            index=pd.DatetimeIndex([timestamp]),
        )
    if funding_rate is not None:
        effective_at = funding_timestamp or timestamp
        auxiliary["funding"] = pd.DataFrame(
            {"rate": [float(funding_rate)]},
            index=pd.DatetimeIndex([effective_at]),
        )
    spot_qty = Decimal(spot_quantity)
    perpetual_qty = Decimal(perpetual_quantity)
    return StrategyContext(
        timestamp=timestamp,
        bars=bars,
        auxiliary=auxiliary,
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal(equity) - spot_qty * Decimal(spot_close),
            equity=Decimal(equity),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(
                    instrument=InstrumentKind.SPOT,
                    quantity=spot_qty,
                    average_price=Decimal(spot_close) if spot_qty else Decimal("0"),
                ),
                Position(
                    instrument=InstrumentKind.PERPETUAL,
                    quantity=perpetual_qty,
                    average_price=(
                        Decimal(perpetual_close)
                        if perpetual_qty
                        else Decimal("0")
                    ),
                ),
            ),
        ),
    )


def _opened_context(
    *,
    funding_rate: str | None = "0.0002",
    timestamp: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    perpetual_close: str = "100",
    include_perpetual: bool = True,
) -> StrategyContext:
    return _context(
        funding_rate=funding_rate,
        timestamp=timestamp,
        perpetual_close=perpetual_close,
        spot_quantity="50",
        perpetual_quantity="-50",
        include_perpetual=include_perpetual,
    )


def test_carry_opens_equal_spot_long_and_perpetual_short() -> None:
    strategy = FundingBasisCarry(
        {"entry_apr": 0.10, "exit_apr": 0.03, "weight": 0.5}
    )

    intents = strategy.on_bar(_context())

    assert [(item.instrument, item.side) for item in intents] == [
        (InstrumentKind.SPOT, OrderSide.BUY),
        (InstrumentKind.PERPETUAL, OrderSide.SELL),
    ]
    assert intents[0].quote_amount == intents[1].quote_amount == Decimal("5000")
    assert all(item.atomic_group for item in intents)
    assert intents[0].group_id == intents[1].group_id


def test_carry_exits_both_legs_when_funding_falls() -> None:
    strategy = FundingBasisCarry(
        {"entry_apr": 0.10, "exit_apr": 0.03, "weight": 0.5}
    )

    intents = strategy.on_bar(_opened_context(funding_rate="0.00001"))

    assert {item.reason for item in intents} == {"carry_exit"}
    assert len(intents) == 2
    assert all(item.atomic_group for item in intents)


def test_carry_exits_both_legs_when_basis_exceeds_risk_cap() -> None:
    strategy = FundingBasisCarry({"max_basis_pct": 0.02})

    intents = strategy.on_bar(_opened_context(perpetual_close="103"))

    assert len(intents) == 2
    assert {item.reason for item in intents} == {"carry_exit"}


def test_carry_exits_after_consecutive_negative_funding_events() -> None:
    strategy = FundingBasisCarry(
        {"exit_apr": -1, "negative_intervals": 2}
    )
    first_at = datetime(2024, 1, 1, tzinfo=UTC)

    first = strategy.on_bar(
        _opened_context(funding_rate="-0.00001", timestamp=first_at)
    )
    second = strategy.on_bar(
        _opened_context(
            funding_rate="-0.00001",
            timestamp=first_at + timedelta(hours=8),
        )
    )

    assert first == ()
    assert len(second) == 2
    assert {item.reason for item in second} == {"carry_exit"}


def test_carry_requires_funding_timestamp_to_have_a_paired_perpetual_bar() -> None:
    strategy = FundingBasisCarry({})
    timestamp = datetime(2024, 1, 1, 8, tzinfo=UTC)

    intents = strategy.on_bar(
        _context(
            timestamp=timestamp,
            funding_timestamp=timestamp - timedelta(hours=4),
        )
    )

    assert intents == ()


def test_carry_exits_an_open_pair_when_perpetual_data_is_missing() -> None:
    strategy = FundingBasisCarry({})

    intents = strategy.on_bar(
        _opened_context(include_perpetual=False, funding_rate=None)
    )

    assert len(intents) == 2
    assert {item.reason for item in intents} == {"carry_exit"}


def test_carry_derisks_an_unpaired_position_without_opening_directional_risk() -> None:
    strategy = FundingBasisCarry({})
    context = _context(spot_quantity="50", perpetual_quantity="0")

    intents = strategy.on_bar(context)

    assert len(intents) == 1
    assert intents[0].instrument is InstrumentKind.SPOT
    assert intents[0].side is OrderSide.SELL
    assert not intents[0].atomic_group
    assert intents[0].reason == "carry_unpaired_exit"


@pytest.mark.parametrize(
    "parameters",
    [
        {"entry_apr": -0.1},
        {"exit_apr": -10.1},
        {"entry_apr": 0.05, "exit_apr": 0.10},
        {"weight": 0},
        {"weight": 1.1},
        {"funding_interval_hours": 0},
        {"negative_intervals": 0},
        {"max_basis_pct": -0.01},
    ],
)
def test_carry_parameter_bounds(parameters: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FundingBasisCarryParams.model_validate(parameters)


def test_carry_strategy_is_registered() -> None:
    assert isinstance(
        default_strategy_registry().create("funding_basis_carry", {}),
        FundingBasisCarry,
    )


def test_carry_runs_paired_entry_funding_and_exit_end_to_end() -> None:
    index = pd.date_range("2024-01-01", periods=4, freq="1h", tz="UTC")
    spot = _market_frame(index, (100.0, 101.0, 102.0, 103.0))
    perpetual = _market_frame(index, (101.0, 102.0, 103.0, 104.0))
    funding = pd.DataFrame(
        {"rate": [0.0002, 0.00001], "mark_price": [101.0, 103.0]},
        index=pd.DatetimeIndex([index[0], index[2]]),
    )
    bundle = MarketBundle(
        primary=MarketDataset(
            frame=spot,
            manifest=_manifest("spot", "a"),
        ),
        auxiliary={
            "perpetual": MarketDataset(
                frame=perpetual,
                manifest=_manifest("perpetual", "b"),
            ),
            "funding": MarketDataset(
                frame=funding,
                manifest=_manifest("perpetual", "c"),
            ),
        },
    )
    spec = BacktestSpec(
        strategy="funding_basis_carry",
        strategy_params={
            "entry_apr": 0.10,
            "exit_apr": 0.03,
            "weight": 0.5,
        },
        data=DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1h",
            start=index[0].to_pydatetime(),
            end=(index[-1] + pd.Timedelta(hours=1)).to_pydatetime(),
        ),
        initial_cash=Decimal("10000"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    result = EventRunner().run(spec, bundle, FundingBasisCarry(spec.strategy_params))

    assert len(result.orders) == len(result.fills) == 4
    assert result.orders[0].quantity == Decimal("50")
    assert result.orders[1].quantity == Decimal("5000") / Decimal("101")
    assert {
        fill.instrument: fill.price
        for fill in result.fills[:2]
    } == {
        InstrumentKind.SPOT: Decimal("101"),
        InstrumentKind.PERPETUAL: Decimal("102"),
    }
    assert result.positions[0].quantity == result.positions[1].quantity == 0
    assert result.diagnostics["funding_events"] == 1


def _market_frame(
    index: pd.DatetimeIndex,
    prices: tuple[float, ...],
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": prices,
            "high": tuple(price + 1 for price in prices),
            "low": tuple(price - 1 for price in prices),
            "close": prices,
            "volume": (10.0,) * len(prices),
        },
        index=index,
    )


def _manifest(market: str, digest_character: str) -> DataManifest:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=4)
    return DataManifest(
        provider="fixture",
        market=market,
        symbol="BTC/USD",
        timeframe="1h",
        requested_start=start,
        requested_end=end,
        delivered_start=start,
        delivered_end=end,
        retrieved_at=end,
        real_data=True,
        raw_sha256=(digest_character * 64,),
        normalized_sha256=digest_character * 64,
    )
