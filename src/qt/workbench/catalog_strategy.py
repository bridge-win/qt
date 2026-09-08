"""Deprecated catalog signal evaluator retained for historical comparison only.

This target-weight class is intentionally not registered in the v3 API or
native executor. It cannot reproduce Freqtrade stoploss, ROI, leverage,
sizing, or order callbacks. Runnable catalog profiles require the explicit
version-pinned source port and its real legacy runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import StrategyContext, StrategyMetadata
from btc_backtest.strategies.target_weight import TargetWeightStrategy

from qt.workbench.catalog import add_catalog_indicators, legacy_catalog, profile
from qt.workbench.catalog_signals import evaluate_signal


class CatalogProfileStrategy(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="legacy_catalog_profile",
        version="btc-quant-e2ecad2",
        description="Historical signal-only evaluator; not Freqtrade-compatible.",
        warmup_bars=260,
        supported_timeframes=("4h",),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema={"profile_id": {"type": "string"}},
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values = dict(parameters or {})
        profile_id = values.pop("profile_id", None)
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("catalog profile_id is required")
        super().__init__(values)
        self.profile = profile(profile_id)
        catalog = legacy_catalog()
        self.catalog_sha256 = str(catalog["sha256"])
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        if len(context.bars) < self.metadata.warmup_bars:
            return self._target
        enriched = add_catalog_indicators(context.bars)
        entry = _all_signals(enriched, self.profile, "entry_signals")
        exit_ = _any_signals(enriched, self.profile, "exit_signals")
        if bool(exit_.iloc[-1]):
            self._target = Decimal("0")
        elif bool(entry.iloc[-1]):
            self._target = Decimal("1")
        return self._target

    def rebalance_reason(self, *, current_value: Decimal, target_value: Decimal) -> str:
        action = "entry" if target_value > current_value else "exit"
        return f"catalog:{self.profile['id']}:{self.catalog_sha256[:12]}:{action}"


def _all_signals(frame: object, values: Mapping[str, object], key: str):
    import pandas as pd

    assert isinstance(frame, pd.DataFrame)
    signal_ids = values[key]
    if not isinstance(signal_ids, list):
        raise ValueError(f"catalog {key} must be a list")
    result = pd.Series(True, index=frame.index)
    params = values.get("signal_params", {})
    if not isinstance(params, Mapping):
        raise ValueError("catalog signal_params must be an object")
    for signal_id in signal_ids:
        if not isinstance(signal_id, str):
            raise ValueError("catalog signal id must be a string")
        signal_params = params.get(signal_id, {})
        if not isinstance(signal_params, Mapping):
            raise ValueError("catalog signal parameters must be an object")
        result &= evaluate_signal(signal_id, frame, signal_params)
    return result


def _any_signals(frame: object, values: Mapping[str, object], key: str):
    import pandas as pd

    assert isinstance(frame, pd.DataFrame)
    signal_ids = values[key]
    if not isinstance(signal_ids, list):
        raise ValueError(f"catalog {key} must be a list")
    result = pd.Series(False, index=frame.index)
    params = values.get("signal_params", {})
    if not isinstance(params, Mapping):
        raise ValueError("catalog signal_params must be an object")
    for signal_id in signal_ids:
        if not isinstance(signal_id, str):
            raise ValueError("catalog signal id must be a string")
        signal_params = params.get(signal_id, {})
        if not isinstance(signal_params, Mapping):
            raise ValueError("catalog signal parameters must be an object")
        result |= evaluate_signal(signal_id, frame, signal_params)
    return result
