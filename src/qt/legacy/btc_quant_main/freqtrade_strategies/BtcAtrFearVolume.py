# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, stoploss_from_absolute

try:
    from qt.legacy.btc_quant_main.fear_volume_indicators import add_fear_volume_features, fear_volume_entry_signal
except ImportError:
    from fear_volume_indicators import add_fear_volume_features, fear_volume_entry_signal

try:
    from .risk import calculate_atr_stop, calculate_position_size
except ImportError:
    from risk import calculate_atr_stop, calculate_position_size


class BtcAtrFearVolume(IStrategy):
    timeframe = "4h"
    startup_candle_count = 220
    can_short = False

    use_custom_stoploss = True
    stoploss = -0.25
    minimal_roi = {"0": 100}
    process_only_new_candles = True

    risk_per_trade = DecimalParameter(0.0025, 0.01, default=0.005, decimals=4, space="buy")
    atr_multiple = DecimalParameter(1.5, 4.0, default=2.8, decimals=1, space="buy")
    fear_window = IntParameter(30, 180, default=90, space="buy")
    fear_drawdown_threshold = DecimalParameter(0.05, 0.35, default=0.12, decimals=2, space="buy")
    volume_window = IntParameter(10, 80, default=30, space="buy")
    volume_spike_multiple = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="buy")
    atr_pct_min = DecimalParameter(0.005, 0.08, default=0.02, decimals=3, space="buy")
    fear_recovery_threshold = DecimalParameter(0.01, 0.15, default=0.04, decimals=2, space="sell")

    @property
    def protections(self) -> list[dict[str, object]]:
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 2,
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
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return add_fear_volume_features(
            dataframe,
            fear_window=int(self.fear_window.value),
            volume_window=int(self.volume_window.value),
        )

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            fear_volume_entry_signal(
                dataframe,
                min_drawdown=float(self.fear_drawdown_threshold.value),
                min_volume_ratio=float(self.volume_spike_multiple.value),
                min_atr_pct=float(self.atr_pct_min.value),
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "atr_fear_volume_reversal")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["volume"] > 0)
                & (dataframe["fear_drawdown"] <= float(self.fear_recovery_threshold.value))
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "fear_recovered")
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

        last_candle = dataframe.iloc[-1].squeeze()
        atr = float(last_candle.get("atr", 0.0) or 0.0)
        if atr <= 0:
            return 0.0

        equity = self._total_stake_equity(max_stake)
        stop_price = calculate_atr_stop(current_rate, atr, float(self.atr_multiple.value))
        min_position_value = min_stake or 0.0
        position_size = calculate_position_size(
            equity=equity,
            entry_price=current_rate,
            stop_price=stop_price,
            risk_fraction=float(self.risk_per_trade.value),
            max_position_value=max_stake,
            min_position_value=min_position_value,
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

        last_candle = dataframe.iloc[-1].squeeze()
        atr = float(last_candle.get("atr", 0.0) or 0.0)
        if atr <= 0:
            return None

        atr_stop = current_rate - (atr * float(self.atr_multiple.value))
        initial_stop = calculate_atr_stop(trade.open_rate, atr, float(self.atr_multiple.value))
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
        if total <= 0:
            return fallback
        return total
