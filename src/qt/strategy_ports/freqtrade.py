"""Research ports for the preserved btc-quant Freqtrade strategy families.

The ports deliberately do not import an ``IStrategy`` fallback or make fake
``Trade`` objects.  They invoke the preserved source indicator/signal/risk
functions and expose the resulting entry, exit, sizing, and stop lifecycle to
the native engine through small explicit domain models.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Literal, Protocol, cast

import pandas as pd
from btc_backtest.engine.models import InstrumentKind, OrderIntent, OrderSide, OrderType
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)
from pandas import DataFrame

from qt.legacy.btc_quant_evolution.freqtrade_strategies import (
    BtcMultiSourceRegimeStrategy as BtcMultiSourceRegimeStrategySource,
)
from qt.legacy.btc_quant_evolution.freqtrade_strategies import (
    multisource_features,
    sota_indicators,
    sota_params,
)
from qt.legacy.btc_quant_main.freqtrade_strategies import catalog_signals, fear_volume_indicators
from qt.legacy.btc_quant_main.freqtrade_strategies import risk as source_risk
from qt.strategy_ports.btcqt import DataDependency, DataVersion, SourceIdentity
from qt.workbench.catalog import legacy_catalog


class FreqtradeRuntimeUnavailableError(RuntimeError):
    """Raised when a source strategy needs a real dependency that is absent."""


class MissingFreqtradeDataError(ValueError):
    """Raised instead of substituting missing external evidence with zeroes."""


class TalibAbstract(Protocol):
    def ATR(self, frame: DataFrame, *, timeperiod: int) -> pd.Series:  # noqa: N802
        ...

    def ADX(self, frame: DataFrame, *, timeperiod: int) -> pd.Series:  # noqa: N802
        ...


@dataclass(frozen=True)
class FreqtradePortMetadata:
    strategy_id: str
    source: SourceIdentity
    required_data: tuple[DataDependency, ...]
    protections: tuple[Mapping[str, object], ...]
    startup_candles: int
    minimal_roi: Mapping[str, int]
    hard_stoploss: float
    requires_talib: bool = False


MAIN_SOURCE = SourceIdentity(
    repository="/Users/kwt/x/btc-quant",
    commit="e2ecad26ccc63f9790aedb1cf44b785e6b03b5b3",
    module="qt.legacy.btc_quant_main",
    source_version="main",
)
EVOLUTION_SOURCE = SourceIdentity(
    repository="/Users/kwt/x/btc-quant/.worktrees/multisource-evolution",
    commit="e0b47ca37910ea30de227c214c834b4796f7a281",
    module="qt.legacy.btc_quant_evolution",
    source_version="multisource-evolution",
)

_STANDARD_PROTECTIONS = (
    MappingProxyType({"method": "CooldownPeriod", "stop_duration_candles": 1}),
    MappingProxyType(
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 180,
            "trade_limit": 1,
            "stop_duration_candles": 6,
            "max_allowed_drawdown": 0.10,
        }
    ),
)

FREQTRADE_PORTS: Mapping[str, FreqtradePortMetadata] = MappingProxyType(
    {
        "btc_quant_catalog": FreqtradePortMetadata(
            "btc_quant_catalog",
            MAIN_SOURCE,
            (DataDependency("ohlcv_4h", "catalog profile indicators"),),
            _STANDARD_PROTECTIONS,
            260,
            MappingProxyType({"0": 100}),
            -0.25,
        ),
        "btc_atr_fear_volume": FreqtradePortMetadata(
            "btc_atr_fear_volume",
            MAIN_SOURCE,
            (DataDependency("ohlcv_4h", "TA-Lib ATR plus fear/volume features"),),
            (
                MappingProxyType({"method": "CooldownPeriod", "stop_duration_candles": 2}),
                _STANDARD_PROTECTIONS[1],
            ),
            220,
            MappingProxyType({"0": 100}),
            -0.25,
            requires_talib=True,
        ),
        "btc_donchian_atr": FreqtradePortMetadata(
            "btc_donchian_atr",
            MAIN_SOURCE,
            (DataDependency("ohlcv_4h", "TA-Lib ATR/ADX and Donchian channels"),),
            _STANDARD_PROTECTIONS,
            120,
            MappingProxyType({"0": 100}),
            -0.25,
            requires_talib=True,
        ),
        "btc_low_freq_trend": FreqtradePortMetadata(
            "btc_low_freq_trend",
            MAIN_SOURCE,
            (DataDependency("ohlcv_4h", "TA-Lib ATR/ADX and long-horizon trend"),),
            (
                MappingProxyType({"method": "CooldownPeriod", "stop_duration_candles": 6}),
                MappingProxyType(
                    {
                        "method": "MaxDrawdown",
                        "lookback_period_candles": 540,
                        "trade_limit": 1,
                        "stop_duration_candles": 18,
                        "max_allowed_drawdown": 0.20,
                    }
                ),
            ),
            1500,
            MappingProxyType({"0": 100}),
            -0.35,
            requires_talib=True,
        ),
        "btc_multisource_regime": FreqtradePortMetadata(
            "btc_multisource_regime",
            EVOLUTION_SOURCE,
            (
                DataDependency("ohlcv_4h", "price and ATR input"),
                DataDependency("multisource_feature_matrix", "causal external score matrix"),
            ),
            _STANDARD_PROTECTIONS,
            260,
            MappingProxyType({"0": 100}),
            -0.25,
        ),
        "btc_sota": FreqtradePortMetadata(
            "btc_sota",
            EVOLUTION_SOURCE,
            (
                DataDependency("ohlcv_4h", "long-horizon trend regime"),
                DataDependency("multisource_feature_matrix", "causal external score matrix"),
            ),
            (
                MappingProxyType({"method": "CooldownPeriod", "stop_duration_candles": 6}),
                MappingProxyType(
                    {
                        "method": "MaxDrawdown",
                        "lookback_period_candles": 540,
                        "trade_limit": 2,
                        "stop_duration_candles": 42,
                        "max_allowed_drawdown": 0.10,
                    }
                ),
            ),
            2161,
            MappingProxyType({"0": 100}),
            -0.25,
        ),
    }
)


@dataclass(frozen=True)
class FreqtradeCausalFrame:
    """Candle history and its data identities, bounded at a decision close."""

    candles: DataFrame
    decision_at: datetime
    available_at: datetime
    inputs: tuple[DataVersion, ...]

    def __post_init__(self) -> None:
        _require_aware(self.decision_at, "decision timestamp")
        _require_aware(self.available_at, "availability timestamp")
        if self.available_at > self.decision_at:
            raise ValueError("strategy data is not available at the decision timestamp")
        if not isinstance(self.candles.index, pd.DatetimeIndex) or self.candles.index.tz is None:
            raise ValueError("candles require a timezone-aware DatetimeIndex")
        if not self.candles.index.is_monotonic_increasing or not self.candles.index.is_unique:
            raise ValueError("candle timestamps must be unique and increasing")
        if self.candles.empty:
            raise ValueError("candle history cannot be empty")
        if self.candles.index[-1] > pd.Timestamp(self.decision_at):
            raise ValueError("candle history contains future rows")
        required = {"open", "high", "low", "close", "volume"}
        missing = sorted(required - set(self.candles.columns))
        if missing:
            raise ValueError(f"candles missing required columns: {', '.join(missing)}")
        if any(item.available_at > self.available_at for item in self.inputs):
            raise ValueError("input identity is unavailable at the decision timestamp")
        object.__setattr__(self, "candles", self.candles.copy(deep=True))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    def visible(self) -> DataFrame:
        frame = self.candles.loc[self.candles.index <= pd.Timestamp(self.decision_at)].copy(deep=True)
        frame.insert(0, "date", frame.index)
        return frame


@dataclass(frozen=True)
class FreqtradeTradeState:
    entry_price: float
    stake_amount: float
    initial_stop: float
    active_stop: float
    remaining_quantity: float
    opened_at: datetime
    total_fees: float = 0.0
    filled_quantities: Mapping[str, float] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isfinite(self.entry_price) or self.entry_price <= 0:
            raise ValueError("trade entry_price must be finite and positive")
        if not all(isfinite(value) and value >= 0 for value in (self.stake_amount, self.remaining_quantity)):
            raise ValueError("trade stake_amount and remaining_quantity must be finite and non-negative")
        if (self.remaining_quantity == 0) != (self.stake_amount == 0):
            raise ValueError("flat trades must have zero stake_amount and quantity")
        if not isfinite(self.initial_stop) or not 0 < self.initial_stop < self.entry_price:
            raise ValueError("initial_stop must be finite and between zero and entry_price")
        if not isfinite(self.active_stop) or self.active_stop <= 0:
            raise ValueError("active_stop must be finite and positive")
        if not isfinite(self.total_fees) or self.total_fees < 0:
            raise ValueError("total_fees must be finite and non-negative")
        _require_aware(self.opened_at, "trade opened_at")
        normalized_fills = {order_id: float(quantity) for order_id, quantity in self.filled_quantities.items()}
        if any(not order_id or not isfinite(quantity) or quantity < 0 for order_id, quantity in normalized_fills.items()):
            raise ValueError("filled quantities require non-empty identifiers and finite non-negative quantities")
        object.__setattr__(self, "filled_quantities", MappingProxyType(normalized_fills))

    @property
    def is_closed(self) -> bool:
        return self.remaining_quantity == 0


@dataclass(frozen=True)
class FreqtradeOrder:
    action: Literal["enter_long", "exit_long", "replace_stop"]
    quantity: float
    price: float | None
    reason: str
    stop_distance: float | None = None
    order_id: str = ""


@dataclass(frozen=True)
class FreqtradeDecision:
    strategy_id: str
    timestamp: datetime
    profile_fingerprint: str
    frame: DataFrame
    input_versions: tuple[DataVersion, ...]
    orders: tuple[FreqtradeOrder, ...]
    protections: tuple[Mapping[str, object], ...]
    protection_state: FreqtradeProtectionState


@dataclass(frozen=True)
class EquityPoint:
    closed_at: datetime
    equity_after: float

    def __post_init__(self) -> None:
        _require_aware(self.closed_at, "equity point timestamp")
        if not isfinite(self.equity_after) or self.equity_after <= 0:
            raise ValueError("equity_after must be finite and positive")


@dataclass(frozen=True)
class FreqtradeProtectionState:
    cooldown_until: datetime | None = None
    drawdown_until: datetime | None = None
    equity_points: tuple[EquityPoint, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.cooldown_until, self.drawdown_until):
            if value is not None:
                _require_aware(value, "protection timestamp")
        if any(
            later.closed_at < earlier.closed_at
            for earlier, later in zip(self.equity_points, self.equity_points[1:], strict=False)
        ):
            raise ValueError("equity points must be ordered by close time")

    def entry_block_reason(self, timestamp: datetime) -> str | None:
        _require_aware(timestamp, "entry timestamp")
        if self.cooldown_until is not None and timestamp < self.cooldown_until:
            return "cooldown"
        if self.drawdown_until is not None and timestamp < self.drawdown_until:
            return "max_drawdown"
        return None


class FreqtradeResearchPort:
    """A per-instance, source-formula port with explicit order/fill lifecycle."""

    def __init__(
        self,
        metadata: FreqtradePortMetadata,
        *,
        profile_id: str | None = None,
        feature_matrix: DataFrame | None = None,
        parameters: Mapping[str, object] | None = None,
    ) -> None:
        self.metadata = metadata
        self.profile_id = profile_id
        self.feature_matrix = feature_matrix.copy(deep=True) if feature_matrix is not None else None
        self.parameters = MappingProxyType(dict(parameters or {}))
        self._profile = _catalog_profile(profile_id) if metadata.strategy_id == "btc_quant_catalog" else None
        self._sota_parameters = _sota_parameter_values(self.parameters) if metadata.strategy_id == "btc_sota" else {}
        self._multisource_parameters = (
            _multisource_parameter_values(self.parameters)
            if metadata.strategy_id == "btc_multisource_regime"
            else {}
        )
        self._fingerprint = _fingerprint(metadata, self._profile, self.parameters)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def decide(
        self,
        context: FreqtradeCausalFrame,
        *,
        equity: float,
        max_stake: float,
        min_stake: float = 0.0,
        proposed_stake: float | None = None,
        trade: FreqtradeTradeState | None = None,
        protection_state: FreqtradeProtectionState | None = None,
    ) -> FreqtradeDecision:
        if not all(isfinite(value) and value > 0 for value in (equity, max_stake)):
            raise ValueError("equity and max_stake must be finite and positive")
        if not isfinite(min_stake) or min_stake < 0:
            raise ValueError("min_stake must be finite and non-negative")
        if proposed_stake is not None and (not isfinite(proposed_stake) or proposed_stake <= 0):
            raise ValueError("proposed_stake must be finite and positive when supplied")
        self._validate_inputs(context.inputs)
        frame = self._evaluate(context.visible(), context.decision_at)
        last = frame.iloc[-1]
        orders: list[FreqtradeOrder] = []
        active_protections = protection_state or FreqtradeProtectionState()
        if (
            (trade is None or trade.is_closed)
            and active_protections.entry_block_reason(context.decision_at) is None
            and bool(last.get("enter_long", False))
        ):
            stake, stop = self._stake_and_stop(last, equity, max_stake, min_stake, proposed_stake)
            if stake > 0:
                close = float(last["close"])
                orders.append(
                    FreqtradeOrder(
                        "enter_long",
                        stake / close,
                        stop,
                        str(last["enter_tag"]),
                        stop_distance=close - stop,
                        order_id=self._order_id(context.decision_at, "enter_long", len(orders)),
                    )
                )
        elif trade is not None:
            close = _finite_positive(last.get("close"), "close")
            if self.roi_exit_reached(trade, close):
                orders.append(
                    FreqtradeOrder(
                        "exit_long",
                        trade.remaining_quantity,
                        None,
                        "minimal_roi",
                        order_id=self._order_id(context.decision_at, "exit_long", len(orders)),
                    )
                )
            elif bool(last.get("exit_long", False)):
                orders.append(
                    FreqtradeOrder(
                        "exit_long",
                        trade.remaining_quantity,
                        None,
                        str(last["exit_tag"]),
                        order_id=self._order_id(context.decision_at, "exit_long", len(orders)),
                    )
                )
            else:
                updated_stop = self._stop_for_open_trade(last, trade)
                if updated_stop is not None:
                    orders.append(
                        FreqtradeOrder(
                            "replace_stop",
                            trade.remaining_quantity,
                            updated_stop,
                            "atr_stop",
                            order_id=self._order_id(context.decision_at, "replace_stop", len(orders)),
                        )
                    )
        return FreqtradeDecision(
            self.metadata.strategy_id,
            context.decision_at,
            self.fingerprint,
            frame,
            context.inputs,
            tuple(orders),
            self.metadata.protections,
            active_protections,
        )


    def on_fill(
        self,
        order: FreqtradeOrder,
        *,
        order_id: str,
        fill_price: float,
        fill_quantity: float,
        filled_at: datetime,
        fees: float,
        trade: FreqtradeTradeState | None = None,
    ) -> FreqtradeTradeState:
        """Apply one actual partial fill, retaining fees on a zero-quantity closed state."""
        if not order.order_id or order_id != order.order_id:
            raise ValueError("fill order_id does not match the submitted native order")
        _require_aware(filled_at, "fill timestamp")
        for name, value in (("fill_price", fill_price), ("fill_quantity", fill_quantity)):
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isfinite(fees) or fees < 0:
            raise ValueError("fees must be finite and non-negative")
        prior_fill = 0.0 if trade is None else trade.filled_quantities.get(order_id, 0.0)
        if prior_fill + fill_quantity > order.quantity + 1e-12:
            raise ValueError("fills exceed submitted order quantity")
        if order.action == "enter_long":
            return self._apply_entry_fill(order, fill_price, fill_quantity, filled_at, fees, trade)
        if order.action == "exit_long":
            return self._apply_exit_fill(order, fill_quantity, fees, trade)
        raise ValueError("replace_stop is acknowledged through on_stop_accepted, not a fill")

    def on_stop_accepted(self, trade: FreqtradeTradeState, order: FreqtradeOrder) -> FreqtradeTradeState:
        """Persist a native cancel-and-replace acknowledgement without loosening."""
        if order.action != "replace_stop" or order.price is None:
            raise ValueError("only a priced replace_stop order can update an active stop")
        if trade.is_closed:
            raise ValueError("cannot replace a stop for a closed position")
        if order.price < trade.active_stop:
            raise ValueError("native stop must not loosen an accepted stop")
        return FreqtradeTradeState(
            trade.entry_price,
            trade.stake_amount,
            trade.initial_stop,
            order.price,
            trade.remaining_quantity,
            trade.opened_at,
            trade.total_fees,
            trade.filled_quantities,
        )

    def initial_stop_order(self, trade: FreqtradeTradeState, *, submitted_at: datetime) -> FreqtradeOrder:
        """Create the required native stop immediately after the first entry fill."""
        _require_aware(submitted_at, "stop submission timestamp")
        if trade.is_closed:
            raise ValueError("cannot create a stop for a closed position")
        return FreqtradeOrder(
            "replace_stop",
            trade.remaining_quantity,
            trade.active_stop,
            "initial_stoploss",
            order_id=self._order_id(submitted_at, "replace_stop", 0),
        )

    def on_trade_closed(
        self,
        state: FreqtradeProtectionState,
        *,
        closed_at: datetime,
        equity_after: float,
    ) -> FreqtradeProtectionState:
        """Apply the source cooldown and max-drawdown protection configuration."""
        _require_aware(closed_at, "trade close timestamp")
        if not isfinite(equity_after) or equity_after <= 0:
            raise ValueError("equity_after must be finite and positive")
        cooldown, drawdown = self._protection_parameters()
        if state.equity_points and closed_at < state.equity_points[-1].closed_at:
            raise ValueError("trade close timestamps must be monotonic")
        points = tuple(point for point in state.equity_points if point.closed_at >= closed_at - drawdown[0])
        points = (*points, EquityPoint(closed_at, equity_after))
        drawdown_until = state.drawdown_until
        if len(points) >= drawdown[1]:
            peak = max(point.equity_after for point in points)
            current_drawdown = (peak - equity_after) / peak
            if current_drawdown >= drawdown[3]:
                drawdown_until = closed_at + drawdown[2]
        return FreqtradeProtectionState(closed_at + cooldown, drawdown_until, points)

    def roi_exit_reached(self, trade: FreqtradeTradeState, current_rate: float) -> bool:
        current_rate = _finite_positive(current_rate, "current_rate")
        minimum_profit = float(self.metadata.minimal_roi["0"])
        return current_rate / trade.entry_price - 1 >= minimum_profit

    def order_intents(self, decision: FreqtradeDecision) -> tuple[OrderIntent, ...]:
        intents: list[OrderIntent] = []
        for order in decision.orders:
            if order.action == "enter_long":
                intents.append(
                    OrderIntent(
                        instrument=InstrumentKind.SPOT,
                        side=OrderSide.BUY,
                        order_type=OrderType.MARKET,
                        base_quantity=Decimal(str(order.quantity)),
                        reason=order.reason,
                    )
                )
            elif order.action == "exit_long":
                intents.append(
                    OrderIntent(
                        instrument=InstrumentKind.SPOT,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        base_quantity=Decimal(str(order.quantity)),
                        reason=order.reason,
                    )
                )
            else:
                raise ValueError("replace_stop requires native cancel-and-replace lifecycle support")
        return tuple(intents)

    def _validate_inputs(self, inputs: Sequence[DataVersion]) -> None:
        actual = {item.dataset_id for item in inputs}
        missing = [item.dataset_id for item in self.metadata.required_data if item.dataset_id not in actual]
        if missing:
            raise MissingFreqtradeDataError(
                f"{self.metadata.strategy_id} requires {', '.join(missing)}; no zero-filled substitute is allowed"
            )

    def _evaluate(self, frame: DataFrame, decision_at: datetime) -> DataFrame:
        if len(frame) < self.metadata.startup_candles:
            return frame.assign(enter_long=False, exit_long=False)
        strategy = self.metadata.strategy_id
        if strategy == "btc_quant_catalog":
            return _evaluate_catalog(frame, _require_profile(self._profile))
        if strategy == "btc_atr_fear_volume":
            return _evaluate_fear_volume(frame, self.parameters)
        if strategy == "btc_donchian_atr":
            return _evaluate_donchian(frame, self.parameters)
        if strategy == "btc_low_freq_trend":
            return _evaluate_low_freq(frame, self.parameters)
        if strategy == "btc_multisource_regime":
            return _evaluate_multisource(frame, self._require_feature_matrix(decision_at), self._multisource_parameters)
        if strategy == "btc_sota":
            return _evaluate_sota(frame, self._require_feature_matrix(decision_at), self._sota_parameters)
        raise KeyError(f"unknown freqtrade port: {strategy}")

    def _stake_and_stop(
        self,
        row: pd.Series,
        equity: float,
        max_stake: float,
        min_stake: float,
        proposed_stake: float | None,
    ) -> tuple[float, float]:
        close = _finite_positive(row.get("close"), "close")
        atr = _finite_positive(row.get("atr"), "atr")
        risk_fraction, atr_multiple = self._risk_parameters()
        stop = source_risk.calculate_atr_stop(close, atr, atr_multiple)
        if self.metadata.strategy_id == "btc_low_freq_trend":
            if proposed_stake is None:
                raise MissingFreqtradeDataError(
                    "btc_low_freq_trend has no source custom stake callback; native proposed_stake is required"
                )
            if proposed_stake > max_stake:
                raise ValueError("native proposed_stake exceeds max_stake")
            return proposed_stake, stop
        size = source_risk.calculate_position_size(
            equity=equity,
            entry_price=close,
            stop_price=stop,
            risk_fraction=risk_fraction,
            max_position_value=max_stake,
            min_position_value=min_stake,
        ).stake_amount
        if self.metadata.strategy_id == "btc_sota":
            realized_vol = _finite_positive(row.get("realized_vol"), "realized_vol")
            volatility_cap = equity * float(self._sota_parameters["target_annual_vol"]) / realized_vol
            size = max(0.0, min(size, volatility_cap, max_stake))
            if size < min_stake:
                size = 0.0
        return size, stop

    def _stop_for_open_trade(self, row: pd.Series, trade: FreqtradeTradeState) -> float | None:
        close = _finite_positive(row.get("close"), "close")
        if self.metadata.strategy_id == "btc_sota":
            source_stop = trade.initial_stop
        else:
            atr = _finite_positive(row.get("atr"), "atr")
            _risk_fraction, atr_multiple = self._risk_parameters()
            source_stop = max(close - atr * atr_multiple, trade.entry_price - atr * atr_multiple)
        candidate = max(source_stop, self._hard_stop(trade.entry_price), trade.active_stop)
        if candidate <= trade.active_stop or candidate >= close:
            return None
        return candidate

    def _apply_entry_fill(
        self,
        order: FreqtradeOrder,
        fill_price: float,
        fill_quantity: float,
        filled_at: datetime,
        fees: float,
        trade: FreqtradeTradeState | None,
    ) -> FreqtradeTradeState:
        if order.stop_distance is None:
            raise ValueError("entry order requires a source-calculated ATR stop distance")
        if trade is not None and trade.opened_at > filled_at:
            raise ValueError("entry fill timestamp precedes the existing position")
        prior_quantity = 0.0 if trade is None else trade.remaining_quantity
        prior_notional = 0.0 if trade is None else trade.stake_amount
        total_quantity = prior_quantity + fill_quantity
        total_notional = prior_notional + fill_price * fill_quantity
        entry_price = total_notional / total_quantity
        source_initial = entry_price - order.stop_distance
        if source_initial <= 0 or source_initial >= entry_price:
            raise ValueError("entry fill cannot produce a valid source ATR stop")
        hard_stop = self._hard_stop(entry_price)
        active_stop = max(source_initial, hard_stop, trade.active_stop if trade is not None else 0.0)
        fills = dict(trade.filled_quantities) if trade is not None else {}
        fills[order.order_id] = fills.get(order.order_id, 0.0) + fill_quantity
        return FreqtradeTradeState(
            entry_price,
            total_notional,
            source_initial,
            active_stop,
            total_quantity,
            filled_at if trade is None else trade.opened_at,
            fees + (0.0 if trade is None else trade.total_fees),
            MappingProxyType(fills),
        )

    def _apply_exit_fill(
        self,
        order: FreqtradeOrder,
        fill_quantity: float,
        fees: float,
        trade: FreqtradeTradeState | None,
    ) -> FreqtradeTradeState:
        if trade is None:
            raise ValueError("exit fill requires an open position")
        remaining = trade.remaining_quantity - fill_quantity
        if remaining < -1e-12:
            raise ValueError("exit fill exceeds the remaining position quantity")
        fills = dict(trade.filled_quantities)
        fills[order.order_id] = fills.get(order.order_id, 0.0) + fill_quantity
        if remaining <= 1e-12:
            return FreqtradeTradeState(
                trade.entry_price,
                0.0,
                trade.initial_stop,
                trade.active_stop,
                0.0,
                trade.opened_at,
                trade.total_fees + fees,
                MappingProxyType(fills),
            )
        return FreqtradeTradeState(
            trade.entry_price,
            trade.entry_price * remaining,
            trade.initial_stop,
            trade.active_stop,
            remaining,
            trade.opened_at,
            trade.total_fees + fees,
            MappingProxyType(fills),
        )

    def _hard_stop(self, entry_price: float) -> float:
        return entry_price * (1.0 + self.metadata.hard_stoploss)

    def _protection_parameters(self) -> tuple[timedelta, tuple[timedelta, int, timedelta, float]]:
        cooldown = next(item for item in self.metadata.protections if item["method"] == "CooldownPeriod")
        drawdown = next(item for item in self.metadata.protections if item["method"] == "MaxDrawdown")
        return (
            timedelta(hours=4 * int(cast(int, cooldown["stop_duration_candles"]))),
            (
                timedelta(hours=4 * int(cast(int, drawdown["lookback_period_candles"]))),
                int(cast(int, drawdown["trade_limit"])),
                timedelta(hours=4 * int(cast(int, drawdown["stop_duration_candles"]))),
                float(cast(float, drawdown["max_allowed_drawdown"])),
            ),
        )

    def _order_id(self, timestamp: datetime, action: str, ordinal: int) -> str:
        return f"{self.fingerprint}:{timestamp.isoformat()}:{action}:{ordinal}"

    def _risk_parameters(self) -> tuple[float, float]:
        strategy = self.metadata.strategy_id
        if strategy == "btc_quant_catalog":
            profile = _require_profile(self._profile)
            risk = cast(Mapping[str, object], profile["risk"])
            return _parameter(risk, "risk_per_trade", 0.005), _parameter(risk, "atr_multiple", 2.5)
        if strategy == "btc_atr_fear_volume":
            return _parameter(self.parameters, "risk_per_trade", 0.005), _parameter(self.parameters, "atr_multiple", 2.8)
        if strategy == "btc_donchian_atr":
            return _parameter(self.parameters, "risk_per_trade", 0.005), _parameter(self.parameters, "atr_multiple", 2.5)
        if strategy == "btc_low_freq_trend":
            return 0.005, _parameter(self.parameters, "atr_multiple", 3.2)
        if strategy == "btc_multisource_regime":
            return (
                _parameter(self._multisource_parameters, "risk_fraction", 0.005),
                _parameter(self._multisource_parameters, "atr_multiple", 2.5),
            )
        return float(self._sota_parameters["risk_per_trade"]), float(self._sota_parameters["atr_multiple"])

    def _require_feature_matrix(self, decision_at: datetime) -> DataFrame:
        if self.feature_matrix is None:
            raise MissingFreqtradeDataError("multisource_feature_matrix is required for this strategy")
        matrix = self.feature_matrix.copy(deep=True)
        required = set(multisource_features.REQUIRED_CONTRACT_COLUMNS)
        missing = sorted(required - set(matrix.columns))
        if missing:
            raise MissingFreqtradeDataError(
                f"multisource_feature_matrix missing causal contract columns: {', '.join(missing)}"
            )
        matrix["date"] = pd.to_datetime(matrix["date"], utc=True, errors="raise")
        availability_columns = [column for column in matrix.columns if column == "available_at" or column.endswith("_available_at")]
        for column in availability_columns:
            matrix[column] = pd.to_datetime(matrix[column], utc=True, errors="raise")
            if matrix[column].isna().any():
                raise MissingFreqtradeDataError(f"multisource_feature_matrix has missing {column}")
        for column in multisource_features.REQUIRED_STRATEGY_COLUMNS[:5]:
            values = pd.to_numeric(matrix[column], errors="coerce")
            if values.isna().any() or not values.map(isfinite).all():
                raise MissingFreqtradeDataError(f"multisource_feature_matrix has invalid {column}")
            matrix[column] = values
        for column in multisource_features.REQUIRED_STRATEGY_COLUMNS[5:]:
            if matrix[column].isna().any() or not pd.api.types.is_bool_dtype(matrix[column]):
                raise MissingFreqtradeDataError(f"multisource_feature_matrix has invalid {column}")
        active_date = pd.Timestamp(decision_at)
        active = matrix["date"] == active_date
        if not active.any():
            raise MissingFreqtradeDataError("multisource_feature_matrix has no row for the active candle")
        for column in availability_columns:
            if (matrix.loc[active, column] > active_date).any():
                raise MissingFreqtradeDataError(
                    f"multisource_feature_matrix {column} is unavailable at the decision timestamp"
                )
        return matrix


class FreqtradeNativeStrategy:
    """Native-only shell preserving a source port's own order lifecycle.

    ``on_bar`` intentionally cannot produce generic ``OrderIntent`` values:
    Freqtrade's stop replacement and partial-fill state belong to
    ``FreqtradeNativeIntentRouter``. The Nautilus adapter recognizes this
    concrete type and invokes ``native_decision`` at each completed bar.
    """

    def __init__(
        self,
        port: FreqtradeResearchPort,
        *,
        dataset_version: str,
    ) -> None:
        if not dataset_version:
            raise ValueError("source-native strategy requires a dataset version")
        unsupported = [
            dependency.dataset_id
            for dependency in port.metadata.required_data
            if dependency.dataset_id != "ohlcv_4h"
        ]
        if unsupported:
            raise MissingFreqtradeDataError(
                f"{port.metadata.strategy_id} needs versioned external inputs "
                f"({', '.join(unsupported)}); this OHLCV-only native path will not zero-fill them"
            )
        self.port = port
        self._dataset_version = dataset_version
        self.parameters = MappingProxyType(dict(port.parameters))
        try:
            self.metadata = StrategyMetadata(
                id=f"native_{port.metadata.strategy_id}",
                version=port.metadata.source.commit,
                description=(
                    "Source-faithful Freqtrade port executed through the native "
                    "order/fill/stop lifecycle."
                ),
                warmup_bars=port.metadata.startup_candles,
                supported_timeframes=("4h",),
                supported_instruments=(InstrumentKind.SPOT,),
                requires_full_history=False,
            )
        except ValueError as error:
            raise FreqtradeRuntimeUnavailableError(
                "btc_backtest must add the source-native 4h timeframe to StrategyMetadata "
                "and DataRequest before Freqtrade ports can enter Nautilus execution"
            ) from error

    def initialize(self, context: InitializationContext) -> None:
        if context.spec.data.timeframe != "4h":
            raise ValueError(
                f"{self.port.metadata.strategy_id} requires a 4h source dataset, "
                f"got {context.spec.data.timeframe}"
            )

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        del context
        raise RuntimeError(
            "Freqtrade source orders require the Nautilus FreqtradeNativeIntentRouter; "
            "generic OrderIntent execution is prohibited"
        )

    def finalize(self, context: FinalizationContext) -> None:
        del context

    def native_decision(
        self,
        *,
        timestamp: datetime,
        bars: DataFrame,
        equity: Decimal,
        trade: FreqtradeTradeState | None,
        protection_state: FreqtradeProtectionState,
    ) -> FreqtradeDecision:
        """Build a source decision only from completed bars and actual state."""

        if equity <= 0 or not equity.is_finite():
            raise ValueError("native portfolio equity must be finite and positive")
        visible = bars.loc[:pd.Timestamp(timestamp)]
        if visible.empty:
            raise ValueError("native source decision has no completed causal bar")
        causal = FreqtradeCausalFrame(
            candles=visible,
            decision_at=timestamp,
            available_at=timestamp,
            inputs=(DataVersion("ohlcv_4h", self._dataset_version, timestamp),),
        )
        return self.port.decide(
            causal,
            equity=float(equity),
            max_stake=float(equity),
            trade=trade,
            protection_state=protection_state,
        )


def create_freqtrade_port(
    strategy_id: str,
    *,
    profile_id: str | None = None,
    feature_matrix: DataFrame | None = None,
    parameters: Mapping[str, object] | None = None,
) -> FreqtradeResearchPort:
    metadata = FREQTRADE_PORTS.get(strategy_id)
    if metadata is None:
        raise KeyError(f"unknown freqtrade research port: {strategy_id}")
    if strategy_id == "btc_quant_catalog" and profile_id is None:
        raise ValueError("btc_quant_catalog requires an explicit profile_id")
    return FreqtradeResearchPort(
        metadata,
        profile_id=profile_id,
        feature_matrix=feature_matrix,
        parameters=parameters,
    )


def _evaluate_catalog(frame: DataFrame, profile: Mapping[str, object]) -> DataFrame:
    enriched = catalog_signals.add_catalog_indicators(frame)
    entry = _combine_catalog(enriched, profile, "entry_signals", require_all=True)
    exit_ = _combine_catalog(enriched, profile, "exit_signals", require_all=False)
    profile_id = str(profile["id"])
    catalog_hash = str(_catalog()["sha256"])
    return enriched.assign(
        enter_long=entry,
        enter_tag=entry.map(lambda value: f"catalog:{profile_id}:{catalog_hash[:12]}" if value else ""),
        exit_long=exit_,
        exit_tag=exit_.map(lambda value: f"catalog_exit:{profile_id}" if value else ""),
    )


def _combine_catalog(frame: DataFrame, profile: Mapping[str, object], key: str, *, require_all: bool) -> pd.Series:
    raw_signal_ids = cast(Sequence[object], profile[key])
    signal_ids = tuple(str(item) for item in raw_signal_ids)
    combined: pd.Series | None = None
    params = cast(Mapping[str, object], profile["signal_params"])
    for signal_id in signal_ids:
        source_params = cast(Mapping[str, object], params.get(signal_id, {}))
        signal = catalog_signals.evaluate_signal(signal_id, frame, source_params)
        if combined is None:
            combined = signal
        elif require_all:
            combined = combined & signal
        else:
            combined = combined | signal
    return combined.fillna(False) if combined is not None else pd.Series(False, index=frame.index)


def _evaluate_fear_volume(frame: DataFrame, parameters: Mapping[str, object]) -> DataFrame:
    ta = _talib()
    enriched = frame.copy()
    enriched["atr"] = ta.ATR(enriched, timeperiod=14)
    enriched = fear_volume_indicators.add_fear_volume_features(
        enriched,
        fear_window=int(_parameter(parameters, "fear_window", 90)),
        volume_window=int(_parameter(parameters, "volume_window", 30)),
    )
    entry = fear_volume_indicators.fear_volume_entry_signal(
        enriched,
        min_drawdown=_parameter(parameters, "fear_drawdown_threshold", 0.12),
        min_volume_ratio=_parameter(parameters, "volume_spike_multiple", 1.5),
        min_atr_pct=_parameter(parameters, "atr_pct_min", 0.02),
    )
    exit_ = (enriched["volume"] > 0) & (enriched["fear_drawdown"] <= _parameter(parameters, "fear_recovery_threshold", 0.04))
    return enriched.assign(enter_long=entry, enter_tag="atr_fear_volume_reversal", exit_long=exit_, exit_tag="fear_recovered")


def _evaluate_donchian(frame: DataFrame, parameters: Mapping[str, object]) -> DataFrame:
    ta = _talib()
    window = int(_parameter(parameters, "donchian_window", 55))
    exit_window = int(_parameter(parameters, "exit_window", 20))
    enriched = frame.copy()
    enriched["atr"] = ta.ATR(enriched, timeperiod=14)
    enriched["adx"] = ta.ADX(enriched, timeperiod=14)
    enriched["donchian_upper"] = enriched["high"].rolling(window).max().shift(1)
    enriched["donchian_lower"] = enriched["low"].rolling(window).min().shift(1)
    enriched["donchian_exit"] = enriched["low"].rolling(exit_window).min().shift(1)
    enriched["donchian_mid"] = (enriched["donchian_upper"] + enriched["donchian_lower"]) / 2
    enriched["atr_pct"] = enriched["atr"] / enriched["close"]
    entry = (
        (enriched["volume"] > 0)
        & (enriched["close"] > enriched["donchian_upper"])
        & (enriched["adx"] >= int(_parameter(parameters, "adx_threshold", 25)))
        & (enriched["atr_pct"] > 0.005)
        & (enriched["atr_pct"] < 0.12)
    )
    exit_ = (enriched["volume"] > 0) & ((enriched["close"] < enriched["donchian_exit"]) | (enriched["close"] < enriched["donchian_mid"]))
    return enriched.assign(enter_long=entry, enter_tag="donchian_breakout_atr_adx", exit_long=exit_, exit_tag="donchian_exit")


def _evaluate_low_freq(frame: DataFrame, parameters: Mapping[str, object]) -> DataFrame:
    ta = _talib()
    enriched = frame.copy()
    fast = int(_parameter(parameters, "fast_ma_window", 300))
    slow = int(_parameter(parameters, "slow_ma_window", 1200))
    breakout = int(_parameter(parameters, "breakout_window", 330))
    exit_ma = int(_parameter(parameters, "exit_ma_window", 300))
    exit_low = int(_parameter(parameters, "exit_low_window", 120))
    enriched["atr"] = ta.ATR(enriched, timeperiod=14)
    enriched["adx"] = ta.ADX(enriched, timeperiod=14)
    enriched["fast_ma"] = enriched["close"].rolling(fast).mean()
    enriched["slow_ma"] = enriched["close"].rolling(slow).mean()
    enriched["breakout_high"] = enriched["high"].rolling(breakout).max().shift(1)
    enriched["exit_ma"] = enriched["close"].rolling(exit_ma).mean()
    enriched["exit_low"] = enriched["low"].rolling(exit_low).min().shift(1)
    enriched["atr_pct"] = enriched["atr"] / enriched["close"]
    enriched["volume_mean"] = enriched["volume"].rolling(90).mean()
    enriched["volume_ratio"] = enriched["volume"] / enriched["volume_mean"]
    entry = (
        (enriched["volume"] > 0)
        & (enriched["close"] > enriched["slow_ma"])
        & (enriched["fast_ma"] > enriched["slow_ma"])
        & (enriched["close"] > enriched["breakout_high"])
        & (enriched["volume_ratio"] >= _parameter(parameters, "volume_ratio_min", 0.7))
        & (enriched["atr_pct"] >= _parameter(parameters, "min_atr_pct", 0.005))
        & (enriched["atr_pct"] <= _parameter(parameters, "max_atr_pct", 0.12))
    )
    exit_ = (enriched["volume"] > 0) & ((enriched["close"] < enriched["exit_ma"]) | (enriched["close"] < enriched["exit_low"]))
    return enriched.assign(enter_long=entry, enter_tag="low_freq_trend_breakout", exit_long=exit_, exit_tag="low_freq_trend_exit")


def _evaluate_multisource(
    frame: DataFrame,
    feature_matrix: DataFrame,
    parameters: Mapping[str, object],
) -> DataFrame:
    enriched = multisource_features.merge_feature_matrix(frame, feature_matrix)
    if enriched["technical_score"].isna().iloc[-1]:
        raise MissingFreqtradeDataError("multisource_feature_matrix has no causal row for the active candle")
    enriched["atr"] = BtcMultiSourceRegimeStrategySource._atr(enriched)
    weights = cast(Mapping[str, float], parameters["feature_weights"])
    scores = ("technical_score", "derivatives_score", "onchain_score", "sentiment_score", "liquidity_score")
    enriched["entry_score"] = pd.concat([enriched[name] * weights[name.removesuffix("_score")] for name in scores], axis=1).sum(axis=1, min_count=len(scores))
    entry = (enriched["entry_score"] >= parameters["entry_threshold"]) & ~enriched["liquidity_bad"].fillna(True) & ~enriched["leverage_crowded"].fillna(True) & (enriched["volume"] > 0)
    exit_ = (enriched["entry_score"] < parameters["exit_threshold"]) | enriched["liquidity_bad"].fillna(True) | enriched["leverage_crowded"].fillna(True)
    return enriched.assign(enter_long=entry, enter_tag="multisource_regime", exit_long=exit_, exit_tag="multisource_regime_exit")


def _evaluate_sota(frame: DataFrame, feature_matrix: DataFrame, parameters: Mapping[str, float | int]) -> DataFrame:
    merged = multisource_features.merge_feature_matrix(frame, feature_matrix)
    if merged["technical_score"].isna().iloc[-1]:
        raise MissingFreqtradeDataError("multisource_feature_matrix has no causal row for the active candle")
    enriched = sota_indicators.add_sota_indicators(merged)
    entry = (
        enriched["feature_row_complete"].fillna(False)
        & (enriched["volume"] > 0)
        & (enriched["close"] > enriched["ema_regime"])
        & (enriched["close"] > enriched["donchian_entry"])
        & (enriched["momentum_score"] >= float(parameters["momentum_entry_threshold"]))
        & (enriched["external_score"] >= float(parameters["external_entry_threshold"]))
        & (enriched["external_confirmations"].fillna(0) >= int(parameters["min_external_confirmations"]))
        & ~enriched["liquidity_bad"].fillna(True)
        & ~enriched["leverage_crowded"].fillna(True)
    ).fillna(False)
    exit_ = (~enriched["feature_row_complete"].fillna(False) | (enriched["close"] < enriched["ema_regime"]) | (enriched["close"] < enriched["donchian_exit"]) | (enriched["momentum_score"] <= 0) | enriched["liquidity_bad"].fillna(True)).fillna(True)
    return enriched.assign(enter_long=entry, enter_tag="sota_trend_regime", exit_long=exit_, exit_tag="sota_regime_exit")


def _catalog() -> Mapping[str, object]:
    return legacy_catalog()


def _catalog_profile(profile_id: str | None) -> Mapping[str, object]:
    if not profile_id:
        raise ValueError("catalog profile_id is required")
    for profile in cast(Sequence[Mapping[str, object]], _catalog()["profiles"]):
        if profile.get("id") == profile_id:
            if profile.get("pair") != "BTC/USDT" or profile.get("timeframe") != "4h":
                raise ValueError(f"unsupported catalog profile pair/timeframe: {profile_id}")
            return MappingProxyType(dict(profile))
    raise ValueError(f"unknown catalog profile: {profile_id}")


def _sota_parameter_values(parameters: Mapping[str, object]) -> Mapping[str, float | int]:
    defaults: dict[str, float | int] = {
        "risk_per_trade": 0.005,
        "atr_multiple": 3.0,
        "target_annual_vol": 0.25,
        "momentum_entry_threshold": 0.5,
        "external_entry_threshold": 0.0,
        "min_external_confirmations": 2,
    }
    candidate = parameters.get("candidate")
    if candidate is None:
        return MappingProxyType(defaults)
    if not isinstance(candidate, Mapping):
        raise ValueError("Sota candidate must be a mapping")
    return MappingProxyType(sota_params.validate_sota_candidate_parameters(dict(candidate)))


def _multisource_parameter_values(parameters: Mapping[str, object]) -> Mapping[str, object]:
    values: dict[str, object] = {
        "feature_weights": {
            "technical": 1.0,
            "derivatives": 1.0,
            "onchain": 1.0,
            "sentiment": 1.0,
            "liquidity": 1.0,
        },
        "entry_threshold": 1.0,
        "exit_threshold": 0.2,
        "atr_multiple": 2.5,
        "risk_fraction": 0.005,
    }
    candidate = parameters.get("candidate")
    if candidate is None:
        return MappingProxyType(values)
    if not isinstance(candidate, Mapping):
        raise ValueError("multisource candidate must be a mapping")
    weights = candidate.get("feature_weights")
    if not isinstance(weights, Mapping):
        raise ValueError("candidate config feature_weights must be a mapping")
    merged_weights = dict(cast(Mapping[str, float], values["feature_weights"]))
    for name, raw in weights.items():
        if name not in merged_weights:
            raise ValueError(f"candidate config has unsupported feature weight: {name}")
        merged_weights[name] = BtcMultiSourceRegimeStrategySource._positive_number(raw, f"feature_weights.{name}")
    values["feature_weights"] = MappingProxyType(merged_weights)
    for target, source_name in (("entry_threshold", "entry_threshold"), ("exit_threshold", "exit_threshold"), ("atr_multiple", "atr_multiple"), ("risk_fraction", "risk_fraction")):
        if source_name in candidate:
            values[target] = BtcMultiSourceRegimeStrategySource._positive_number(candidate[source_name], source_name)
    return MappingProxyType(values)


def _fingerprint(metadata: FreqtradePortMetadata, profile: Mapping[str, object] | None, parameters: Mapping[str, object]) -> str:
    profile_identity = "" if profile is None else f":{profile['id']}:{_catalog()['sha256']}"
    parameter_identity = repr(sorted(parameters.items()))
    return f"{metadata.source.commit}:{metadata.strategy_id}{profile_identity}:{parameter_identity}"


def _parameter(parameters: Mapping[str, object], name: str, default: float) -> float:
    value = parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite numeric")
    return float(value)


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value) or value <= 0:
        raise MissingFreqtradeDataError(f"{name} is missing or non-positive")
    return float(value)


def _require_profile(profile: Mapping[str, object] | None) -> Mapping[str, object]:
    if profile is None:
        raise ValueError("catalog profile is required")
    return profile


def _talib() -> TalibAbstract:
    try:
        import talib.abstract as ta
    except ModuleNotFoundError as error:
        raise FreqtradeRuntimeUnavailableError(
            "TA-Lib is required for btc_atr_fear_volume, btc_donchian_atr, and btc_low_freq_trend; "
            "the source formulas will not be approximated"
        ) from error
    return cast(TalibAbstract, ta)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
