"""Causal deterministic long/flat reference strategies for V2 experiments.

These are simple benchmark rules, not validated alpha. Deployment approval,
position sizing, cash availability, costs and execution remain outside this
module. Each instance is a single-writer actor; persist ``snapshot_state`` or
replay its immutable input prefix when reconstructing it after a restart.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field

from qt.contracts import Contract, MarketEvent, SignalIntent, StrategyState, content_hash
from qt.contracts.base import NonEmptyStr, NonNegativeDecimal, PositiveDecimal, Probability
from qt.marketdata import ClosedBar


class EventStrategyParameters(Contract):
    deployment_id: NonEmptyStr
    venue: NonEmptyStr
    instrument: NonEmptyStr
    position_size: PositiveDecimal
    lookback: int = Field(default=20, ge=2, le=10_000)
    threshold_bps: PositiveDecimal = Decimal("100")
    signal_ttl_ns: int = Field(default=60_000_000_000, gt=0)
    maximum_bar_delay_ns: int = Field(default=60_000_000_000, ge=0)
    maximum_holding_bars: int = Field(default=20, ge=1)
    minimum_lower_wick_fraction: Probability = Decimal("0.5")
    minimum_depth_recovery: PositiveDecimal = Decimal("1")


class _ReferenceStrategy:
    name: Literal["momentum", "mean_reversion", "wick_orderflow"]

    def __init__(self, parameters: EventStrategyParameters) -> None:
        self.parameters = parameters
        self._closes: deque[Decimal] = deque(maxlen=parameters.lookback)
        self._last_close_ns: int | None = None
        self._target = Decimal("0")
        self._held_bars = 0

    def snapshot_state(self) -> StrategyState:
        return StrategyState.model_validate({"values": {
            "strategy": self.name, "parameters_hash": self.parameters.content_hash(),
            "closes": [str(value) for value in self._closes],
            "last_close_ns": self._last_close_ns, "target": str(self._target),
            "held_bars": self._held_bars,
        }})

    def restore_state(self, state: StrategyState) -> None:
        values = state.values
        if (values.get("strategy") != self.name
                or values.get("parameters_hash") != self.parameters.content_hash()):
            raise ValueError("strategy state is bound to different rules or parameters")
        closes = values.get("closes")
        if not isinstance(closes, tuple) or len(closes) > self.parameters.lookback:
            raise ValueError("invalid strategy window")
        # Use the same strict financial validator as the public contracts.
        restored = [self._positive(value) for value in closes]
        target = self._nonnegative(values.get("target"))
        if target not in (Decimal("0"), self.parameters.position_size):
            raise ValueError("invalid strategy target")
        last_close_ns, held = values.get("last_close_ns"), values.get("held_bars")
        if (last_close_ns is not None and (type(last_close_ns) is not int or last_close_ns < 0)):
            raise ValueError("invalid strategy close time")
        if type(held) is not int or held < 0:
            raise ValueError("invalid holding age")
        self._closes = deque(restored, maxlen=self.parameters.lookback)
        self._last_close_ns = last_close_ns
        self._target, self._held_bars = target, held

    @staticmethod
    def _positive(value: object) -> Decimal:
        from pydantic import TypeAdapter

        return TypeAdapter(PositiveDecimal).validate_python(value, strict=True)

    @staticmethod
    def _nonnegative(value: object) -> Decimal:
        from pydantic import TypeAdapter

        return TypeAdapter(NonNegativeDecimal).validate_python(value, strict=True)

    @staticmethod
    def _bar(event: MarketEvent) -> ClosedBar:
        payload = event.payload
        required = ("open_ts_ns", "close_ts_ns", "open", "high", "low", "close", "volume")
        if any(key not in payload for key in required):
            raise ValueError("strategy requires an explicit closed OHLCV bar")
        close_ns = payload["close_ts_ns"]
        if type(close_ns) is not int or close_ns > event.exchange_ts_ns:
            raise ValueError("forming or future bar is not observable")
        return ClosedBar.model_validate({
            "instrument": event.instrument, **{key: payload[key] for key in required},
            "available_at_ns": event.available_at_ns if event.available_at_ns is not None else close_ns,
            "received_at_ns": event.received_ts_ns,
        })

    def _decision(self, bar: ClosedBar, event: MarketEvent) -> tuple[Decimal, str]:
        raise NotImplementedError

    def on_event(self, event: MarketEvent, state: StrategyState) -> list[SignalIntent]:
        if state.values:
            self.restore_state(state)
        if (event.venue != self.parameters.venue or event.instrument != self.parameters.instrument
                or event.event_type != "bar"):
            return []
        if any(flag in {"BOOK_INVALID", "SEQUENCE_GAP", "STALE", "CLOCK_INVALID"}
               for flag in event.quality_flags):
            return []
        bar = self._bar(event)
        if event.received_ts_ns - bar.close_ts_ns > self.parameters.maximum_bar_delay_ns:
            return []
        if self._last_close_ns is not None and bar.close_ts_ns <= self._last_close_ns:
            return []  # duplicate/revised historical rows never rewrite the consumed prefix
        if self._last_close_ns is not None and bar.open_ts_ns != self._last_close_ns:
            raise ValueError("strategy bar sequence has a gap or overlap; resync required")
        if self._target > 0:
            self._held_bars += 1
        with localcontext() as context:
            context.prec = 38
            target, reason = self._decision(bar, event)
        if self._target > 0 and self._held_bars >= self.parameters.maximum_holding_bars:
            target, reason = Decimal("0"), "maximum_holding_period"
        self._closes.append(bar.close)
        self._last_close_ns = bar.close_ts_ns
        if target == self._target:
            return []
        self._target = target
        self._held_bars = 0
        identity = content_hash({
            "strategy": self.name, "parameters": self.parameters.content_hash(),
            "close_ns": bar.close_ts_ns, "target": str(target),
        })
        return [SignalIntent(
            signal_id=identity, deployment_id=self.parameters.deployment_id,
            event_id=event.content_hash(), instrument=event.instrument, target_position=target,
            produced_at_ns=event.received_ts_ns,
            valid_until_ns=event.received_ts_ns + self.parameters.signal_ttl_ns,
            reason_codes=(self.name, reason),
        )]


class MomentumStrategy(_ReferenceStrategy):
    """Enter on a close above a prior-window baseline; exit on nonpositive momentum."""

    name = "momentum"

    def _decision(self, bar: ClosedBar, event: MarketEvent) -> tuple[Decimal, str]:
        if len(self._closes) < self.parameters.lookback:
            return self._target, "warmup"
        change_bps = (bar.close / self._closes[0] - 1) * 10_000
        if change_bps >= self.parameters.threshold_bps:
            return self.parameters.position_size, "positive_momentum"
        if change_bps <= 0:
            return Decimal("0"), "momentum_exit"
        return self._target, "hold"


class MeanReversionStrategy(_ReferenceStrategy):
    """Enter below the prior-window mean; exit on a return to that mean."""

    name = "mean_reversion"

    def _decision(self, bar: ClosedBar, event: MarketEvent) -> tuple[Decimal, str]:
        if len(self._closes) < self.parameters.lookback:
            return self._target, "warmup"
        mean = sum(self._closes, Decimal("0")) / len(self._closes)
        deviation_bps = (bar.close / mean - 1) * 10_000
        if deviation_bps <= -self.parameters.threshold_bps:
            return self.parameters.position_size, "negative_deviation"
        if deviation_bps >= 0:
            return Decimal("0"), "reversion_exit"
        return self._target, "hold"


class WickOrderFlowStrategy(_ReferenceStrategy):
    """Lower-wick rebound confirmed by observed flow and valid depth features.

    ``buy_volume``, ``sell_volume`` and ``depth_recovery_ratio`` must come from
    the deterministic feature pipeline over the closed bar's information
    interval. Missing or invalid book features never fall back to OHLC alone.
    """

    name = "wick_orderflow"

    def _decision(self, bar: ClosedBar, event: MarketEvent) -> tuple[Decimal, str]:
        if self._target > 0:
            if bar.close < bar.open:
                return Decimal("0"), "rebound_failed"
            return self._target, "hold"
        payload = event.payload
        if payload.get("book_valid") is not True or not {
            "buy_volume", "sell_volume", "depth_recovery_ratio", "features_available_at_ns",
        } <= payload.keys():
            return Decimal("0"), "missing_orderflow_confirmation"
        available = payload["features_available_at_ns"]
        if type(available) is not int or available > event.received_ts_ns or available < bar.close_ts_ns:
            raise ValueError("orderflow features are not causal for the decision")
        buy_volume = self._nonnegative(payload["buy_volume"])
        sell_volume = self._nonnegative(payload["sell_volume"])
        recovery = self._positive(payload["depth_recovery_ratio"])
        spread = bar.high - bar.low
        wick = min(bar.open, bar.close) - bar.low
        if (spread > 0 and wick / spread >= self.parameters.minimum_lower_wick_fraction
                and bar.close >= bar.open and buy_volume > sell_volume
                and recovery >= self.parameters.minimum_depth_recovery):
            return self.parameters.position_size, "wick_flow_depth_confirmed"
        return Decimal("0"), "no_confirmation"


REFERENCE_STRATEGIES = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "wick_orderflow": WickOrderFlowStrategy,
}
