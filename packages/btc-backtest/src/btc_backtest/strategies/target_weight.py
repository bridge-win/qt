"""Shared long/flat target-weight allocation adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

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

_BPS = Decimal("10000")


class TargetWeightStrategy(ABC):
    """Translate a bounded spot target weight into one market order intent."""

    metadata: StrategyMetadata

    def __init__(
        self,
        parameters: Mapping[str, object] | None = None,
    ) -> None:
        values = dict(parameters or {})
        tolerance = _decimal(
            values.pop("rebalance_tolerance", Decimal("0.005")),
            "rebalance_tolerance",
        )
        if tolerance < 0 or tolerance > 1:
            raise ValueError(
                "rebalance_tolerance must be between zero and one"
            )
        self.parameters = MappingProxyType(values)
        self.rebalance_tolerance = tolerance
        self._execution_multiplier = Decimal("1")

    def initialize(self, context: InitializationContext) -> None:
        slippage = context.spec.slippage_bps / _BPS
        fee = context.spec.fee_bps / _BPS
        self._execution_multiplier = (
            (Decimal("1") + slippage) * (Decimal("1") + fee)
        )

    @abstractmethod
    def target_weight(self, context: StrategyContext) -> Decimal:
        """Return the desired point-in-time BTC fraction of equity."""

    def on_bar(self, context: StrategyContext) -> tuple[OrderIntent, ...]:
        desired = _decimal(
            self.target_weight(context),
            "target_weight",
        )
        bounded = min(
            self.metadata.max_weight,
            max(self.metadata.min_weight, desired),
        )
        equity = context.portfolio.equity
        if equity <= 0:
            return ()
        close = _decimal(context.current_bar["close"], "close")
        if close <= 0:
            raise ValueError("close must be positive")
        position = context.portfolio.position(InstrumentKind.SPOT)
        current_value = position.quantity * close
        target_value = equity * bounded
        difference = target_value - current_value
        if abs(difference) / equity <= self.rebalance_tolerance:
            return ()
        if difference > 0:
            affordable = (
                context.portfolio.cash / self._execution_multiplier
            )
            quote_amount = min(difference, affordable)
            if quote_amount <= 0:
                return ()
            return (
                OrderIntent(
                    instrument=InstrumentKind.SPOT,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quote_amount=quote_amount,
                    reason=self.rebalance_reason(
                        current_value=current_value,
                        target_value=target_value,
                    ),
                ),
            )

        base_quantity = min(
            -difference / close,
            position.quantity,
        )
        if base_quantity <= 0:
            return ()
        return (
            OrderIntent(
                instrument=InstrumentKind.SPOT,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                base_quantity=base_quantity,
                reason=self.rebalance_reason(
                    current_value=current_value,
                    target_value=target_value,
                ),
            ),
        )

    def rebalance_reason(
        self,
        *,
        current_value: Decimal,
        target_value: Decimal,
    ) -> str:
        if target_value > current_value:
            return "target_weight_increase"
        return "target_weight_decrease"

    def finalize(self, context: FinalizationContext) -> None:
        return None


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result
