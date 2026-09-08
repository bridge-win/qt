# Migrated verbatim from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


CATALOG_SCHEMA_VERSION = 1
CATALOG_SIGNAL_COUNT = 50
CATALOG_PROFILE_COUNT = 100
RUNTIME_CATALOG_PATH = Path("user_data/strategies/catalog_profiles.json")

Direction = Literal["entry", "exit", "both"]


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    minimum: float
    maximum: float
    default: float


@dataclass(frozen=True)
class SignalDefinition:
    id: str
    category: str
    direction: Direction
    required_indicators: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...]
    evaluator: str


@dataclass(frozen=True)
class StrategyProfile:
    id: str
    family: str
    version: int
    entry_signals: tuple[str, ...]
    exit_signals: tuple[str, ...]
    risk: dict[str, float]
    signal_params: dict[str, dict[str, float]]
    pair: str = "BTC/USDT"
    timeframe: str = "4h"


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    signals: tuple[SignalDefinition, ...]
    profiles: tuple[StrategyProfile, ...]


@dataclass(frozen=True)
class RuntimeCatalogArtifact:
    path: Path
    sha256: str
    profile_count: int
    signal_count: int


def build_catalog() -> Catalog:
    signals = tuple(_build_signals())
    profiles = tuple(_build_profiles())
    catalog = Catalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        signals=signals,
        profiles=profiles,
    )
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: Catalog) -> None:
    if catalog.schema_version != CATALOG_SCHEMA_VERSION:
        raise ValueError(f"unsupported catalog schema version: {catalog.schema_version}")
    if len(catalog.signals) != CATALOG_SIGNAL_COUNT:
        raise ValueError(f"catalog must contain exactly {CATALOG_SIGNAL_COUNT} signals")
    if len(catalog.profiles) != CATALOG_PROFILE_COUNT:
        raise ValueError(f"catalog must contain exactly {CATALOG_PROFILE_COUNT} profiles")

    signal_ids = [signal.id for signal in catalog.signals]
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("signal IDs must be unique")
    signal_by_id = {signal.id: signal for signal in catalog.signals}

    profile_ids = [profile.id for profile in catalog.profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ValueError("profile IDs must be unique")

    for signal in catalog.signals:
        if not signal.parameters:
            raise ValueError(f"signal {signal.id} must declare parameters")
        for parameter in signal.parameters:
            if parameter.minimum > parameter.default or parameter.default > parameter.maximum:
                raise ValueError(f"signal {signal.id} parameter {parameter.name} default is out of range")

    for profile in catalog.profiles:
        if profile.pair != "BTC/USDT" or profile.timeframe != "4h":
            raise ValueError(f"profile {profile.id} is not compatible with BTC/USDT 4h")
        if not profile.entry_signals:
            raise ValueError(f"profile {profile.id} must define entry signals")
        if not profile.exit_signals:
            raise ValueError(f"profile {profile.id} must define exit signals")
        for signal_id in (*profile.entry_signals, *profile.exit_signals):
            if signal_id not in signal_by_id:
                raise ValueError(f"profile {profile.id} references unknown signal {signal_id}")

        risk_per_trade = float(profile.risk.get("risk_per_trade", 0.0))
        atr_multiple = float(profile.risk.get("atr_multiple", 0.0))
        if not 0.0025 <= risk_per_trade <= 0.01:
            raise ValueError(f"profile {profile.id} risk_per_trade is out of range")
        if not 1.5 <= atr_multiple <= 4.0:
            raise ValueError(f"profile {profile.id} atr_multiple is out of range")

        for signal_id, params in profile.signal_params.items():
            if signal_id not in signal_by_id:
                raise ValueError(f"profile {profile.id} has params for unknown signal {signal_id}")
            specs = {parameter.name: parameter for parameter in signal_by_id[signal_id].parameters}
            for name, value in params.items():
                if name not in specs:
                    raise ValueError(f"profile {profile.id} parameter {signal_id}.{name} is not supported")
                spec = specs[name]
                if float(value) < spec.minimum or float(value) > spec.maximum:
                    raise ValueError(f"profile {profile.id} parameter {signal_id}.{name} is out of range")


def runtime_catalog_payload(catalog: Catalog) -> dict[str, Any]:
    validate_catalog(catalog)
    payload: dict[str, Any] = {
        "schema_version": catalog.schema_version,
        "signals": [_signal_payload(signal) for signal in catalog.signals],
        "profiles": [_profile_payload(profile) for profile in catalog.profiles],
    }
    payload["sha256"] = catalog_sha256(payload)
    return payload


def write_runtime_catalog(*, root_dir: Path) -> RuntimeCatalogArtifact:
    catalog = build_catalog()
    payload = runtime_catalog_payload(catalog)
    path = root_dir / RUNTIME_CATALOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(payload, pretty=True), encoding="utf-8")
    return RuntimeCatalogArtifact(
        path=path,
        sha256=str(payload["sha256"]),
        profile_count=len(catalog.profiles),
        signal_count=len(catalog.signals),
    )


def load_runtime_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_hash = payload.get("sha256")
    actual_hash = catalog_sha256(payload)
    if expected_hash != actual_hash:
        raise ValueError(f"runtime catalog hash mismatch: expected {expected_hash}, got {actual_hash}")
    catalog = _catalog_from_payload(payload)
    validate_catalog(catalog)
    return payload


def catalog_sha256(payload: dict[str, Any]) -> str:
    payload_without_hash = {key: value for key, value in payload.items() if key != "sha256"}
    return hashlib.sha256(_canonical_json(payload_without_hash, pretty=False).encode("utf-8")).hexdigest()


def profile_by_id(catalog: Catalog, profile_id: str) -> StrategyProfile:
    for profile in catalog.profiles:
        if profile.id == profile_id:
            return profile
    raise ValueError(f"unknown catalog profile: {profile_id}")


def _canonical_json(payload: dict[str, Any], *, pretty: bool) -> str:
    if pretty:
        return json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _signal_payload(signal: SignalDefinition) -> dict[str, Any]:
    return {
        "id": signal.id,
        "category": signal.category,
        "direction": signal.direction,
        "required_indicators": list(signal.required_indicators),
        "parameters": [asdict(parameter) for parameter in signal.parameters],
        "evaluator": signal.evaluator,
    }


def _profile_payload(profile: StrategyProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "family": profile.family,
        "version": profile.version,
        "entry_signals": list(profile.entry_signals),
        "exit_signals": list(profile.exit_signals),
        "risk": dict(sorted(profile.risk.items())),
        "signal_params": {
            signal_id: dict(sorted(params.items()))
            for signal_id, params in sorted(profile.signal_params.items())
        },
        "pair": profile.pair,
        "timeframe": profile.timeframe,
    }


def _catalog_from_payload(payload: dict[str, Any]) -> Catalog:
    signals = tuple(
        SignalDefinition(
            id=str(signal["id"]),
            category=str(signal["category"]),
            direction=signal["direction"],
            required_indicators=tuple(str(item) for item in signal["required_indicators"]),
            parameters=tuple(ParameterSpec(**parameter) for parameter in signal["parameters"]),
            evaluator=str(signal["evaluator"]),
        )
        for signal in payload["signals"]
    )
    profiles = tuple(
        StrategyProfile(
            id=str(profile["id"]),
            family=str(profile["family"]),
            version=int(profile["version"]),
            entry_signals=tuple(str(item) for item in profile["entry_signals"]),
            exit_signals=tuple(str(item) for item in profile["exit_signals"]),
            risk={str(key): float(value) for key, value in profile["risk"].items()},
            signal_params={
                str(signal_id): {str(name): float(value) for name, value in params.items()}
                for signal_id, params in profile["signal_params"].items()
            },
            pair=str(profile.get("pair", "BTC/USDT")),
            timeframe=str(profile.get("timeframe", "4h")),
        )
        for profile in payload["profiles"]
    )
    return Catalog(
        schema_version=int(payload["schema_version"]),
        signals=signals,
        profiles=profiles,
    )


def _params(*names: str) -> tuple[ParameterSpec, ...]:
    specs = {
        "period": ParameterSpec("period", 3, 240, 20),
        "fast_period": ParameterSpec("fast_period", 3, 120, 12),
        "slow_period": ParameterSpec("slow_period", 5, 260, 26),
        "signal_period": ParameterSpec("signal_period", 3, 60, 9),
        "threshold": ParameterSpec("threshold", -100, 100, 0),
        "upper": ParameterSpec("upper", 1, 100, 70),
        "lower": ParameterSpec("lower", 0, 99, 30),
        "multiple": ParameterSpec("multiple", 0.1, 10, 2),
        "lookback": ParameterSpec("lookback", 3, 240, 20),
        "stddev": ParameterSpec("stddev", 0.5, 5, 2),
        "atr_multiple": ParameterSpec("atr_multiple", 0.5, 8, 2),
        "min_value": ParameterSpec("min_value", 0, 1, 0.01),
        "max_value": ParameterSpec("max_value", 0, 1, 0.12),
    }
    return tuple(specs[name] for name in names)


def _build_signals() -> list[SignalDefinition]:
    raw = [
        ("close_above_ema", "trend", "both", ("ema",), _params("period"), "close_above_ema"),
        ("close_below_ema", "trend", "both", ("ema",), _params("period"), "close_below_ema"),
        ("ema_fast_above_slow", "trend", "both", ("ema_fast", "ema_slow"), _params("fast_period", "slow_period"), "ema_fast_above_slow"),
        ("ema_fast_below_slow", "trend", "both", ("ema_fast", "ema_slow"), _params("fast_period", "slow_period"), "ema_fast_below_slow"),
        ("ema_cross_up", "trend", "entry", ("ema_fast", "ema_slow"), _params("fast_period", "slow_period"), "ema_cross_up"),
        ("ema_cross_down", "trend", "exit", ("ema_fast", "ema_slow"), _params("fast_period", "slow_period"), "ema_cross_down"),
        ("adx_trending", "trend", "entry", ("adx",), _params("period", "threshold"), "adx_trending"),
        ("adx_weak", "trend", "exit", ("adx",), _params("period", "threshold"), "adx_weak"),
        ("supertrend_bullish", "trend", "entry", ("supertrend",), _params("period", "atr_multiple"), "supertrend_bullish"),
        ("supertrend_bearish", "trend", "exit", ("supertrend",), _params("period", "atr_multiple"), "supertrend_bearish"),
        ("rsi_oversold", "mean_reversion", "entry", ("rsi",), _params("period", "lower"), "rsi_oversold"),
        ("rsi_overbought", "mean_reversion", "exit", ("rsi",), _params("period", "upper"), "rsi_overbought"),
        ("rsi_cross_up", "mean_reversion", "entry", ("rsi",), _params("period", "lower"), "rsi_cross_up"),
        ("rsi_cross_down", "mean_reversion", "exit", ("rsi",), _params("period", "upper"), "rsi_cross_down"),
        ("bollinger_lower_reentry", "mean_reversion", "entry", ("bb_lower",), _params("lookback", "stddev"), "bollinger_lower_reentry"),
        ("bollinger_upper_reentry", "mean_reversion", "exit", ("bb_upper",), _params("lookback", "stddev"), "bollinger_upper_reentry"),
        ("zscore_low", "mean_reversion", "entry", ("zscore",), _params("lookback", "threshold"), "zscore_low"),
        ("zscore_high", "mean_reversion", "exit", ("zscore",), _params("lookback", "threshold"), "zscore_high"),
        ("stoch_oversold_cross", "mean_reversion", "entry", ("stoch_k", "stoch_d"), _params("period", "lower"), "stoch_oversold_cross"),
        ("stoch_overbought_cross", "mean_reversion", "exit", ("stoch_k", "stoch_d"), _params("period", "upper"), "stoch_overbought_cross"),
        ("macd_bullish", "momentum", "entry", ("macd",), _params("fast_period", "slow_period", "signal_period"), "macd_bullish"),
        ("macd_bearish", "momentum", "exit", ("macd",), _params("fast_period", "slow_period", "signal_period"), "macd_bearish"),
        ("macd_cross_up", "momentum", "entry", ("macd",), _params("fast_period", "slow_period", "signal_period"), "macd_cross_up"),
        ("macd_cross_down", "momentum", "exit", ("macd",), _params("fast_period", "slow_period", "signal_period"), "macd_cross_down"),
        ("roc_positive", "momentum", "entry", ("roc",), _params("period", "threshold"), "roc_positive"),
        ("roc_negative", "momentum", "exit", ("roc",), _params("period", "threshold"), "roc_negative"),
        ("cci_oversold_recovery", "momentum", "entry", ("cci",), _params("period", "lower"), "cci_oversold_recovery"),
        ("cci_overbought_reversal", "momentum", "exit", ("cci",), _params("period", "upper"), "cci_overbought_reversal"),
        ("mfi_oversold", "momentum", "entry", ("mfi",), _params("period", "lower"), "mfi_oversold"),
        ("mfi_overbought", "momentum", "exit", ("mfi",), _params("period", "upper"), "mfi_overbought"),
        ("atr_pct_above", "volatility_range", "entry", ("atr_pct",), _params("period", "min_value"), "atr_pct_above"),
        ("atr_pct_below", "volatility_range", "exit", ("atr_pct",), _params("period", "max_value"), "atr_pct_below"),
        ("bb_width_expanding", "volatility_range", "entry", ("bb_width",), _params("lookback"), "bb_width_expanding"),
        ("bb_width_contracting", "volatility_range", "exit", ("bb_width",), _params("lookback"), "bb_width_contracting"),
        ("keltner_breakout_up", "volatility_range", "entry", ("keltner_upper",), _params("period", "atr_multiple"), "keltner_breakout_up"),
        ("keltner_breakout_down", "volatility_range", "exit", ("keltner_lower",), _params("period", "atr_multiple"), "keltner_breakout_down"),
        ("donchian_breakout_up", "volatility_range", "entry", ("donchian_upper",), _params("lookback"), "donchian_breakout_up"),
        ("donchian_breakout_down", "volatility_range", "exit", ("donchian_lower",), _params("lookback"), "donchian_breakout_down"),
        ("squeeze_release_up", "volatility_range", "entry", ("bb_width", "keltner_upper"), _params("lookback"), "squeeze_release_up"),
        ("squeeze_release_down", "volatility_range", "exit", ("bb_width", "keltner_lower"), _params("lookback"), "squeeze_release_down"),
        ("volume_spike", "volume_price", "entry", ("volume_ratio",), _params("lookback", "multiple"), "volume_spike"),
        ("volume_dry_up", "volume_price", "exit", ("volume_ratio",), _params("lookback", "multiple"), "volume_dry_up"),
        ("obv_rising", "volume_price", "entry", ("obv",), _params("lookback"), "obv_rising"),
        ("obv_falling", "volume_price", "exit", ("obv",), _params("lookback"), "obv_falling"),
        ("cmf_positive", "volume_price", "entry", ("cmf",), _params("period", "threshold"), "cmf_positive"),
        ("cmf_negative", "volume_price", "exit", ("cmf",), _params("period", "threshold"), "cmf_negative"),
        ("vwap_above", "volume_price", "entry", ("vwap",), _params("period"), "vwap_above"),
        ("vwap_below", "volume_price", "exit", ("vwap",), _params("period"), "vwap_below"),
        ("drawdown_fear", "volume_price", "entry", ("fear_drawdown",), _params("lookback", "min_value"), "drawdown_fear"),
        ("drawdown_recovery", "volume_price", "exit", ("fear_drawdown",), _params("lookback", "max_value"), "drawdown_recovery"),
    ]
    return [
        SignalDefinition(
            id=signal_id,
            category=category,
            direction=direction,
            required_indicators=required_indicators,
            parameters=parameters,
            evaluator=evaluator,
        )
        for signal_id, category, direction, required_indicators, parameters, evaluator in raw
    ]


def _build_profiles() -> list[StrategyProfile]:
    family_specs = {
        "ema-pullback": (("ema_fast_above_slow", "rsi_cross_up"), ("ema_cross_down", "rsi_overbought")),
        "donchian-breakout": (("donchian_breakout_up", "adx_trending", "volume_spike"), ("donchian_breakout_down", "macd_cross_down")),
        "rsi-bollinger": (("rsi_oversold", "bollinger_lower_reentry", "adx_weak"), ("rsi_overbought", "bollinger_upper_reentry")),
        "macd-momentum": (("macd_bullish", "macd_cross_up", "close_above_ema"), ("macd_bearish", "roc_negative")),
        "atr-squeeze": (("bb_width_contracting", "squeeze_release_up", "atr_pct_above"), ("squeeze_release_down", "atr_pct_below")),
        "volume-vwap": (("volume_spike", "vwap_above", "obv_rising"), ("vwap_below", "volume_dry_up")),
        "fear-reversal": (("drawdown_fear", "volume_spike", "cci_oversold_recovery"), ("drawdown_recovery", "rsi_overbought")),
        "trend-regime": (("adx_trending", "close_above_ema", "ema_fast_above_slow"), ("adx_weak", "close_below_ema")),
        "keltner-breakout": (("keltner_breakout_up", "roc_positive", "cmf_positive"), ("keltner_breakout_down", "cmf_negative")),
        "multi-factor": (("ema_fast_above_slow", "macd_bullish", "volume_spike"), ("ema_fast_below_slow", "macd_cross_down", "vwap_below")),
    }
    profiles: list[StrategyProfile] = []
    for family, (entry_signals, exit_signals) in family_specs.items():
        for version in range(1, 11):
            profiles.append(
                StrategyProfile(
                    id=f"{family}-v{version:02d}",
                    family=family,
                    version=version,
                    entry_signals=entry_signals,
                    exit_signals=exit_signals,
                    risk={
                        "risk_per_trade": round(0.0025 + (version - 1) * 0.0007, 4),
                        "atr_multiple": round(1.6 + (version - 1) * 0.22, 2),
                    },
                    signal_params=_profile_params((*entry_signals, *exit_signals), version),
                )
            )
    return profiles


def _profile_params(signal_ids: tuple[str, ...], version: int) -> dict[str, dict[str, float]]:
    signal_parameters = {
        signal.id: {parameter.name for parameter in signal.parameters}
        for signal in _build_signals()
    }
    params: dict[str, dict[str, float]] = {}
    for signal_id in signal_ids:
        values = {
            "period": float(10 + version * 2),
            "fast_period": float(8 + version),
            "slow_period": float(24 + version * 2),
            "signal_period": 9.0,
            "threshold": 0.0,
            "upper": float(min(85, 65 + version)),
            "lower": float(max(15, 35 - version)),
            "multiple": round(1.1 + version * 0.12, 2),
            "lookback": float(15 + version * 4),
            "stddev": round(1.5 + version * 0.08, 2),
            "atr_multiple": round(1.4 + version * 0.18, 2),
            "min_value": round(0.004 + version * 0.003, 4),
            "max_value": round(0.12 - version * 0.006, 4),
        }
        params[signal_id] = {
            name: value
            for name, value in values.items()
            if name in signal_parameters[signal_id]
        }
    return params

