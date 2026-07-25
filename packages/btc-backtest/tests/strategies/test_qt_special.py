from __future__ import annotations

from datetime import timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from btc_backtest.engine.models import (
    InstrumentKind,
    OrderSide,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import StrategyContext
from btc_backtest.strategies.qt_special import (
    Capitulation,
    CapitulationParams,
    WickCatcher,
    WickCatcherParams,
)
from btc_backtest.strategies.registry import (
    BUILTIN_STRATEGY_IDS,
    EXTRA_STRATEGY_IDS,
    default_strategy_registry,
)
from pydantic import ValidationError

from qt.indicators.composite import compute_extreme_score
from qt.strategies.base import StrategyConfig
from qt.strategies.wick_catcher import WickCatcher as LegacyWickCatcher

UTC = timezone.utc


def _context(
    bars: pd.DataFrame,
    *,
    auxiliary: dict[str, pd.DataFrame] | None = None,
    cash: str = "10000",
    quantity: str = "0",
) -> StrategyContext:
    timestamp = pd.Timestamp(bars.index[-1]).to_pydatetime()
    close = Decimal(str(bars["close"].iloc[-1]))
    qty = Decimal(quantity)
    return StrategyContext(
        timestamp=timestamp,
        bars=bars,
        auxiliary=auxiliary or {},
        portfolio=PortfolioSnapshot(
            timestamp=timestamp,
            cash=Decimal(cash),
            equity=Decimal(cash) + qty * close,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            positions=(
                Position(
                    instrument=InstrumentKind.SPOT,
                    quantity=qty,
                    average_price=close if qty else Decimal("0"),
                ),
                Position(instrument=InstrumentKind.PERPETUAL),
            ),
        ),
    )


def _crash_fixture() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    index = pd.date_range("2024-01-01", periods=800, freq="1h", tz="UTC")
    close = np.linspace(100.0, 120.0, len(index))
    close[-1] = 70.0
    bars = pd.DataFrame(
        {
            "open": np.r_[close[:-1], 100.0],
            "high": np.r_[close[:-1] + 1, 102.0],
            "low": np.r_[close[:-1] - 1, 60.0],
            "close": close,
            "volume": np.r_[np.full(len(index) - 1, 10.0), 1000.0],
        },
        index=index,
    )
    funding = np.zeros(len(index))
    funding[-3:] = -0.001
    auxiliary = {
        "funding": pd.DataFrame({"rate": funding}, index=index),
        "open_interest": pd.DataFrame(
            {"value": np.r_[np.full(len(index) - 24, 100.0), np.linspace(100, 70, 24)]},
            index=index,
        ),
        "long_short_ratio": pd.DataFrame(
            {"value": np.linspace(2.0, 0.1, len(index))},
            index=index,
        ),
        "mvrv": pd.DataFrame(
            {"value": np.r_[np.ones(len(index) - 1), -1.0]},
            index=index,
        ),
        "fear_greed": pd.DataFrame(
            {"value": np.r_[np.full(len(index) - 1, 50.0), 10.0]},
            index=index,
        ),
        "macro": pd.DataFrame(
            {"vix": np.full(len(index), 20.0), "dxy_z": np.zeros(len(index))},
            index=index,
        ),
    }
    return bars, auxiliary


def _wick_fixture() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [99.0, 91.5],
            "close": [100.0, 97.5],
            "volume": [10.0, 20.0],
        },
        index=index,
    )


def test_capitulation_buys_when_four_factor_groups_fire() -> None:
    bars, auxiliary = _crash_fixture()

    intents = Capitulation({}).on_bar(_context(bars, auxiliary=auxiliary))

    assert len(intents) == 1
    assert intents[0].side is OrderSide.BUY
    assert intents[0].reason == "capitulation_entry"


def test_capitulation_macro_veto_blocks_entry() -> None:
    bars, auxiliary = _crash_fixture()
    auxiliary["macro"].iloc[-1, auxiliary["macro"].columns.get_loc("vix")] = 50

    intents = Capitulation({}).on_bar(_context(bars, auxiliary=auxiliary))

    assert intents == ()


def test_capitulation_decision_matches_legacy_normalized_formula() -> None:
    bars, auxiliary = _crash_fixture()
    legacy = compute_extreme_score(
        ohlcv=bars,
        funding=auxiliary["funding"]["rate"],
        oi=auxiliary["open_interest"]["value"],
        long_short_ratio=auxiliary["long_short_ratio"]["value"],
        mvrv_z=auxiliary["mvrv"]["value"],
        fear_greed=auxiliary["fear_greed"]["value"],
        vix=auxiliary["macro"]["vix"],
    )
    legacy_triggered = (
        float(legacy.score.iloc[-1]) >= 0.60
        and int(legacy.group_flags.iloc[-1].sum()) >= 4
        and bool(legacy.macro_ok.iloc[-1])
    )

    current_triggered = bool(
        Capitulation({}).on_bar(_context(bars, auxiliary=auxiliary))
    )

    assert legacy_triggered
    assert current_triggered == legacy_triggered


def test_wick_catcher_creates_bounded_deep_limit_levels() -> None:
    strategy = WickCatcher(
        {
            "ladder_drawdowns": [0.05, 0.08, 0.12],
            "rung_quote": 100,
        }
    )

    intents = strategy.on_bar(_context(_wick_fixture()))

    assert [item.limit_price for item in intents] == [
        Decimal("95.00"),
        Decimal("92.00"),
        Decimal("88.00"),
    ]
    assert all(item.side is OrderSide.BUY for item in intents)
    assert sum(
        (item.quote_amount or Decimal("0") for item in intents),
        Decimal("0"),
    ) <= Decimal("10000")


def test_wick_catcher_pierced_rungs_match_legacy_decision() -> None:
    bars = _wick_fixture()
    parameters = {
        "ladder_drawdowns": [0.05, 0.08, 0.12],
        "rung_quote": 100,
    }
    legacy = LegacyWickCatcher(
        StrategyConfig(name="wick", params=parameters)
    ).evaluate({"ohlcv": bars})
    current = WickCatcher(parameters).on_bar(_context(bars))
    low = Decimal(str(bars["low"].iloc[-1]))
    pierced = [
        float(Decimal("1") - (item.limit_price or Decimal("0")) / Decimal("100"))
        for item in current
        if item.limit_price is not None and low <= item.limit_price
    ]

    assert legacy.opportunity is not None
    assert pierced == legacy.opportunity.details["filled_rungs"]


def test_wick_catcher_respects_inventory_cap_and_macro_veto() -> None:
    strategy = WickCatcher(
        {
            "ladder_drawdowns": [0.05, 0.08, 0.12],
            "rung_quote": 100,
            "max_inventory_weight": 0.2,
        }
    )
    bars = _wick_fixture()
    timestamp = bars.index[-1]
    macro = pd.DataFrame({"vix": [50.0], "dxy_z": [0.0]}, index=[timestamp])

    capped_context = _context(bars, cash="810", quantity="1.9")
    capped = strategy.on_bar(capped_context)
    vetoed = strategy.on_bar(_context(bars, auxiliary={"macro": macro}))
    inventory = Decimal("1.9") * Decimal("97.5")
    capacity = capped_context.portfolio.equity * Decimal("0.2") - inventory

    assert sum(
        (item.quote_amount or Decimal("0") for item in capped),
        Decimal("0"),
    ) <= capacity
    assert vetoed == ()


def test_qt_special_ids_do_not_change_top_twenty() -> None:
    registry = default_strategy_registry()

    assert set(EXTRA_STRATEGY_IDS) == {
        "buy_and_hold",
        "capitulation",
        "wick_catcher",
    }
    assert set(EXTRA_STRATEGY_IDS) <= set(registry.list())
    assert len(BUILTIN_STRATEGY_IDS) == 20


@pytest.mark.parametrize(
    ("model", "parameters"),
    [
        (CapitulationParams, {"score_min": 0.1, "exit_score": 0.2}),
        (CapitulationParams, {"min_groups_firing": 6}),
        (CapitulationParams, {"allocation": 0}),
        (WickCatcherParams, {"ladder_drawdowns": []}),
        (WickCatcherParams, {"ladder_drawdowns": [0.05, 0.05]}),
        (WickCatcherParams, {"ladder_drawdowns": [1]}),
        (WickCatcherParams, {"rung_quote": 0}),
        (WickCatcherParams, {"max_inventory_weight": 1.1}),
    ],
)
def test_qt_special_parameter_bounds(
    model: type[CapitulationParams] | type[WickCatcherParams],
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(parameters)


def test_capitulation_declares_point_in_time_factor_dependencies() -> None:
    assert Capitulation.metadata.signal_dependencies == (
        "funding",
        "open_interest",
        "long_short_ratio",
        "mvrv",
        "fear_greed",
        "macro",
    )
