"""Reuse the exact registered native strategy and sizing code in Paper/Shadow.

This bridge changes only the input/output boundary: closed event prefixes and
an OMS portfolio become the same ``StrategyContext`` consumed by the native
engine. Source identity, parameters and the shared sizing wrapper are verified.
Matching differences remain explicit: native bar fills versus bounded L2 Paper.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pandas as pd
from btc_backtest.data.models import DataRequest
from btc_backtest.engine.models import (
    BacktestSpec,
    InstrumentKind,
    OrderSide,
    PortfolioSnapshot,
    Position,
)
from btc_backtest.strategies.base import InitializationContext, StrategyContext
from btc_backtest.strategies.registry import default_strategy_registry

from qt.contracts import (
    DeploymentManifest,
    FeeSchedule,
    InstrumentSpec,
    MarketEvent,
    SignalIntent,
    StrategyArtifact,
    StrategyState,
    content_hash,
)
from qt.experiments.native import _BudgetedBuiltin, _strategy_parameters
from qt.marketdata import ClosedBar
from qt.strategies.event_driven import _ReferenceStrategy

_TIMEFRAMES = {
    60_000_000_000: "1m", 300_000_000_000: "5m", 900_000_000_000: "15m",
    3_600_000_000_000: "1h", 14_400_000_000_000: "4h", 86_400_000_000_000: "1d",
}


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, deque):
        return {"type": "deque", "maximum": value.maxlen, "values": [_encode(item) for item in value]}
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ValueError("native_strategy_state_type_not_supported")


def _decode(value: Any) -> Any:
    if isinstance(value, Mapping):
        if value.get("type") == "decimal" and set(value) == {"type", "value"}:
            amount = Decimal(value["value"])
            if not amount.is_finite():
                raise ValueError("nonfinite_native_strategy_state")
            return amount
        if value.get("type") == "deque" and set(value) == {"type", "maximum", "values"}:
            maximum = value["maximum"]
            if type(maximum) is not int or not 1 <= maximum <= 10_000:
                raise ValueError("invalid_native_deque_size")
            return deque((_decode(item) for item in value["values"]), maxlen=maximum)
        raise ValueError("invalid_native_strategy_state_encoding")
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ValueError("invalid_native_strategy_state_value")


class NativeEventStrategy:
    accepted_event_types = frozenset({"bar"})

    def __init__(
        self, artifact: StrategyArtifact, instrument: InstrumentSpec, fees: FeeSchedule,
        deployment_id: str, *, maximum_history_bars: int = 100_000,
    ) -> None:
        if maximum_history_bars < 3 or maximum_history_bars > 100_000:
            raise ValueError("invalid_native_history_capacity")
        if artifact.signal_interval_ns not in _TIMEFRAMES:
            raise ValueError("native_live_timeframe_not_supported")
        self.artifact, self.instrument, self.fees = artifact, instrument, fees
        self.deployment_id, self.maximum_history_bars = deployment_id, maximum_history_bars
        self.artifact_hash = artifact.content_hash()
        self.kind, self.canonical_parameters = _strategy_parameters(artifact)
        self._source = default_strategy_registry().create(self.kind, self.canonical_parameters)
        self._bounded = _BudgetedBuiltin(self._source, artifact, instrument, fees.taker_rate)
        self._rows: list[dict[str, Any]] = []
        self._initialized = False
        self._cash = artifact.risk_budget.capital
        self._base = Decimal("0")

    @property
    def bridge_hash(self) -> str:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def verify_artifact(self, artifact: StrategyArtifact) -> None:
        kind, parameters = _strategy_parameters(artifact)
        if (artifact.content_hash() != self.artifact_hash or kind != self.kind
                or parameters != self.canonical_parameters
                or artifact.instruments != (self.instrument.instrument,)):
            raise ValueError("native_live_artifact_binding_mismatch")

    def set_portfolio(self, *, cash: Decimal, base_quantity: Decimal, asof_ns: int) -> None:
        if any(not value.is_finite() or value < 0 for value in (cash, base_quantity)):
            raise ValueError("native_live_portfolio_invalid")
        self._cash, self._base = cash, base_quantity

    def _initialize(self, first: ClosedBar) -> None:
        interval = self.artifact.signal_interval_ns
        start = pd.Timestamp(first.open_ts_ns // interval * interval, unit="ns", tz="UTC").to_pydatetime()
        spec = BacktestSpec(
            strategy=self.kind, strategy_params=dict(self._source.parameters),
            data=DataRequest(provider=self.instrument.venue,
                             symbol=f"{self.instrument.base_asset}/{self.instrument.quote_asset}",
                             timeframe=_TIMEFRAMES[self.artifact.signal_interval_ns],
                             start=start, end=start + timedelta(days=1), market="spot"),
            initial_cash=self.artifact.risk_budget.capital,
            fee_bps=self.fees.taker_rate * Decimal("10000"), slippage_bps=Decimal("0"),
        )
        self._bounded.initialize(InitializationContext(spec=spec, data_manifests=(),
                                                       parameters=spec.strategy_params))
        self._initialized = True

    def _source_state(self) -> dict[str, object]:
        return {key: _encode(value) for key, value in self._source.__dict__.items()
                if key not in {"metadata", "params", "parameters", "_parameter_values"}}

    def snapshot_state(self) -> StrategyState:
        return StrategyState.model_validate({"values": {
            "artifact_hash": self.artifact_hash, "bridge_hash": self.bridge_hash,
            "initialized": self._initialized, "rows": self._rows,
            "source_state": self._source_state(),
        }})

    def restore_state(self, state: StrategyState) -> None:
        values = state.values
        if values.get("artifact_hash") != self.artifact_hash or values.get("bridge_hash") != self.bridge_hash:
            raise ValueError("native_live_state_version_mismatch")
        raw_rows = values.get("rows")
        if not isinstance(raw_rows, tuple) or len(raw_rows) > self.maximum_history_bars:
            raise ValueError("native_live_state_history_invalid")
        self._rows = [dict(row) for row in raw_rows]
        self._initialized = values.get("initialized") is True
        source = values.get("source_state")
        if not isinstance(source, Mapping) or set(source) != set(self._source_state()):
            raise ValueError("native_live_source_state_shape_changed")
        for key, value in source.items():
            setattr(self._source, key, _decode(value))

    def on_event(self, event: MarketEvent, state: StrategyState) -> list[SignalIntent]:
        if state.values:
            self.restore_state(state)
        if (event.event_type != "bar" or event.venue != self.instrument.venue
                or event.instrument != self.instrument.instrument):
            return []
        bar = _ReferenceStrategy._bar(event)
        if bar.close_ts_ns - bar.open_ts_ns != self.artifact.signal_interval_ns:
            raise ValueError("native_live_bar_interval_mismatch")
        if self._rows:
            previous = self._rows[-1]
            if bar.close_ts_ns <= previous["close_ts_ns"]:
                return []
            if bar.open_ts_ns != previous["close_ts_ns"]:
                raise ValueError("native_live_bar_gap_requires_resync")
        if len(self._rows) >= self.maximum_history_bars:
            raise ValueError("native_live_history_capacity_requires_checkpoint_upgrade")
        self._rows.append({"received_at_ns": bar.received_at_ns, "close_ts_ns": bar.close_ts_ns,
                           **{name: str(getattr(bar, name)) for name in ("open", "high", "low", "close", "volume")}})
        if not self._initialized:
            self._initialize(bar)
        if len(self._rows) <= self._source.metadata.warmup_bars:
            return []
        frame = pd.DataFrame(self._rows)
        frame.index = pd.to_datetime(frame.pop("received_at_ns"), unit="ns", utc=True)
        frame.pop("close_ts_ns")
        frame = frame.astype("float64")
        timestamp = frame.index[-1]
        with localcontext() as context:
            context.prec = 80
            portfolio = PortfolioSnapshot(
                timestamp=timestamp, cash=self._cash, equity=self._cash + self._base * bar.close,
                realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"),
                positions=(Position(instrument=InstrumentKind.SPOT, quantity=self._base),),
            )
            native_context = StrategyContext(timestamp=timestamp, bars=frame, portfolio=portfolio,
                                             parameters=dict(self._source.parameters))
            intents = tuple(self._bounded.on_bar(native_context))
            if len(intents) > 1:
                raise ValueError("native_live_multileg_intent_not_supported")
            result = []
            for intent in intents:
                if intent.instrument != InstrumentKind.SPOT or intent.base_quantity is None:
                    raise ValueError("native_live_intent_requires_frozen_spot_quantity")
                target = self._base + (intent.base_quantity if intent.side == OrderSide.BUY else -intent.base_quantity)
                result.append(SignalIntent(
                    signal_id=content_hash([self.deployment_id, self.artifact_hash, bar.close_ts_ns,
                                            intent.side.value, str(intent.base_quantity)]),
                    deployment_id=self.deployment_id, event_id=event.content_hash(),
                    instrument=self.instrument.instrument, target_position=target,
                    produced_at_ns=event.received_ts_ns,
                    valid_until_ns=event.received_ts_ns + self.artifact.signal_ttl_ns,
                    reason_codes=(self.kind, intent.reason, "shared_native_strategy_and_sizing"),
                ))
        return result

    def seed_history(self, events: Iterable[MarketEvent], *, decision_at_ns: int) -> int:
        """Load an observable warmup prefix without making historical decisions.

        Supply the same registered dataset prefix used for native initialization.
        A persisted runtime checkpoint takes precedence on restart. This method
        never opens a position or replays old signals against today's portfolio.
        """
        if self._rows:
            raise ValueError("native_history_can_only_be_seeded_once_before_execution")
        for event in events:
            if event.event_type != "bar":
                continue
            if event.received_ts_ns > decision_at_ns:
                break
            bar = _ReferenceStrategy._bar(event)
            if (event.instrument != self.instrument.instrument or event.venue != self.instrument.venue
                    or bar.close_ts_ns - bar.open_ts_ns != self.artifact.signal_interval_ns):
                raise ValueError("native_seed_identity_or_interval_mismatch")
            if self._rows and bar.open_ts_ns != self._rows[-1]["close_ts_ns"]:
                raise ValueError("native_seed_bar_gap_or_overlap")
            if len(self._rows) >= self.maximum_history_bars:
                raise ValueError("native_seed_history_capacity_exceeded")
            self._rows.append({"received_at_ns": bar.received_at_ns, "close_ts_ns": bar.close_ts_ns,
                               **{name: str(getattr(bar, name))
                                  for name in ("open", "high", "low", "close", "volume")}})
            if not self._initialized:
                self._initialize(bar)
        return len(self._rows)


def strategy_for_deployment(
    service: Any, deployment: DeploymentManifest | str,
) -> tuple[NativeEventStrategy, InstrumentSpec, FeeSchedule]:
    """Resolve exact immutable versions; no latest/name lookup or model code."""
    manifest = (service.verify_deployment(deployment) if isinstance(deployment, str)
                else service.verify_deployment(deployment.deployment_id))
    artifact = StrategyArtifact.model_validate(
        service.repository.get(service.tenant, "strategy", manifest.strategy_hash)["payload"],
    )
    report = service.repository.get(service.tenant, "validation", manifest.validation_report_hash)["payload"]
    spec = service.repository.get(service.tenant, "experiment_spec", report["experiment_hash"])["payload"]
    hashes = spec.get("instrument_spec_hashes", [])
    if len(hashes) != 1 or len(manifest.fee_schedule_hashes) != 1:
        raise ValueError("native_live_requires_one_versioned_spot_instrument_and_fee")
    instrument = InstrumentSpec.model_validate(
        service.repository.get(service.tenant, "instrument", hashes[0])["payload"],
    )
    fees = FeeSchedule.model_validate(
        service.repository.get(service.tenant, "fees", manifest.fee_schedule_hashes[0])["payload"],
    )
    return NativeEventStrategy(artifact, instrument, fees, manifest.deployment_id), instrument, fees
