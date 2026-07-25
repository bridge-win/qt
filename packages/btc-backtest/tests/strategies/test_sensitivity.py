from __future__ import annotations

from decimal import Decimal

import pytest
from btc_backtest.strategies.registry import BUILTIN_STRATEGY_IDS

from .catalog_support import run_catalog

NEARBY_PARAMETERS: dict[str, dict[str, object]] = {
    "fixed_dca": {"quote_amount": 110},
    "smart_dca": {"base_quote": 110},
    "sma_crossover": {"fast_window": 40, "slow_window": 180},
    "ema_crossover": {"fast_window": 40, "slow_window": 180},
    "macd_trend": {"fast": 10, "slow": 24, "signal": 8},
    "rsi_mean_reversion": {"window": 12, "entry": 28, "exit": 52},
    "stochastic_reversal": {
        "k_window": 12,
        "d_window": 3,
        "entry": 15,
        "exit": 85,
    },
    "bollinger_mean_reversion": {"window": 18, "stddev": 2.2},
    "bollinger_breakout": {"window": 18, "stddev": 2.2},
    "donchian_breakout": {"entry_window": 18, "exit_window": 8},
    "turtle_trend": {"entry_window": 18, "exit_window": 9},
    "time_series_momentum": {"lookback": 80},
    "dual_momentum": {"lookback": 80},
    "rate_of_change": {"lookback": 80, "entry": 0.04},
    "adx_trend": {"window": 12, "threshold": 22},
    "atr_volatility_breakout": {"atr_window": 12, "multiplier": 1.1},
    "keltner_channel": {"ema_window": 18, "atr_window": 12},
    "vwap_mean_reversion": {"window": 18, "entry_z": -1.4},
    "grid_rebalance": {
        "lower": 70,
        "upper": 170,
        "levels": 11,
        "quote_per_level": 90,
    },
    "funding_basis_carry": {
        "entry_apr": 0.12,
        "exit_apr": 0.04,
        "weight": 0.35,
    },
}


@pytest.mark.parametrize("strategy_id", BUILTIN_STRATEGY_IDS)
def test_small_parameter_perturbation_stays_finite(strategy_id: str) -> None:
    result, _ = run_catalog(strategy_id, NEARBY_PARAMETERS[strategy_id])

    assert result.snapshots
    assert all(
        snapshot.equity.is_finite()
        and snapshot.cash.is_finite()
        and snapshot.equity > Decimal("0")
        for snapshot in result.snapshots
    )


def test_sensitivity_matrix_covers_exact_common_catalog() -> None:
    assert tuple(NEARBY_PARAMETERS) == BUILTIN_STRATEGY_IDS
