# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pandas import DataFrame
from qt.workbench.catalog import legacy_catalog

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
    from .catalog_signals import add_catalog_indicators, evaluate_signal
    from .risk import calculate_atr_stop, calculate_position_size
except ImportError:
    from catalog_signals import add_catalog_indicators, evaluate_signal
    from risk import calculate_atr_stop, calculate_position_size


class BtcCatalogStrategy(IStrategy):
    timeframe = "4h"
    startup_candle_count = 260
    can_short = False

    use_custom_stoploss = True
    stoploss = -0.25
    minimal_roi = {"0": 100}
    process_only_new_candles = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        profile_id = os.environ.get("FREQTRADE_PROFILE", "").strip()
        if not profile_id:
            raise ValueError("FREQTRADE_PROFILE is required for BtcCatalogStrategy")
        self.catalog = legacy_catalog()
        self.profile = _select_profile(self.catalog, profile_id)
        self.profile_id = str(self.profile["id"])
        self.catalog_sha256 = str(self.catalog["sha256"])

    @property
    def protections(self) -> list[dict[str, object]]:
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 1,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 180,
                "trade_limit": 1,
                "stop_duration_candles": 6,
                "max_allowed_drawdown": 0.10,
            },
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        return add_catalog_indicators(dataframe)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        signal = _combine_entry_signals(dataframe, self.profile)
        dataframe.loc[signal, ["enter_long", "enter_tag"]] = (
            1,
            f"catalog:{self.profile_id}:{self.catalog_sha256[:12]}",
        )
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        signal = _combine_exit_signals(dataframe, self.profile)
        dataframe.loc[signal, ["exit_long", "exit_tag"]] = (1, f"catalog_exit:{self.profile_id}")
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

        equity = self._total_stake_equity(max_stake)
        risk = self.profile["risk"]
        stop_price = calculate_atr_stop(current_rate, atr, float(risk["atr_multiple"]))
        position_size = calculate_position_size(
            equity=equity,
            entry_price=current_rate,
            stop_price=stop_price,
            risk_fraction=float(risk["risk_per_trade"]),
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

        atr_multiple = float(self.profile["risk"]["atr_multiple"])
        atr_stop = current_rate - (atr * atr_multiple)
        initial_stop = calculate_atr_stop(trade.open_rate, atr, atr_multiple)
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


def _combine_entry_signals(dataframe: DataFrame, profile: dict[str, Any]):
    signal_ids = profile["entry_signals"]
    combined = None
    for signal_id in signal_ids:
        signal = evaluate_signal(signal_id, dataframe, profile["signal_params"].get(signal_id, {}))
        combined = signal if combined is None else combined & signal
    return combined.fillna(False) if combined is not None else dataframe["close"] == -1


def _combine_exit_signals(dataframe: DataFrame, profile: dict[str, Any]):
    signal_ids = profile["exit_signals"]
    combined = None
    for signal_id in signal_ids:
        signal = evaluate_signal(signal_id, dataframe, profile["signal_params"].get(signal_id, {}))
        combined = signal if combined is None else combined | signal
    return combined.fillna(False) if combined is not None else dataframe["close"] == -1


def _load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"missing runtime catalog artifact: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload.get("sha256")
    actual = _catalog_sha256(payload)
    if expected != actual:
        raise ValueError(f"runtime catalog hash mismatch: expected {expected}, got {actual}")
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported runtime catalog schema: {payload.get('schema_version')}")
    if len(payload.get("signals", [])) != 50 or len(payload.get("profiles", [])) != 100:
        raise ValueError("runtime catalog must contain exactly 50 signals and 100 profiles")
    return payload


def _select_profile(catalog: dict[str, Any], profile_id: str) -> dict[str, Any]:
    for profile in catalog["profiles"]:
        if profile["id"] == profile_id:
            if profile.get("pair") != "BTC/USDT" or profile.get("timeframe") != "4h":
                raise ValueError(f"unsupported catalog profile pair/timeframe: {profile_id}")
            return profile
    raise ValueError(f"unknown catalog profile: {profile_id}")


def _catalog_sha256(payload: dict[str, Any]) -> str:
    payload_without_hash = {key: value for key, value in payload.items() if key != "sha256"}
    serialized = json.dumps(payload_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
