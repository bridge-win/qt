# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import os
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from pandas import DataFrame, Series, concat

try:
    from freqtrade.persistence import Trade
    from freqtrade.strategy import IStrategy, stoploss_from_absolute
except ImportError:
    class IStrategy:
        def __init__(self, config: dict[str, Any] | None = None) -> None:
            self.config = config or {}

    class Trade:
        open_rate = 0.0
        is_short = False
        leverage = 1.0

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
except ImportError:
    from multisource_features import load_feature_matrix, merge_feature_matrix
    from risk import calculate_atr_stop, calculate_position_size

_SCORE_COLUMNS = (
    "technical_score",
    "derivatives_score",
    "onchain_score",
    "sentiment_score",
    "liquidity_score",
)


class BtcMultiSourceRegimeStrategy(IStrategy):
    timeframe = "4h"
    startup_candle_count = 260
    can_short = False
    use_custom_stoploss = True
    stoploss = -0.25
    minimal_roi = {"0": 100}
    process_only_new_candles = True

    risk_per_trade = 0.005
    atr_multiple = 2.5
    entry_threshold = 1.0
    exit_threshold = 0.2

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        path_text = os.environ.get("FREQTRADE_FEATURE_MATRIX", "").strip()
        if not path_text:
            raise ValueError("FREQTRADE_FEATURE_MATRIX is required")
        self.feature_matrix_path = Path(path_text)
        if not self.feature_matrix_path.exists():
            raise ValueError(f"missing multi-source feature matrix: {self.feature_matrix_path}")
        self.feature_matrix = load_feature_matrix(self.feature_matrix_path)
        self.feature_weights = {column.removesuffix("_score"): 1.0 for column in _SCORE_COLUMNS}
        candidate = _load_candidate_config()
        if candidate is not None:
            self._apply_candidate_config(candidate)

    @property
    def protections(self) -> list[dict[str, object]]:
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 1},
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 180,
                "trade_limit": 1,
                "stop_duration_candles": 6,
                "max_allowed_drawdown": 0.10,
            },
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        enriched = merge_feature_matrix(dataframe, self.feature_matrix)
        enriched["atr"] = _atr(enriched)
        weighted_scores = [
            enriched[column] * self.feature_weights[column.removesuffix("_score")]
            for column in _SCORE_COLUMNS
        ]
        enriched["entry_score"] = concat(weighted_scores, axis=1).sum(axis=1, min_count=len(_SCORE_COLUMNS))
        return enriched

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["entry_score"] >= self.entry_threshold)
                & ~dataframe["liquidity_bad"].fillna(True)
                & ~dataframe["leverage_crowded"].fillna(True)
                & (dataframe["volume"] > 0)
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "multisource_regime")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["entry_score"] < self.exit_threshold)
                | dataframe["liquidity_bad"].fillna(True)
                | dataframe["leverage_crowded"].fillna(True)
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "multisource_regime_exit")
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

        atr = float(dataframe.iloc[-1].get("atr", 0.0) or 0.0)
        if atr <= 0:
            return 0.0

        stop_price = calculate_atr_stop(current_rate, atr, self.atr_multiple)
        position_size = calculate_position_size(
            equity=self._total_stake_equity(max_stake),
            entry_price=current_rate,
            stop_price=stop_price,
            risk_fraction=self.risk_per_trade,
            max_position_value=max_stake,
            min_position_value=min_stake or 0.0,
        )
        return position_size.stake_amount

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
        if atr <= 0:
            return None

        atr_stop = current_rate - (atr * self.atr_multiple)
        initial_stop = calculate_atr_stop(trade.open_rate, atr, self.atr_multiple)
        stop_price = max(atr_stop, initial_stop)
        if stop_price <= 0 or stop_price >= current_rate:
            return None

        return stoploss_from_absolute(
            stop_price,
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

    def _apply_candidate_config(self, candidate: dict[str, object]) -> None:
        weights = candidate.get("feature_weights")
        if not isinstance(weights, dict):
            raise ValueError("candidate config feature_weights must be an object")
        for name, value in weights.items():
            if name not in self.feature_weights:
                raise ValueError(f"candidate config has unsupported feature weight: {name}")
            self.feature_weights[name] = _positive_number(value, f"feature_weights.{name}")
        self.entry_threshold = _positive_number(candidate.get("entry_threshold"), "entry_threshold")
        self.exit_threshold = _positive_number(candidate.get("exit_threshold"), "exit_threshold")
        self.atr_multiple = _positive_number(candidate.get("atr_multiple"), "atr_multiple")
        self.risk_per_trade = _positive_number(candidate.get("risk_fraction"), "risk_fraction")


def _load_candidate_config() -> dict[str, object] | None:
    path_text = os.environ.get("FREQTRADE_EVOLUTION_CANDIDATE", "").strip()
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_file():
        raise ValueError(f"missing evolution candidate config: {path}")
    try:
        candidate = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid evolution candidate config: {path}") from exc
    if not isinstance(candidate, dict):
        raise ValueError("evolution candidate config must be an object")
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate config id is required")
    return candidate


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"candidate config {name} must be a positive finite number")
    return float(value)


def _atr(dataframe: DataFrame) -> Series:
    previous_close = dataframe["close"].shift(1)
    true_range = concat(
        [
            dataframe["high"] - dataframe["low"],
            (dataframe["high"] - previous_close).abs(),
            (dataframe["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(14).mean()

