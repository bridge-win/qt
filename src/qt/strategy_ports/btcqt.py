"""Causal, stateful adapters for the original btc-qt S0/S1/S2 strategies.

This module intentionally calls the migrated ``qt.legacy.btcqt`` strategy
classes.  It does not restate their rules in target weights or generic
threshold expressions.  The native-engine integration must drive
``decide`` at candle close and ``on_fill`` for every actual fill.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite
from types import MappingProxyType

import pandas as pd
from btc_backtest.engine.models import (
    InstrumentKind,
    OrderIntent,
    OrderSide,
    OrderType,
)
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    StrategyContext,
    StrategyMetadata,
)

from qt.legacy.btcqt.config import Config, S0Cfg, S1Cfg, S2Cfg
from qt.legacy.btcqt.indicators.engine import IndicatorEngine
from qt.legacy.btcqt.models import (
    BookTicker,
    Candle,
    Fill,
    ForceOrder,
    FundingRate,
    Intent,
    MarketState,
    MarkPrice,
    OISnapshot,
    Side,
)
from qt.legacy.btcqt.strategy.base import Strategy
from qt.legacy.btcqt.strategy.s0_trend import S0Trend
from qt.legacy.btcqt.strategy.s1_wick_catcher import S1WickCatcher
from qt.legacy.btcqt.strategy.s2_crowding_fader import S2CrowdingFader


class MissingRequiredDataError(ValueError):
    """Raised when a port would otherwise replace required evidence with zeros."""


class UnsupportedExecutionSemanticsError(ValueError):
    """Raised when ``OrderIntent`` cannot truthfully represent a legacy command."""


@dataclass(frozen=True)
class BtcqtCausalRecord:
    """One versioned auxiliary observation supplied to the original engine."""

    observed_at: datetime
    available_at: datetime
    values: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "auxiliary observation timestamp")
        _require_aware(self.available_at, "auxiliary availability timestamp")
        if self.available_at < self.observed_at:
            raise ValueError("auxiliary availability cannot precede its observation")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class BtcqtCausalBundle:
    """Immutable auxiliary datasets for one native btc-qt execution.

    The bundle deliberately carries no filesystem locations.  A queued job
    captures dataset IDs, content fingerprints, and per-record availability;
    the indicator bridge receives only those attested records.
    """

    versions: Mapping[str, str]
    records: Mapping[str, tuple[BtcqtCausalRecord, ...]]

    def __post_init__(self) -> None:
        versions = {str(key): str(value) for key, value in self.versions.items()}
        if any(not key or not value for key, value in versions.items()):
            raise ValueError("causal bundle dataset IDs and fingerprints must not be empty")
        records = {
            str(key): tuple(sorted(value, key=lambda item: (item.observed_at, item.available_at)))
            for key, value in self.records.items()
        }
        if set(records).difference(versions):
            raise ValueError("causal bundle records require a dataset fingerprint")
        object.__setattr__(self, "versions", MappingProxyType(versions))
        object.__setattr__(self, "records", MappingProxyType(records))

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> BtcqtCausalBundle:
        """Parse the JSON-safe immutable job payload without host paths."""

        versions: dict[str, str] = {}
        records: dict[str, tuple[BtcqtCausalRecord, ...]] = {}
        for stream, raw_dataset in payload.items():
            if not isinstance(raw_dataset, Mapping):
                raise ValueError(f"btc-qt causal dataset {stream} must be an object")
            dataset_id = _required_text(raw_dataset.get("dataset_id"), "dataset_id")
            if dataset_id != stream:
                raise ValueError(f"btc-qt causal dataset key {stream} must equal dataset_id")
            fingerprint = _required_text(raw_dataset.get("fingerprint"), "fingerprint")
            raw_records = raw_dataset.get("records")
            if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes)):
                raise ValueError(f"btc-qt causal dataset {dataset_id} requires a records array")
            parsed: list[BtcqtCausalRecord] = []
            for raw_record in raw_records:
                if not isinstance(raw_record, Mapping):
                    raise ValueError(f"btc-qt causal dataset {dataset_id} has a non-object record")
                observed_at = _parse_timestamp(raw_record.get("observed_at"), "observed_at")
                available_at = _parse_timestamp(raw_record.get("available_at"), "available_at")
                values = {
                    str(key): value
                    for key, value in raw_record.items()
                    if key not in {"observed_at", "available_at"}
                }
                parsed.append(BtcqtCausalRecord(observed_at, available_at, values))
            computed = causal_records_fingerprint(dataset_id, parsed)
            if fingerprint.removeprefix("sha256:") != computed:
                raise ValueError(
                    f"btc-qt causal dataset {dataset_id} fingerprint does not match its records",
                )
            versions[dataset_id] = f"sha256:{computed}"
            records[dataset_id] = tuple(parsed)
        return cls(versions=versions, records=records)

    @classmethod
    def from_attested_streams(
        cls,
        streams: Mapping[str, tuple[str, Sequence[BtcqtCausalRecord]]],
    ) -> BtcqtCausalBundle:
        """Build from server-attested parquet records, never caller JSON rows."""

        return cls(
            versions={stream: fingerprint for stream, (fingerprint, _) in streams.items()},
            records={stream: tuple(records) for stream, (_, records) in streams.items()},
        )


@dataclass(frozen=True)
class DataVersion:
    """Versioned identity of one causal input stream."""

    dataset_id: str
    version: str
    available_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.available_at, "data availability timestamp")
        if not self.dataset_id or not self.version:
            raise ValueError("dataset_id and version must not be empty")


@dataclass(frozen=True)
class DataDependency:
    dataset_id: str
    reason: str


@dataclass(frozen=True)
class SourceIdentity:
    repository: str
    commit: str
    module: str
    source_version: str


@dataclass(frozen=True)
class PortMetadata:
    strategy_id: str
    source: SourceIdentity
    required_data: tuple[DataDependency, ...]
    optional_data: tuple[DataDependency, ...]
    supports_short: bool
    native_hook: str


BTCQT_SOURCE = SourceIdentity(
    repository="/Users/kwt/x/btc-qt",
    commit="4aa80b4cb09fdbf5a14f749986cba3c9d16535c9",
    module="qt.legacy.btcqt",
    source_version="0.3.0",
)


BTCQT_PORTS: Mapping[str, PortMetadata] = MappingProxyType(
    {
        "btcqt_s0_trend": PortMetadata(
            strategy_id="btcqt_s0_trend",
            source=BTCQT_SOURCE,
            required_data=(DataDependency("ohlcv_1m", "legacy IndicatorEngine input"),),
            optional_data=(),
            supports_short=True,
            native_hook="BtcqtStrategyPort.decide/on_fill",
        ),
        "btcqt_s1_wick_confirm": PortMetadata(
            strategy_id="btcqt_s1_wick_confirm",
            source=BTCQT_SOURCE,
            required_data=(
                DataDependency("ohlcv_1m", "legacy IndicatorEngine and event recovery"),
                DataDependency("event_state", "legacy EventTracker verdict and event VWAP"),
            ),
            optional_data=(
                DataDependency("liquidations", "full-data REJECT classification"),
                DataDependency("open_interest", "forced-deleverage corroboration"),
                DataDependency("taker_flow", "flow reversal corroboration"),
                DataDependency("cross_venue_prices", "local-dislocation corroboration"),
            ),
            supports_short=True,
            native_hook="BtcqtStrategyPort.decide/on_fill",
        ),
        "btcqt_s1_wick_ladder": PortMetadata(
            strategy_id="btcqt_s1_wick_ladder",
            source=BTCQT_SOURCE,
            required_data=(DataDependency("ohlcv_1m", "legacy IndicatorEngine input"),),
            optional_data=(
                DataDependency("liquidations", "continuation guard"),
                DataDependency("open_interest", "continuation guard"),
            ),
            supports_short=True,
            native_hook="BtcqtStrategyPort.decide/on_fill",
        ),
        "btcqt_s2_crowding_fader": PortMetadata(
            strategy_id="btcqt_s2_crowding_fader",
            source=BTCQT_SOURCE,
            required_data=(
                DataDependency("ohlcv_1m", "legacy IndicatorEngine input"),
                DataDependency("funding_settlements", "8-hour persistence state"),
                DataDependency("open_interest", "crowding confirmation and stop sizing"),
            ),
            optional_data=(),
            supports_short=True,
            native_hook="BtcqtStrategyPort.decide/on_fill",
        ),
    }
)


@dataclass(frozen=True)
class CausalState:
    """A legacy indicator snapshot together with its availability evidence."""

    state: MarketState
    observed_at: datetime
    available_at: datetime
    inputs: tuple[DataVersion, ...]

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observation timestamp")
        _require_aware(self.available_at, "availability timestamp")
        if self.available_at < self.observed_at:
            raise ValueError("state cannot be available before it was observed")
        state_at = datetime.fromtimestamp(self.state.ts / 1000, tz=timezone.utc)
        if state_at != self.observed_at:
            raise ValueError("MarketState.ts must match observed_at exactly")
        if len({item.dataset_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("input dataset identities must be unique")
        if any(item.available_at > self.available_at for item in self.inputs):
            raise ValueError("state availability precedes one of its input versions")
        # ``MarketState`` comes from legacy code and is deliberately mutable.
        # Capture it here so an indicator engine's next candle cannot rewrite a
        # previously admitted observation.
        object.__setattr__(self, "state", deepcopy(self.state))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    def snapshot(self) -> CausalState:
        """Return an isolated copy for a single decision or fill callback."""
        return CausalState(
            state=self.state,
            observed_at=self.observed_at,
            available_at=self.available_at,
            inputs=self.inputs,
        )


class CausalStateTimeline:
    """Causal state lookup which never exposes an unavailable future snapshot."""

    def __init__(self, states: Sequence[CausalState]) -> None:
        ordered = tuple(sorted(states, key=lambda item: item.observed_at))
        if len({item.observed_at for item in ordered}) != len(ordered):
            raise ValueError("state observations must have unique timestamps")
        self._states = tuple(item.snapshot() for item in ordered)

    def at(self, decision_at: datetime) -> CausalState:
        _require_aware(decision_at, "decision timestamp")
        visible = [
            item
            for item in self._states
            if item.observed_at <= decision_at and item.available_at <= decision_at
        ]
        if not visible:
            raise MissingRequiredDataError(
                f"no causal btc-qt state is available at {decision_at.isoformat()}"
            )
        return visible[-1].snapshot()


class BtcqtIndicatorStateBuilder:
    """Feed the original incremental IndicatorEngine without future observations.

    The bridge owns source ingestion and calls these methods in observed-time
    order.  It must supply the data identities used for each closed candle;
    this class never manufactures a liquidation, funding, or OI value.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.engine = IndicatorEngine(config or Config())
        self._pending: list[tuple[int, int, datetime, Callable[[], None]]] = []
        self._sequence = 0
        self._last_candle_close_ms: int | None = None

    def on_force_order(self, event: ForceOrder, *, available_at: datetime) -> None:
        self._queue(event.ts, available_at, lambda: self.engine.on_force_order(event))

    def on_funding_settled(self, event: FundingRate, *, available_at: datetime) -> None:
        self._queue(event.ts, available_at, lambda: self.engine.on_funding_settled(event))

    def on_open_interest(self, event: OISnapshot, *, available_at: datetime) -> None:
        self._queue(event.ts, available_at, lambda: self.engine.on_oi(event))

    def on_mark_price(self, event: MarkPrice, *, available_at: datetime) -> None:
        self._queue(event.ts, available_at, lambda: self.engine.on_mark_price(event))

    def on_book(self, event: BookTicker, *, available_at: datetime) -> None:
        self._queue(event.ts, available_at, lambda: self.engine.on_book(event))

    def on_taker_flow(
        self,
        timestamp_ms: int,
        buy_qty: float,
        sell_qty: float,
        *,
        available_at: datetime,
    ) -> None:
        self._queue(
            timestamp_ms,
            available_at,
            lambda: self.engine.on_taker(timestamp_ms, buy_qty, sell_qty),
        )

    def on_cross_venue_price(
        self,
        venue: str,
        timestamp_ms: int,
        price: float,
        *,
        available_at: datetime,
    ) -> None:
        self._queue(
            timestamp_ms,
            available_at,
            lambda: self.engine.on_xprice(venue, timestamp_ms, price),
        )

    def on_candle(
        self,
        candle: Candle,
        *,
        available_at: datetime,
        inputs: Sequence[DataVersion],
    ) -> CausalState:
        observed_at = datetime.fromtimestamp(candle.close_ts / 1000, tz=timezone.utc)
        _require_aware(available_at, "state availability timestamp")
        if available_at < observed_at:
            raise ValueError("a candle state cannot be available before candle close")
        if self._last_candle_close_ms is not None and candle.close_ts <= self._last_candle_close_ms:
            raise ValueError("candle closes must be strictly increasing")
        self._flush_eligible(candle.close_ts, available_at)
        self._last_candle_close_ms = candle.close_ts
        return CausalState(
            state=self.engine.on_candle(candle),
            observed_at=observed_at,
            available_at=available_at,
            inputs=tuple(inputs),
        )

    def _queue(
        self,
        observed_ms: int,
        available_at: datetime,
        apply: Callable[[], None],
    ) -> None:
        _require_aware(available_at, "event availability timestamp")
        observed_at = datetime.fromtimestamp(observed_ms / 1000, tz=timezone.utc)
        if available_at < observed_at:
            raise ValueError("event availability cannot precede its observation")
        self._sequence += 1
        self._pending.append((observed_ms, self._sequence, available_at, apply))

    def _flush_eligible(self, candle_close_ms: int, state_available_at: datetime) -> None:
        queued = sorted(self._pending, key=lambda item: (item[0], item[1]))
        remaining: list[tuple[int, int, datetime, Callable[[], None]]] = []
        for observed_ms, sequence, available_at, apply in queued:
            if observed_ms <= candle_close_ms and available_at <= state_available_at:
                apply()
            else:
                remaining.append((observed_ms, sequence, available_at, apply))
        self._pending = remaining


@dataclass(frozen=True)
class PortDecision:
    strategy_id: str
    timestamp: datetime
    state: CausalState
    intents: tuple[Intent, ...]


class BtcqtStrategyPort:
    """State/fill callback hook around an unmodified btc-qt strategy instance."""

    def __init__(self, metadata: PortMetadata, strategy: Strategy) -> None:
        self.metadata = metadata
        self.strategy = strategy

    def validate_data(self, inputs: Sequence[DataVersion]) -> None:
        versions = {item.dataset_id: item for item in inputs}
        missing = [item.dataset_id for item in self.metadata.required_data if item.dataset_id not in versions]
        if missing:
            names = ", ".join(missing)
            raise MissingRequiredDataError(
                f"{self.metadata.strategy_id} requires {names}; no zero-filled substitute is allowed"
            )

    def decide(
        self,
        timeline: CausalStateTimeline,
        *,
        timestamp: datetime,
        equity: float,
    ) -> PortDecision:
        _require_aware(timestamp, "decision timestamp")
        if not isfinite(equity) or equity <= 0:
            raise ValueError("equity must be finite and positive")
        causal_state = timeline.at(timestamp)
        self.validate_data(causal_state.inputs)
        intents = tuple(self.strategy.on_state(causal_state.state, equity))
        return PortDecision(self.metadata.strategy_id, timestamp, causal_state, intents)

    def on_fill(self, fill: Fill, state: CausalState) -> PortDecision:
        if fill.strategy != self.strategy.name:
            raise ValueError(f"fill strategy {fill.strategy!r} does not belong to {self.strategy.name!r}")
        self.validate_data(state.inputs)
        intents, _realized_pnl = self.strategy.on_fill(fill, state.state)
        timestamp = datetime.fromtimestamp(fill.ts / 1000, tz=timezone.utc)
        return PortDecision(self.metadata.strategy_id, timestamp, state, tuple(intents))

    def order_intents(self, decision: PortDecision) -> tuple[OrderIntent, ...]:
        """Convert only exactly representable commands to btc-backtest intents.

        Cancellations, ladder replacement, IOC, and take-profit need an
        execution lifecycle.  Returning a normal limit order for any of those
        would be a semantic change, so this method rejects them explicitly.
        """
        return tuple(
            _to_order_intent(intent, position_side=self.strategy.pos.side)
            for intent in decision.intents
        )


def create_btcqt_port(
    strategy_id: str,
    parameters: Mapping[str, object] | None = None,
) -> BtcqtStrategyPort:
    """Create a source-faithful S0, S1 (confirm/ladder), or S2 port."""
    params = dict(parameters or {})
    if strategy_id == "btcqt_s0_trend":
        return BtcqtStrategyPort(BTCQT_PORTS[strategy_id], S0Trend(S0Cfg.model_validate(params)))
    if strategy_id == "btcqt_s1_wick_confirm":
        cfg = S1Cfg.model_validate({**params, "entry_mode": "confirm"})
        return BtcqtStrategyPort(BTCQT_PORTS[strategy_id], S1WickCatcher(cfg))
    if strategy_id == "btcqt_s1_wick_ladder":
        cfg = S1Cfg.model_validate({**params, "entry_mode": "ladder"})
        return BtcqtStrategyPort(BTCQT_PORTS[strategy_id], S1WickCatcher(cfg))
    if strategy_id == "btcqt_s2_crowding_fader":
        return BtcqtStrategyPort(BTCQT_PORTS[strategy_id], S2CrowdingFader(S2Cfg.model_validate(params)))
    raise KeyError(f"unknown btc-qt strategy port: {strategy_id}")


class BtcqtNativeStrategy:
    """Native-only shell for original S0/S1/S2 state and fill lifecycles."""

    def __init__(
        self,
        port: BtcqtStrategyPort,
        *,
        dataset_version: str,
        causal_bundle: BtcqtCausalBundle | None = None,
        leverage: Decimal = Decimal("1"),
    ) -> None:
        if not dataset_version:
            raise ValueError("btc-qt native strategy requires an OHLCV fingerprint")
        if not Decimal("1") <= leverage <= Decimal("2"):
            raise ValueError(
                "btc-qt source gateway permits only bounded 1x-2x USDT-M leverage",
            )
        self.port = port
        self._dataset_version = dataset_version
        self._bundle = causal_bundle or BtcqtCausalBundle({}, {})
        self._builder = BtcqtIndicatorStateBuilder()
        self._queued_auxiliary = False
        self._last_candle_open: pd.Timestamp | None = None
        self.parameters = MappingProxyType({})
        self.native_instrument_kind = InstrumentKind.PERPETUAL
        self.source_leverage_cap = Decimal("2")
        self._validate_bundle_requirements()
        self.metadata = StrategyMetadata(
            id=f"native_{port.metadata.strategy_id}",
            version=port.metadata.source.commit,
            description=(
                "Original btc-qt source strategy through the causal indicator "
                "and native order/fill lifecycle on Binance USDT-M semantics."
            ),
            warmup_bars=0,
            supported_timeframes=("1m",),
            supported_instruments=(InstrumentKind.PERPETUAL,),
            requires_full_history=False,
        )

    def initialize(self, context: InitializationContext) -> None:
        if context.spec.data.timeframe != "1m":
            raise ValueError(
                f"{self.port.metadata.strategy_id} requires 1m source candles, "
                f"got {context.spec.data.timeframe}",
            )

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        del context
        raise RuntimeError(
            "btc-qt source intents require the Nautilus BtcqtNativeIntentRouter; "
            "generic OrderIntent execution is prohibited",
        )

    def finalize(self, context: FinalizationContext) -> None:
        del context

    def native_decision(
        self,
        *,
        timestamp: datetime,
        bars: pd.DataFrame,
        equity: Decimal,
    ) -> PortDecision:
        """Advance the original 1m engine from completed, causally visible bars."""

        if equity <= 0 or not equity.is_finite():
            raise ValueError("native portfolio equity must be finite and positive")
        cutoff = pd.Timestamp(timestamp)
        end = bars.index.searchsorted(cutoff, side="right")
        start = 0 if self._last_candle_open is None else bars.index.searchsorted(
            self._last_candle_open,
            side="right",
        )
        pending = bars.iloc[start:end]
        if pending.empty and self._last_candle_open is None:
            raise MissingRequiredDataError("btc-qt native decision has no completed 1m candle")
        if not self._queued_auxiliary:
            self._queue_auxiliary_records()
            self._queued_auxiliary = True
        decision: PortDecision | None = None
        for opened_at, row in pending.iterrows():
            if not isinstance(opened_at, pd.Timestamp):
                raise ValueError("btc-qt native bars require timestamped 1m candles")
            candle = Candle(
                ts=int(opened_at.timestamp() * 1000),
                open=_finite_float(row.open, "open"),
                high=_finite_float(row.high, "high"),
                low=_finite_float(row.low, "low"),
                close=_finite_float(row.close, "close"),
                volume=_finite_float(row.volume, "volume"),
            )
            close_at = datetime.fromtimestamp(candle.close_ts / 1000, tz=timezone.utc)
            state = self._builder.on_candle(
                candle,
                available_at=close_at,
                inputs=self._inputs_at(close_at),
            )
            decision = self.port.decide(
                CausalStateTimeline((state,)),
                timestamp=close_at,
                equity=float(equity),
            )
            self._last_candle_open = opened_at
        if decision is None:
            raise ValueError("btc-qt native decision attempted to replay an already processed candle")
        return decision

    def _validate_bundle_requirements(self) -> None:
        required = {
            dependency.dataset_id
            for dependency in self.port.metadata.required_data
            if dependency.dataset_id not in {"ohlcv_1m", "event_state"}
        }
        missing = sorted(required.difference(self._bundle.versions))
        if missing:
            raise MissingRequiredDataError(
                f"{self.port.metadata.strategy_id} requires attested causal datasets "
                f"({', '.join(missing)}); no zero-filled substitute is allowed",
            )
        empty = sorted(dataset_id for dataset_id in required if not self._bundle.records[dataset_id])
        if empty:
            raise MissingRequiredDataError(
                f"{self.port.metadata.strategy_id} requires nonempty causal records for "
                f"{', '.join(empty)}; an empty stream cannot prove historical availability",
            )

    def _queue_auxiliary_records(self) -> None:
        for stream, records in self._bundle.records.items():
            for record in records:
                timestamp_ms = int(record.observed_at.timestamp() * 1000)
                values = record.values
                if stream == "liquidations":
                    self._builder.on_force_order(
                        ForceOrder(
                            timestamp_ms,
                            _required_text(values.get("side"), "liquidation side"),
                            _finite_float(values.get("price"), "liquidation price"),
                            _finite_float(values.get("qty"), "liquidation qty"),
                        ),
                        available_at=record.available_at,
                    )
                elif stream == "funding_settlements":
                    self._builder.on_funding_settled(
                        FundingRate(timestamp_ms, _finite_float(values.get("rate"), "funding rate")),
                        available_at=record.available_at,
                    )
                elif stream == "open_interest":
                    self._builder.on_open_interest(
                        OISnapshot(timestamp_ms, _finite_float(values.get("oi"), "open interest")),
                        available_at=record.available_at,
                    )
                elif stream == "mark_prices":
                    self._builder.on_mark_price(
                        MarkPrice(
                            timestamp_ms,
                            _finite_float(values.get("mark"), "mark price"),
                            _finite_float(values.get("index"), "index price"),
                            _finite_float(values.get("funding_rate"), "predicted funding rate"),
                            int(values.get("next_funding_ts", 0)),
                        ),
                        available_at=record.available_at,
                    )
                elif stream == "book_tickers":
                    self._builder.on_book(
                        BookTicker(
                            timestamp_ms,
                            _finite_float(values.get("bid"), "bid"),
                            _finite_float(values.get("ask"), "ask"),
                        ),
                        available_at=record.available_at,
                    )
                elif stream == "taker_flow":
                    self._builder.on_taker_flow(
                        timestamp_ms,
                        _finite_float(values.get("buy_qty"), "buy_qty"),
                        _finite_float(values.get("sell_qty"), "sell_qty"),
                        available_at=record.available_at,
                    )
                elif stream == "cross_venue_prices":
                    self._builder.on_cross_venue_price(
                        _required_text(values.get("venue"), "cross-venue name"),
                        timestamp_ms,
                        _finite_float(values.get("price"), "cross-venue price"),
                        available_at=record.available_at,
                    )
                else:
                    raise MissingRequiredDataError(
                        f"unsupported btc-qt causal stream {stream}; no substitute is permitted",
                    )

    def _inputs_at(self, close_at: datetime) -> tuple[DataVersion, ...]:
        inputs = [DataVersion("ohlcv_1m", self._dataset_version, close_at)]
        if any(item.dataset_id == "event_state" for item in self.port.metadata.required_data):
            # EventTracker is a deterministic result of the original IndicatorEngine
            # over this exact OHLCV stream, not a fabricated external feature.
            inputs.append(DataVersion("event_state", self._dataset_version, close_at))
        for stream, version in self._bundle.versions.items():
            visible = [record for record in self._bundle.records[stream] if record.available_at <= close_at]
            if visible:
                inputs.append(DataVersion(stream, version, max(record.available_at for record in visible)))
        return tuple(inputs)


def _to_order_intent(intent: Intent, *, position_side: Side | None) -> OrderIntent:
    if intent.action == "MARKET_ENTER":
        return OrderIntent(
            instrument=_entry_instrument(intent.side),
            side=OrderSide.BUY if intent.side is Side.BUY else OrderSide.SELL,
            order_type=OrderType.MARKET,
            base_quantity=_positive_decimal(intent.qty, intent.action),
            reason=intent.reason,
        )
    if intent.action in {"MARKET_EXIT", "PLACE_STOP"}:
        instrument = _exit_instrument(position_side)
        return OrderIntent(
            instrument=instrument,
            side=OrderSide.BUY if intent.side is Side.BUY else OrderSide.SELL,
            order_type=OrderType.MARKET if intent.action == "MARKET_EXIT" else OrderType.STOP,
            base_quantity=_positive_decimal(intent.qty, intent.action),
            stop_price=(
                _positive_decimal(intent.price, intent.action)
                if intent.action == "PLACE_STOP"
                else None
            ),
            reason=intent.reason,
        )
    raise UnsupportedExecutionSemanticsError(
        f"{intent.action} from {intent.strategy} requires the native state/fill hook; "
        "btc-backtest OrderIntent cannot preserve its cancellation, IOC, reduce-only, "
        "or ladder semantics"
    )


def _entry_instrument(side: Side | None) -> InstrumentKind:
    if side in {Side.BUY, Side.SELL}:
        return InstrumentKind.PERPETUAL
    raise ValueError("MARKET_ENTER requires a side")


def _exit_instrument(position_side: Side | None) -> InstrumentKind:
    if position_side in {Side.BUY, Side.SELL}:
        return InstrumentKind.PERPETUAL
    raise UnsupportedExecutionSemanticsError(
        "exit or stop conversion requires the source strategy's open position"
    )


def _positive_decimal(value: float | None, action: str) -> Decimal:
    if value is None or value <= 0:
        raise ValueError(f"{action} requires a positive price or quantity")
    return Decimal(str(value))


def causal_records_fingerprint(dataset_id: str, records: Sequence[BtcqtCausalRecord]) -> str:
    canonical = {
        "dataset_id": dataset_id,
        "records": [
            {
                "observed_at": record.observed_at.astimezone(timezone.utc).isoformat(),
                "available_at": record.available_at.astimezone(timezone.utc).isoformat(),
                "values": dict(sorted(record.values.items())),
            }
            for record in records
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    _require_aware(parsed, field)
    return parsed.astimezone(timezone.utc)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be finite") from error
    if not isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
