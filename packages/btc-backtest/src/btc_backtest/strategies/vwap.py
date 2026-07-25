"""Rolling VWAP mean-reversion strategy."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import StrategyContext, StrategyMetadata
from btc_backtest.strategies.indicators import rolling_vwap
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class VWAPMeanReversionParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    window: int = Field(default=20, ge=2, le=1000)
    entry_z: Decimal = Field(default=Decimal("-1.5"), lt=0)
    exit_z: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_levels(self) -> VWAPMeanReversionParams:
        if self.entry_z >= self.exit_z:
            raise ValueError("entry_z must be below exit_z")
        return self


class VWAPMeanReversion(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="vwap_mean_reversion",
        version="1.0.0",
        description="Buy a negative rolling VWAP z-score and exit at center.",
        warmup_bars=20,
        supported_timeframes=("1h", "1d"),
        supported_instruments=(InstrumentKind.SPOT,),
        parameter_schema=VWAPMeanReversionParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        values, tolerance = _parameters(parameters)
        super().__init__({"rebalance_tolerance": tolerance})
        self.params = VWAPMeanReversionParams.model_validate(values)
        self.metadata = type(self).metadata.model_copy(
            update={"warmup_bars": self.params.window}
        )
        self._target = Decimal("0")

    def target_weight(self, context: StrategyContext) -> Decimal:
        frame = context.bars
        typical = (frame["high"] + frame["low"] + frame["close"]) / 3
        center = rolling_vwap(
            frame["high"],
            frame["low"],
            frame["close"],
            frame["volume"],
            self.params.window,
        )
        deviation = typical.rolling(
            self.params.window,
            min_periods=self.params.window,
        ).std(ddof=0)
        zscore = (typical - center) / deviation.replace(0, np.nan)
        value = float(zscore.iloc[-1])
        if not np.isfinite(value):
            return self._target
        current = Decimal(str(value))
        if self._target == 0 and current <= self.params.entry_z:
            self._target = Decimal("1")
        elif self._target == 1 and current >= self.params.exit_z:
            self._target = Decimal("0")
        return self._target

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return "vwap_negative_z_entry"
        return "vwap_center_exit"


def _parameters(
    parameters: Mapping[str, object] | None,
) -> tuple[dict[str, object], object]:
    values = dict(parameters or {})
    tolerance = values.pop("rebalance_tolerance", Decimal("0.005"))
    return values, tolerance
