from __future__ import annotations

import math

import pandas as pd
import pytest
from btc_backtest.validation.monte_carlo import BlockBootstrap


def returns() -> pd.Series:
    return pd.Series([0.01, -0.02, 0.03, 0.00, -0.01, 0.02])


def test_block_bootstrap_is_seeded_and_preserves_length() -> None:
    first = BlockBootstrap.run(
        returns(),
        simulations=100,
        block_size=3,
        seed=7,
    )
    second = BlockBootstrap.run(
        returns(),
        simulations=100,
        block_size=3,
        seed=7,
    )

    assert first == second
    assert len(first.paths) == 100
    assert all(len(path) == len(returns()) for path in first.paths)
    assert all(math.isfinite(value) for path in first.paths for value in path)


def test_block_bootstrap_rejects_empty_or_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="returns"):
        BlockBootstrap.run(pd.Series(dtype="float64"), simulations=10, block_size=2, seed=7)
    with pytest.raises(ValueError, match="positive"):
        BlockBootstrap.run(returns(), simulations=0, block_size=2, seed=7)
    with pytest.raises(ValueError, match="block_size"):
        BlockBootstrap.run(returns(), simulations=10, block_size=0, seed=7)
