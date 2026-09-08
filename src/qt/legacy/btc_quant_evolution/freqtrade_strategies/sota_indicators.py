# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from pandas import DataFrame, Series, concat

EXTERNAL_SCORE_COLUMNS = (
    "derivatives_score",
    "onchain_score",
    "sentiment_score",
    "liquidity_score",
)
REQUIRED_FEATURE_COLUMNS = (
    "technical_score",
    *EXTERNAL_SCORE_COLUMNS,
    "liquidity_bad",
    "leverage_crowded",
)


@dataclass(frozen=True)
class SotaIndicatorParameters:
    # Round 30/90/180/360-day horizons avoid fitting a fragile single lookback.
    momentum_windows: tuple[int, int, int, int] = (180, 540, 1080, 2160)
    ema_period: int = 1200  # 200 days on 4h candles.
    entry_window: int = 330  # 55-day Donchian breakout.
    exit_window: int = 120  # 20-day Donchian exit.
    atr_period: int = 20
    realized_vol_window: int = 180  # 30 days on 4h candles.
    periods_per_year: int = 6 * 365

    def __post_init__(self) -> None:
        values = (
            *self.momentum_windows,
            self.ema_period,
            self.entry_window,
            self.exit_window,
            self.atr_period,
            self.realized_vol_window,
            self.periods_per_year,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Sota indicator periods must be positive")
        if tuple(sorted(self.momentum_windows)) != self.momentum_windows:
            raise ValueError("Sota momentum windows must be strictly ordered")
        if len(set(self.momentum_windows)) != len(self.momentum_windows):
            raise ValueError("Sota momentum windows must be unique")


def add_sota_indicators(
    dataframe: DataFrame,
    parameters: SotaIndicatorParameters | None = None,
) -> DataFrame:
    parameters = parameters or SotaIndicatorParameters()
    _require_columns(dataframe, {"high", "low", "close", *REQUIRED_FEATURE_COLUMNS})
    result = dataframe.copy()

    result["atr"] = _wilder_atr(result, parameters.atr_period)
    log_returns = np.log(result["close"] / result["close"].shift(1))
    result["realized_vol"] = (
        log_returns.rolling(parameters.realized_vol_window, min_periods=parameters.realized_vol_window).std(ddof=0)
        * math.sqrt(parameters.periods_per_year)
    )
    result["ema_regime"] = result["close"].ewm(
        span=parameters.ema_period,
        adjust=False,
        min_periods=parameters.ema_period,
    ).mean()

    # Shift channels by one candle so the current high/low cannot define its own signal.
    result["donchian_entry"] = (
        result["high"].rolling(parameters.entry_window, min_periods=parameters.entry_window).max().shift(1)
    )
    result["donchian_exit"] = (
        result["low"].rolling(parameters.exit_window, min_periods=parameters.exit_window).min().shift(1)
    )

    momentum_returns = concat(
        [result["close"].pct_change(window, fill_method=None) for window in parameters.momentum_windows],
        axis=1,
    )
    result["momentum_score"] = np.sign(momentum_returns).mean(
        axis=1,
        skipna=False,
    )

    external = result.loc[:, EXTERNAL_SCORE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    feature_complete = result.loc[:, REQUIRED_FEATURE_COLUMNS].notna().all(axis=1)
    # Median and equal votes reduce dependence on any one noisy alternative source.
    result["external_score"] = external.median(axis=1, skipna=False)
    confirmations = external.ge(0).sum(axis=1).astype("Int64")
    result["external_confirmations"] = confirmations.where(feature_complete, pd.NA)
    result["feature_row_complete"] = feature_complete.astype(bool)
    return result


def _wilder_atr(dataframe: DataFrame, period: int) -> Series:
    previous_close = dataframe["close"].shift(1)
    true_range = concat(
        [
            dataframe["high"] - dataframe["low"],
            (dataframe["high"] - previous_close).abs(),
            (dataframe["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _require_columns(dataframe: DataFrame, required: set[str]) -> None:
    missing = sorted(required - set(dataframe.columns))
    if missing:
        raise ValueError(f"Sota indicators missing required columns: {', '.join(missing)}")

