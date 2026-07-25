"""Independent ports of QT capitulation and wick-catcher strategies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import numpy as np
import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

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
from btc_backtest.strategies.indicators import rsi
from btc_backtest.strategies.target_weight import TargetWeightStrategy


class CapitulationParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score_min: Decimal = Field(default=Decimal("0.60"), ge=0, le=1)
    exit_score: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    min_groups_firing: int = Field(default=4, ge=1, le=5)
    allocation: Decimal = Field(default=Decimal("0.20"), gt=0, le=1)
    rebalance_tolerance: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        le=1,
    )

    @model_validator(mode="after")
    def validate_scores(self) -> CapitulationParams:
        if self.exit_score >= self.score_min:
            raise ValueError("capitulation exit_score must be below score_min")
        return self


class Capitulation(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="capitulation",
        version="1.0.0",
        description="Buy multi-factor BTC capitulation with a macro veto.",
        warmup_bars=720,
        supported_timeframes=("1h", "1d"),
        signal_dependencies=(
            "funding",
            "open_interest",
            "long_short_ratio",
            "mvrv",
            "fear_greed",
            "macro",
        ),
        parameter_schema=CapitulationParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        self.params = CapitulationParams.model_validate(parameters or {})
        super().__init__(
            {"rebalance_tolerance": self.params.rebalance_tolerance}
        )
        self._active = False

    def initialize(self, context: InitializationContext) -> None:
        super().initialize(context)
        self._active = False

    def target_weight(self, context: StrategyContext) -> Decimal:
        snapshot = _capitulation_snapshot(context)
        triggered = (
            snapshot.macro_ok
            and snapshot.score >= self.params.score_min
            and snapshot.groups_firing >= self.params.min_groups_firing
        )
        if triggered:
            self._active = True
        elif self._active and (
            not snapshot.macro_ok
            or snapshot.score <= self.params.exit_score
        ):
            self._active = False
        return self.params.allocation if self._active else Decimal("0")

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        return (
            "capitulation_entry"
            if target_value > current_value
            else "capitulation_exit"
        )


class WickCatcherParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ladder_drawdowns: tuple[Decimal, ...] = (
        Decimal("0.05"),
        Decimal("0.08"),
        Decimal("0.12"),
    )
    take_profit_pct: Decimal = Field(default=Decimal("0.03"), gt=0, le=1)
    rung_quote: Decimal = Field(default=Decimal("100"), gt=0)
    max_inventory_weight: Decimal = Field(
        default=Decimal("0.30"),
        gt=0,
        le=1,
    )
    vix_max: Decimal = Field(default=Decimal("40"), gt=0)
    dxy_z_max: Decimal = Field(default=Decimal("2.5"), gt=0)

    @field_validator("ladder_drawdowns")
    @classmethod
    def validate_ladder(
        cls,
        values: tuple[Decimal, ...],
    ) -> tuple[Decimal, ...]:
        if not values or len(values) > 20:
            raise ValueError("wick ladder requires between one and 20 rungs")
        if any(
            not value.is_finite() or value <= 0 or value >= 1
            for value in values
        ):
            raise ValueError("wick ladder drawdowns must be between zero and one")
        if len(set(values)) != len(values):
            raise ValueError("wick ladder drawdowns must be unique")
        return tuple(sorted(values))


class WickCatcher:
    metadata = StrategyMetadata(
        id="wick_catcher",
        version="1.0.0",
        description="Maintain a macro-gated deep-limit BTC wick ladder.",
        warmup_bars=2,
        supported_timeframes=("1h", "1d"),
        signal_dependencies=("macro",),
        parameter_schema=WickCatcherParams.model_json_schema(),
    )

    def __init__(self, parameters: Mapping[str, object] | None = None) -> None:
        self.params = WickCatcherParams.model_validate(parameters or {})

    def initialize(self, context: InitializationContext) -> None:
        return None

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        if len(context.bars) < 2:
            return ()
        close = _decimal(context.current_bar["close"])
        if close is None or close <= 0:
            return ()
        position = context.portfolio.position(InstrumentKind.SPOT)
        existing = {
            order.group_id
            for order in context.open_orders
            if order.group_id is not None
        }
        intents = _take_profit_intents(
            context,
            position.quantity,
            existing,
            self.params.take_profit_pct,
        )
        if _macro_veto(context, self.params):
            return tuple(intents)

        inventory_value = position.quantity * close
        maximum_inventory = (
            context.portfolio.equity * self.params.max_inventory_weight
        )
        reserved_buys = sum(
            (
                (order.remaining_quantity or Decimal("0"))
                * (order.limit_price or close)
                for order in context.open_orders
                if (
                    order.instrument is InstrumentKind.SPOT
                    and order.side is OrderSide.BUY
                )
            ),
            Decimal("0"),
        )
        remaining = min(
            max(Decimal("0"), context.portfolio.cash - reserved_buys),
            max(
                Decimal("0"),
                maximum_inventory - inventory_value - reserved_buys,
            ),
        )
        prior_close = _decimal(context.bars["close"].iloc[-2])
        if prior_close is None or prior_close <= 0:
            return tuple(intents)
        for drawdown in self.params.ladder_drawdowns:
            limit_price = prior_close * (Decimal("1") - drawdown)
            group_id = (
                f"wick:buy:{_decimal_text(drawdown)}:"
                f"{_decimal_text(limit_price)}"
            )
            if group_id in existing:
                continue
            quote = min(self.params.rung_quote, remaining)
            if quote <= 0:
                break
            intents.append(
                OrderIntent(
                    instrument=InstrumentKind.SPOT,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quote_amount=quote,
                    limit_price=limit_price,
                    group_id=group_id,
                    reason="wick_ladder_buy",
                )
            )
            remaining -= quote
        return tuple(intents)

    def finalize(self, context: FinalizationContext) -> None:
        return None


@dataclass(frozen=True)
class _CapitulationSnapshot:
    score: Decimal
    groups_firing: int
    macro_ok: bool


def _capitulation_snapshot(context: StrategyContext) -> _CapitulationSnapshot:
    bars = context.bars
    close = bars["close"].astype("float64")
    open_ = bars["open"].astype("float64")
    high = bars["high"].astype("float64")
    low = bars["low"].astype("float64")
    volume = bars["volume"].astype("float64")

    average = close.rolling(20).mean()
    deviation = close.rolling(20).std(ddof=0).replace(0, np.nan)
    bollinger_z = (close - average) / deviation
    body = (close - open_).abs()
    lower_wick = pd.concat((open_, close), axis=1).min(axis=1) - low
    epsilon = (high - low).abs().clip(lower=1e-9) * 1e-3
    wick_ratio = lower_wick.clip(lower=0) / body.where(body > 0, epsilon)
    drawdown = close / close.rolling(720, min_periods=1).max() - 1
    volume_mean = volume.rolling(720).mean()
    volume_std = volume.rolling(720).std(ddof=0).replace(0, np.nan)
    volume_capitulation = (
        (volume - volume_mean) / volume_std >= 2
    ) & (close.pct_change(fill_method=None) <= -0.05)
    price_group = any(
        (
            _latest_bool(rsi(close, 14) < 20),
            _latest_bool(bollinger_z <= -2.5),
            _latest_bool(wick_ratio >= 3),
            _latest_bool(drawdown <= -0.15),
            _latest_bool(volume_capitulation),
        )
    )

    returns = np.log(close / close.shift(1))
    fast = returns.rolling(24).std(ddof=0)
    slow = returns.rolling(720).std(ddof=0).replace(0, np.nan)
    volatility_group = _latest_bool(fast / slow >= 2)

    derivative_flags: list[bool] = []
    funding = _auxiliary_series(context, "funding", ("rate", "funding_rate"))
    if funding is not None:
        aligned = funding.reindex(close.index).ffill()
        mean = aligned.rolling(90).mean()
        std = aligned.rolling(90).std(ddof=0).replace(0, np.nan)
        z_score = (aligned - mean) / std
        sustained = (aligned <= -0.0005).astype(int).rolling(3).sum() >= 3
        derivative_flags.append(
            _latest_bool(z_score.fillna(0) <= -2)
            or _latest_bool(sustained)
        )
    open_interest = _auxiliary_series(
        context,
        "open_interest",
        ("value", "oi_usd"),
    )
    if open_interest is not None:
        aligned = open_interest.reindex(close.index).ffill()
        derivative_flags.append(_latest_bool(aligned.pct_change(24) <= -0.10))
    long_short = _auxiliary_series(
        context,
        "long_short_ratio",
        ("value", "long_short_ratio"),
    )
    if long_short is not None:
        aligned = long_short.reindex(close.index).ffill()
        derivative_flags.append(
            _latest_bool(aligned.rolling(720).rank(pct=True) <= 0.10)
        )

    mvrv = _auxiliary_series(context, "mvrv", ("value", "mvrv"))
    onchain_group = mvrv is not None and _latest_bool(mvrv <= 0)
    fear_greed = _auxiliary_series(
        context,
        "fear_greed",
        ("value", "fear_greed"),
    )
    sentiment_group = fear_greed is not None and _latest_bool(fear_greed <= 20)
    macro_ok = not _macro_veto(context, None)

    available = [price_group, volatility_group]
    fired = [price_group, volatility_group]
    if derivative_flags:
        available.append(True)
        fired.append(any(derivative_flags))
    if mvrv is not None:
        available.append(True)
        fired.append(onchain_group)
    if fear_greed is not None:
        available.append(True)
        fired.append(sentiment_group)
    groups_firing = sum(fired)
    score = (
        Decimal(groups_firing) / Decimal(len(available))
        if macro_ok
        else Decimal("0")
    )
    return _CapitulationSnapshot(
        score=score,
        groups_firing=groups_firing,
        macro_ok=macro_ok,
    )


def _take_profit_intents(
    context: StrategyContext,
    quantity: Decimal,
    existing: set[str],
    take_profit_pct: Decimal,
) -> list[OrderIntent]:
    if quantity <= 0:
        return []
    position = context.portfolio.position(InstrumentKind.SPOT)
    if position.average_price <= 0:
        return []
    price = position.average_price * (
        Decimal("1") + take_profit_pct
    )
    group_id = f"wick:sell:{_decimal_text(price)}"
    if group_id in existing:
        return []
    return [
        OrderIntent(
            instrument=InstrumentKind.SPOT,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            base_quantity=quantity,
            limit_price=price,
            group_id=group_id,
            reason="wick_take_profit",
        )
    ]


def _macro_veto(
    context: StrategyContext,
    params: WickCatcherParams | None,
) -> bool:
    macro = context.auxiliary.get("macro")
    if macro is None or macro.empty:
        return False
    active = macro.iloc[-1]
    vix_max = params.vix_max if params is not None else Decimal("40")
    dxy_z_max = params.dxy_z_max if params is not None else Decimal("2.5")
    vix = _decimal(active.get("vix"))
    dxy_z = _decimal(active.get("dxy_z"))
    return (
        (vix is not None and vix >= vix_max)
        or (dxy_z is not None and dxy_z >= dxy_z_max)
    )


def _auxiliary_series(
    context: StrategyContext,
    name: str,
    columns: tuple[str, ...],
) -> pd.Series | None:
    frame = context.auxiliary.get(name)
    if frame is None or frame.empty:
        return None
    for column in columns:
        if column in frame.columns:
            return pd.to_numeric(frame[column], errors="coerce")
    return None


def _latest_bool(values: pd.Series) -> bool:
    return bool(values.iloc[-1]) if not values.empty else False


def _decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
