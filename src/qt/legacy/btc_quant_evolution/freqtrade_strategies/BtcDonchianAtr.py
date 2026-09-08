# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, stoploss_from_absolute

try:
    from .risk import calculate_atr_stop, calculate_position_size
except ImportError:
    from risk import calculate_atr_stop, calculate_position_size


class BtcDonchianAtr(IStrategy):
    timeframe = "4h"
    startup_candle_count = 120
    can_short = False

    use_custom_stoploss = True
    stoploss = -0.25
    minimal_roi = {"0": 100}
    process_only_new_candles = True

    risk_per_trade = DecimalParameter(0.0025, 0.01, default=0.005, decimals=4, space="buy")
    atr_multiple = DecimalParameter(1.5, 4.0, default=2.5, decimals=1, space="buy")
    donchian_window = IntParameter(20, 80, default=55, space="buy")
    adx_threshold = IntParameter(18, 35, default=25, space="buy")
    exit_window = IntParameter(10, 40, default=20, space="sell")

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
        window = int(self.donchian_window.value)
        exit_window = int(self.exit_window.value)

        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["donchian_upper"] = dataframe["high"].rolling(window).max().shift(1)
        dataframe["donchian_lower"] = dataframe["low"].rolling(window).min().shift(1)
        dataframe["donchian_exit"] = dataframe["low"].rolling(exit_window).min().shift(1)
        dataframe["donchian_mid"] = (dataframe["donchian_upper"] + dataframe["donchian_lower"]) / 2
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["volume"] > 0)
                & (dataframe["close"] > dataframe["donchian_upper"])
                & (dataframe["adx"] >= int(self.adx_threshold.value))
                & (dataframe["atr_pct"] > 0.005)
                & (dataframe["atr_pct"] < 0.12)
            ),
            ["enter_long", "enter_tag"],
        ] = (1, "donchian_breakout_atr_adx")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict[str, object]) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["volume"] > 0)
                & (
                    (dataframe["close"] < dataframe["donchian_exit"])
                    | (dataframe["close"] < dataframe["donchian_mid"])
                )
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "donchian_exit")
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
