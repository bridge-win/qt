# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, stoploss_from_absolute

try:
    from .risk import calculate_atr_stop
except ImportError:
    from risk import calculate_atr_stop


class BtcLowFreqTrend(IStrategy):
    timeframe = "4h"
    startup_candle_count = 1500
    can_short = False

    use_custom_stoploss = True
    stoploss = -0.35
    minimal_roi = {"0": 100}
    process_only_new_candles = True

    atr_multiple = DecimalParameter(2.0, 5.0, default=3.2, decimals=1, space="sell")
    fast_ma_window = IntParameter(240, 720, default=300, space="buy")
    slow_ma_window = IntParameter(900, 1800, default=1200, space="buy")
    breakout_window = IntParameter(180, 720, default=330, space="buy")
    exit_ma_window = IntParameter(180, 720, default=300, space="sell")
    exit_low_window = IntParameter(60, 240, default=120, space="sell")
    min_atr_pct = DecimalParameter(0.002, 0.03, default=0.005, decimals=3, space="buy")
    max_atr_pct = DecimalParameter(0.06, 0.16, default=0.12, decimals=2, space="buy")
    volume_ratio_min = DecimalParameter(0.5, 1.5, default=0.7, decimals=1, space="buy")

    @property
    def protections(self) -> list[dict[str, object]]:
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 6,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 540,
                "trade_limit": 1,
                "stop_duration_candles": 18,
                "max_allowed_drawdown": 0.20,
            },
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        fast_window = int(self.fast_ma_window.value)
        slow_window = int(self.slow_ma_window.value)
        breakout_window = int(self.breakout_window.value)
        exit_ma_window = int(self.exit_ma_window.value)
        exit_low_window = int(self.exit_low_window.value)

        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["fast_ma"] = dataframe["close"].rolling(fast_window).mean()
        dataframe["slow_ma"] = dataframe["close"].rolling(slow_window).mean()
        dataframe["breakout_high"] = dataframe["high"].rolling(breakout_window).max().shift(1)
        dataframe["exit_ma"] = dataframe["close"].rolling(exit_ma_window).mean()
        dataframe["exit_low"] = dataframe["low"].rolling(exit_low_window).min().shift(1)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean"] = dataframe["volume"].rolling(90).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_mean"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["volume"] > 0)
                & (dataframe["close"] > dataframe["slow_ma"])
                & (dataframe["fast_ma"] > dataframe["slow_ma"])
                & (dataframe["close"] > dataframe["breakout_high"])
                & (dataframe["volume_ratio"] >= float(self.volume_ratio_min.value))
                & (dataframe["atr_pct"] >= float(self.min_atr_pct.value))
                & (dataframe["atr_pct"] <= float(self.max_atr_pct.value))
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "low_freq_trend_breakout")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["volume"] > 0)
                & (
                    (dataframe["close"] < dataframe["exit_ma"])
                    | (dataframe["close"] < dataframe["exit_low"])
                )
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "low_freq_trend_exit")
        return dataframe

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
