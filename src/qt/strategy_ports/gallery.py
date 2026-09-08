"""Causal research adapters for the original Qt5 gallery strategies.

The live gallery strategies emit ``EvaluationResult`` values for an operator;
the ``qt.strategies.sim`` variants replay a complete history into a
``StrategyResult``.  Those are intentionally separate interfaces.  This
module delegates to the original classes and never substitutes target weights
or ``btc_backtest`` aliases for the live strategies.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import datetime
from types import MappingProxyType
from typing import cast

import pandas as pd
from pandas import DataFrame, Series

from qt.strategies.base import EvaluationResult, StrategyConfig
from qt.strategies.capitulation import Capitulation
from qt.strategies.carry import BasisCarry
from qt.strategies.dca import SmartDCA
from qt.strategies.sim.base import StrategyResult
from qt.strategies.sim.basis_carry import BasisCarry as SimBasisCarry
from qt.strategies.sim.basis_carry import BasisCarryConfig
from qt.strategies.sim.smart_dca import SmartDCA as SimSmartDCA
from qt.strategies.sim.smart_dca import SmartDCAConfig
from qt.strategies.sim.trend_weekly import WeeklyTrend as SimWeeklyTrend
from qt.strategies.sim.trend_weekly import WeeklyTrendConfig
from qt.strategies.sim.wick_catcher import WickCatcher as SimWickCatcher
from qt.strategies.sim.wick_catcher import WickCatcherConfig
from qt.strategies.trend import WeeklyTrend
from qt.strategies.wick_catcher import WickCatcher
from qt.strategy_ports.btcqt import DataDependency, DataVersion, SourceIdentity


class GalleryPortDataError(ValueError):
    """Raised when an adapter's causal input boundary cannot be established."""


QT5_SOURCE = SourceIdentity(
    repository="/Users/kwt/x/qt",
    commit="fdb94dfb37193a62986473c15fdba4b4948ce3b0",
    module="qt.strategies",
    source_version="qt5-gallery",
)


@dataclass(frozen=True)
class GalleryPortMetadata:
    strategy_id: str
    source: SourceIdentity
    interface: str
    required_data: tuple[DataDependency, ...]
    optional_data: tuple[DataDependency, ...]
    native_hook: str


GALLERY_PORTS: Mapping[str, GalleryPortMetadata] = MappingProxyType(
    {
        "qt5_smart_dca": GalleryPortMetadata(
            "qt5_smart_dca",
            QT5_SOURCE,
            "live_evaluation_result",
            (DataDependency("ohlcv", "SmartDCA price stress inputs"),),
            (DataDependency("fear_greed", "lagged stress component"),),
            "operator consumes EvaluationResult.opportunity; no execution order",
        ),
        "qt5_capitulation": GalleryPortMetadata(
            "qt5_capitulation",
            QT5_SOURCE,
            "live_evaluation_result",
            (DataDependency("ohlcv", "composite extreme score price group"),),
            (
                DataDependency("funding", "composite derivatives group"),
                DataDependency("oi", "composite derivatives group"),
                DataDependency("lsr", "composite derivatives group"),
                DataDependency("fear_greed", "composite sentiment group"),
                DataDependency("mvrv", "composite on-chain group"),
            ),
            "operator consumes EvaluationResult.opportunity; no execution order",
        ),
        "qt5_weekly_trend": GalleryPortMetadata(
            "qt5_weekly_trend",
            QT5_SOURCE,
            "live_evaluation_result",
            (DataDependency("ohlcv", "weekly resample and volatility shock"),),
            (),
            "operator consumes EvaluationResult.opportunity; no execution order",
        ),
        "qt5_basis_carry": GalleryPortMetadata(
            "qt5_basis_carry",
            QT5_SOURCE,
            "live_evaluation_result",
            (
                DataDependency("ohlcv", "spot reference price"),
                DataDependency("funding", "funding-rate carry gate"),
            ),
            (),
            "operator interprets open/close Opportunity as paired spot/perp proposal",
        ),
        "qt5_wick_catcher": GalleryPortMetadata(
            "qt5_wick_catcher",
            QT5_SOURCE,
            "live_evaluation_result",
            (DataDependency("ohlcv", "prior close and latest wick"),),
            (),
            "operator interprets Opportunity details as a limit-ladder proposal",
        ),
        "qt5_sim_smart_dca": GalleryPortMetadata(
            "qt5_sim_smart_dca",
            QT5_SOURCE,
            "batch_strategy_result",
            (DataDependency("ohlcv", "hourly DCA replay"),),
            (
                DataDependency("fear_greed", "lagged stress component"),
                DataDependency("mvrv_z", "lagged stress component"),
                DataDependency("nupl", "lagged take-profit gate"),
            ),
            "research-only StrategyResult; native execution is intentionally unsupported",
        ),
        "qt5_sim_weekly_trend": GalleryPortMetadata(
            "qt5_sim_weekly_trend",
            QT5_SOURCE,
            "batch_strategy_result",
            (DataDependency("ohlcv", "hourly-to-weekly trend replay"),),
            (),
            "research-only StrategyResult; native execution is intentionally unsupported",
        ),
        "qt5_sim_basis_carry": GalleryPortMetadata(
            "qt5_sim_basis_carry",
            QT5_SOURCE,
            "batch_strategy_result",
            (
                DataDependency("ohlcv", "spot/perp replay reference"),
                DataDependency("funding", "per-bar funding accrual"),
            ),
            (),
            "research-only StrategyResult; native execution is intentionally unsupported",
        ),
        "qt5_sim_wick_catcher": GalleryPortMetadata(
            "qt5_sim_wick_catcher",
            QT5_SOURCE,
            "batch_strategy_result",
            (DataDependency("ohlcv", "OHLC limit-ladder replay"),),
            (),
            "research-only StrategyResult; native execution is intentionally unsupported",
        ),
    }
)


@dataclass(frozen=True)
class GalleryCausalInput:
    """Versioned data snapshots, clipped at the adapter decision timestamp."""

    data: Mapping[str, object]
    decision_at: datetime
    available_at: datetime
    inputs: tuple[DataVersion, ...]

    def __post_init__(self) -> None:
        _require_aware(self.decision_at, "decision timestamp")
        _require_aware(self.available_at, "availability timestamp")
        if self.available_at > self.decision_at:
            raise GalleryPortDataError("strategy data is unavailable at the decision timestamp")
        if any(version.available_at > self.available_at for version in self.inputs):
            raise GalleryPortDataError("input version is unavailable at the decision timestamp")
        object.__setattr__(self, "data", MappingProxyType({key: _copy_value(value) for key, value in self.data.items()}))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    def visible_data(self) -> dict[str, object]:
        return {key: _visible_value(value, self.decision_at) for key, value in self.data.items()}


@dataclass(frozen=True)
class GalleryLiveDecision:
    metadata: GalleryPortMetadata
    evaluation: EvaluationResult
    input_versions: tuple[DataVersion, ...]


@dataclass(frozen=True)
class GallerySimulationDecision:
    metadata: GalleryPortMetadata
    result: StrategyResult
    input_versions: tuple[DataVersion, ...]


class GalleryLivePort:
    """Delegates one current live gallery evaluation without placing orders."""

    def __init__(self, metadata: GalleryPortMetadata, parameters: Mapping[str, object] | None = None) -> None:
        self.metadata = metadata
        self.parameters = MappingProxyType(dict(parameters or {}))
        self._strategy = _live_strategy(metadata.strategy_id, self.parameters)

    def evaluate(self, context: GalleryCausalInput) -> GalleryLiveDecision:
        _validate_live_versions(self.metadata, context)
        return GalleryLiveDecision(self.metadata, self._strategy.evaluate(context.visible_data()), context.inputs)


class GallerySimulationPort:
    """Delegates one complete historical replay to its original simulator."""

    def __init__(self, metadata: GalleryPortMetadata, parameters: Mapping[str, object] | None = None) -> None:
        self.metadata = metadata
        self.parameters = MappingProxyType(dict(parameters or {}))
        self._strategy = _simulation_strategy(metadata.strategy_id, self.parameters)

    def run(self, context: GalleryCausalInput) -> GallerySimulationDecision:
        _validate_required(self.metadata, context.inputs)
        data = context.visible_data()
        ohlcv = _required_dataframe(data, "ohlcv")
        strategy = self.metadata.strategy_id
        if strategy == "qt5_sim_smart_dca":
            result = cast(SimSmartDCA, self._strategy).run(
                ohlcv,
                fear_greed=_optional_series(data, "fear_greed"),
                mvrv_z=_optional_series(data, "mvrv_z"),
                nupl=_optional_series(data, "nupl"),
            )
        elif strategy == "qt5_sim_weekly_trend":
            result = cast(SimWeeklyTrend, self._strategy).run(ohlcv)
        elif strategy == "qt5_sim_basis_carry":
            result = cast(SimBasisCarry, self._strategy).run(ohlcv, funding=_required_series(data, "funding"))
        elif strategy == "qt5_sim_wick_catcher":
            result = cast(SimWickCatcher, self._strategy).run(ohlcv)
        else:
            raise KeyError(f"unknown Qt5 simulation port: {strategy}")
        return GallerySimulationDecision(self.metadata, result, context.inputs)


def create_gallery_port(
    strategy_id: str,
    *,
    parameters: Mapping[str, object] | None = None,
) -> GalleryLivePort | GallerySimulationPort:
    metadata = GALLERY_PORTS.get(strategy_id)
    if metadata is None:
        raise KeyError(f"unknown Qt5 gallery port: {strategy_id}")
    if metadata.interface == "live_evaluation_result":
        return GalleryLivePort(metadata, parameters)
    return GallerySimulationPort(metadata, parameters)


def _live_strategy(strategy_id: str, parameters: Mapping[str, object]) -> SmartDCA | Capitulation | WeeklyTrend | BasisCarry | WickCatcher:
    config = StrategyConfig(name=strategy_id, params=dict(parameters))
    if strategy_id == "qt5_smart_dca":
        return SmartDCA(config)
    if strategy_id == "qt5_capitulation":
        return Capitulation(config)
    if strategy_id == "qt5_weekly_trend":
        return WeeklyTrend(config)
    if strategy_id == "qt5_basis_carry":
        return BasisCarry(config)
    if strategy_id == "qt5_wick_catcher":
        return WickCatcher(config)
    raise KeyError(f"unknown Qt5 live port: {strategy_id}")


def _simulation_strategy(
    strategy_id: str,
    parameters: Mapping[str, object],
) -> SimSmartDCA | SimWeeklyTrend | SimBasisCarry | SimWickCatcher:
    if strategy_id == "qt5_sim_smart_dca":
        return SimSmartDCA(cast(SmartDCAConfig, _configured(SmartDCAConfig(), parameters)))
    if strategy_id == "qt5_sim_weekly_trend":
        return SimWeeklyTrend(cast(WeeklyTrendConfig, _configured(WeeklyTrendConfig(), parameters)))
    if strategy_id == "qt5_sim_basis_carry":
        return SimBasisCarry(cast(BasisCarryConfig, _configured(BasisCarryConfig(), parameters)))
    if strategy_id == "qt5_sim_wick_catcher":
        return SimWickCatcher(cast(WickCatcherConfig, _configured(WickCatcherConfig(), parameters)))
    raise KeyError(f"unknown Qt5 simulation port: {strategy_id}")


def _validate_required(metadata: GalleryPortMetadata, inputs: tuple[DataVersion, ...]) -> None:
    available = {item.dataset_id for item in inputs}
    missing = [dependency.dataset_id for dependency in metadata.required_data if dependency.dataset_id not in available]
    if missing:
        raise GalleryPortDataError(f"{metadata.strategy_id} requires {', '.join(missing)}")


def _validate_live_versions(metadata: GalleryPortMetadata, context: GalleryCausalInput) -> None:
    versions = {item.dataset_id for item in context.inputs}
    declared = (*metadata.required_data, *metadata.optional_data)
    missing = [item.dataset_id for item in declared if item.dataset_id in context.data and item.dataset_id not in versions]
    if missing:
        raise GalleryPortDataError(f"{metadata.strategy_id} has unversioned supplied data: {', '.join(missing)}")


def _required_dataframe(data: Mapping[str, object], name: str) -> DataFrame:
    value = data.get(name)
    if not isinstance(value, DataFrame):
        raise GalleryPortDataError(f"{name} must be a pandas DataFrame")
    return value.copy(deep=True)


def _required_series(data: Mapping[str, object], name: str) -> Series:
    value = data.get(name)
    if not isinstance(value, Series):
        raise GalleryPortDataError(f"{name} must be a pandas Series")
    return value.copy(deep=True)


def _optional_series(data: Mapping[str, object], name: str) -> Series | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, Series):
        raise GalleryPortDataError(f"{name} must be a pandas Series when supplied")
    return value.copy(deep=True)


def _copy_value(value: object) -> object:
    if isinstance(value, DataFrame | Series):
        return value.copy(deep=True)
    return deepcopy(value)


def _configured(config: object, parameters: Mapping[str, object]) -> object:
    if isinstance(config, SmartDCAConfig | WeeklyTrendConfig | BasisCarryConfig | WickCatcherConfig):
        names = {item.name for item in fields(config)}
    else:
        raise GalleryPortDataError("unsupported simulation configuration")
    unknown = sorted(set(parameters) - names)
    if unknown:
        raise GalleryPortDataError(f"unsupported source configuration: {', '.join(unknown)}")
    for name, value in parameters.items():
        setattr(config, name, value)
    return config


def _visible_value(value: object, decision_at: datetime) -> object:
    if not isinstance(value, DataFrame | Series):
        return _copy_value(value)
    copied = value.copy(deep=True)
    if not isinstance(copied.index, pd.DatetimeIndex):
        return copied
    if copied.index.tz is None:
        raise GalleryPortDataError("time-indexed source data must be timezone-aware")
    if not copied.index.is_monotonic_increasing or not copied.index.is_unique:
        raise GalleryPortDataError("time-indexed source data must be unique and increasing")
    return copied.loc[copied.index <= pd.Timestamp(decision_at)].copy(deep=True)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GalleryPortDataError(f"{label} must be timezone-aware")


__all__ = [
    "GALLERY_PORTS",
    "GalleryCausalInput",
    "GalleryLiveDecision",
    "GalleryLivePort",
    "GalleryPortDataError",
    "GalleryPortMetadata",
    "GallerySimulationDecision",
    "GallerySimulationPort",
    "create_gallery_port",
]
