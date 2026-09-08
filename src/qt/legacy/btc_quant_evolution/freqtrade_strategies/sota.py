# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pandas import DataFrame

try:
    from freqtrade.persistence import Trade
    from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, stoploss_from_absolute
except ImportError:
    class IStrategy:
        def __init__(self, config: dict[str, object] | None = None) -> None:
            self.config = config or {}

    class Trade:
        open_rate = 0.0
        is_short = False
        leverage = 1.0

    class _Parameter:
        def __init__(self, low: float, high: float, *, default: float, **kwargs: Any) -> None:
            self.low = low
            self.high = high
            self.value = default

    DecimalParameter = _Parameter
    IntParameter = _Parameter

    def stoploss_from_absolute(
        stop_rate: float,
        *,
        current_rate: float,
        is_short: bool,
        leverage: float,
    ) -> float:
        return (stop_rate / current_rate - 1) * leverage

try:
    from .multisource_features import load_feature_matrix, merge_feature_matrix
    from .risk import calculate_atr_stop, calculate_position_size
    from .sota_indicators import SotaIndicatorParameters, add_sota_indicators
    from .sota_params import SOTA_PARAMETER_SPECS, load_sota_candidate_parameters
except ImportError:
    from multisource_features import load_feature_matrix, merge_feature_matrix
    from risk import calculate_atr_stop, calculate_position_size
    from sota_indicators import SotaIndicatorParameters, add_sota_indicators
    from sota_params import SOTA_PARAMETER_SPECS, load_sota_candidate_parameters


class Sota(IStrategy):
    """Evidence-grounded BTC trend/regime reference; not a profit guarantee."""

    timeframe = "4h"
    startup_candle_count = 2161
    can_short = False
    process_only_new_candles = True

    use_custom_stoploss = True
    stoploss = -0.25
    minimal_roi = {"0": 100}

    # Narrow ranges are priors for train-only optimization, not permission to fit on OOS data.
    risk_per_trade = DecimalParameter(
        SOTA_PARAMETER_SPECS["risk_per_trade"]["low"],
        SOTA_PARAMETER_SPECS["risk_per_trade"]["high"],
        default=0.005,
        decimals=4,
        space="buy",
    )
    atr_multiple = DecimalParameter(
        SOTA_PARAMETER_SPECS["atr_multiple"]["low"],
        SOTA_PARAMETER_SPECS["atr_multiple"]["high"],
        default=3.0,
        decimals=1,
        space="buy",
    )
    target_annual_vol = DecimalParameter(
        SOTA_PARAMETER_SPECS["target_annual_vol"]["low"],
        SOTA_PARAMETER_SPECS["target_annual_vol"]["high"],
        default=0.25,
        decimals=2,
        space="buy",
    )
    momentum_entry_threshold = DecimalParameter(
        SOTA_PARAMETER_SPECS["momentum_entry_threshold"]["low"],
        SOTA_PARAMETER_SPECS["momentum_entry_threshold"]["high"],
        default=0.50,
        decimals=2,
        space="buy",
    )
    external_entry_threshold = DecimalParameter(
        SOTA_PARAMETER_SPECS["external_entry_threshold"]["low"],
        SOTA_PARAMETER_SPECS["external_entry_threshold"]["high"],
        default=0.00,
        decimals=2,
        space="buy",
    )
    min_external_confirmations = IntParameter(
        SOTA_PARAMETER_SPECS["min_external_confirmations"]["low"],
        SOTA_PARAMETER_SPECS["min_external_confirmations"]["high"],
        default=2,
        space="buy",
    )

    indicator_parameters = SotaIndicatorParameters()
    _initial_stop_key = "sota_initial_stop"

    def __init__(self, config: dict[str, object] | None = None) -> None:
        super().__init__(config or {})
        path_text = os.environ.get("FREQTRADE_FEATURE_MATRIX", "").strip()
        if not path_text:
            raise ValueError("FREQTRADE_FEATURE_MATRIX is required")
        self.feature_matrix_path = Path(path_text)
        if not self.feature_matrix_path.is_file():
            raise ValueError(f"missing multi-source feature matrix: {self.feature_matrix_path}")
        self.feature_matrix = load_feature_matrix(self.feature_matrix_path)
        self._runtime_parameters: dict[str, float | int] = {}
        candidate_path_text = os.environ.get("FREQTRADE_SOTA_CANDIDATE", "").strip()
        if candidate_path_text:
            self._runtime_parameters = load_sota_candidate_parameters(
                Path(candidate_path_text),
                root_dir=Path.cwd(),
            )

    @property
    def protections(self) -> list[dict[str, object]]:
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 6},
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 540,
                "trade_limit": 2,
                "stop_duration_candles": 42,
                "max_allowed_drawdown": 0.10,
            },
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        merged = merge_feature_matrix(dataframe, self.feature_matrix)
        return add_sota_indicators(merged, self.indicator_parameters)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        qualified = (
            dataframe["feature_row_complete"].fillna(False)
            & (dataframe["volume"] > 0)
            & (dataframe["close"] > dataframe["ema_regime"])
            & (dataframe["close"] > dataframe["donchian_entry"])
            & (dataframe["momentum_score"] >= float(self._parameter_value("momentum_entry_threshold")))
            & (dataframe["external_score"] >= float(self._parameter_value("external_entry_threshold")))
            & (
                dataframe["external_confirmations"].fillna(0)
                >= int(self._parameter_value("min_external_confirmations"))
            )
            & ~dataframe["liquidity_bad"].fillna(True)
            & ~dataframe["leverage_crowded"].fillna(True)
        ).fillna(False)
        dataframe.loc[qualified, ["enter_long", "enter_tag"]] = (1, "sota_trend_regime")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        should_exit = (
            ~dataframe["feature_row_complete"].fillna(False)
            | (dataframe["close"] < dataframe["ema_regime"])
            | (dataframe["close"] < dataframe["donchian_exit"])
            | (dataframe["momentum_score"] <= 0)
            | dataframe["liquidity_bad"].fillna(True)
        ).fillna(True)
        dataframe.loc[should_exit, ["exit_long", "exit_tag"]] = (1, "sota_regime_exit")
        return dataframe

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: object,
    ) -> float:
        if current_rate <= 0 or max_stake <= 0:
            return 0.0
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return 0.0

        last_candle = dataframe.iloc[-1]
        atr = float(last_candle.get("atr", 0.0) or 0.0)
        realized_vol = float(last_candle.get("realized_vol", 0.0) or 0.0)
        if atr <= 0 or realized_vol <= 0 or not math.isfinite(atr) or not math.isfinite(realized_vol):
            return 0.0

        equity = self._total_stake_equity(max_stake)
        stop_price = calculate_atr_stop(current_rate, atr, float(self._parameter_value("atr_multiple")))
        if not 0 < stop_price < current_rate:
            return 0.0
        atr_sized = calculate_position_size(
            equity=equity,
            entry_price=current_rate,
            stop_price=stop_price,
            risk_fraction=float(self._parameter_value("risk_per_trade")),
            max_position_value=max_stake,
            min_position_value=min_stake or 0.0,
        ).stake_amount
        volatility_cap = equity * float(self._parameter_value("target_annual_vol")) / realized_vol
        stake = min(atr_sized, volatility_cap, max_stake)
        if stake < (min_stake or 0.0):
            return 0.0
        return max(0.0, stake)

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: object,
    ) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty or current_rate <= 0:
            return None
        atr = float(dataframe.iloc[-1].get("atr", 0.0) or 0.0)
        if atr <= 0 or not math.isfinite(atr):
            return None

        atr_multiple = float(self._parameter_value("atr_multiple"))
        # Persist entry risk. Trend exits manage winners; this stop is not a fitted
        # short-horizon trailing rule that can repeatedly cut a valid long trend.
        initial_stop = self._stored_stop(trade, self._initial_stop_key)
        if initial_stop is None:
            candidate = calculate_atr_stop(trade.open_rate, atr, atr_multiple)
            if 0 < candidate < trade.open_rate:
                initial_stop = candidate
                trade.set_custom_data(self._initial_stop_key, initial_stop)
        if initial_stop is None:
            return None

        if initial_stop <= 0 or initial_stop >= current_rate:
            return None
        return stoploss_from_absolute(
            initial_stop,
            current_rate=current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )

    def _total_stake_equity(self, fallback: float) -> float:
        wallets = getattr(self, "wallets", None)
        if wallets is None:
            return fallback
        get_total = getattr(wallets, "get_total_stake_amount", None)
        if not callable(get_total):
            return fallback
        total = float(get_total())
        return total if total > 0 else fallback

    def _parameter_value(self, name: str) -> float | int:
        if name in self._runtime_parameters:
            return self._runtime_parameters[name]
        parameter = getattr(self, name)
        value = parameter.value
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValueError(f"Sota parameter {name} is not numeric")
        return value

    @staticmethod
    def _stored_stop(trade: Trade, key: str) -> float | None:
        value = trade.get_custom_data(key, default=None)
        if value is None:
            return None
        stop = float(value)
        return stop if stop > 0 and math.isfinite(stop) else None

