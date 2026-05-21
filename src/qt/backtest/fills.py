"""Fill model: explicit fees and slippage applied to every order."""

from __future__ import annotations

from dataclasses import dataclass

from qt.core.types import OrderSide


@dataclass(frozen=True)
class FillModel:
    fee_bps: float = 5.0          # taker fee, basis points (5 = 0.05%)
    slippage_bps: float = 8.0     # adverse slippage from mid

    def fill_price(self, mark_price: float, side: OrderSide) -> float:
        slip = self.slippage_bps / 10_000
        return mark_price * (1 + slip) if side == OrderSide.BUY else mark_price * (1 - slip)

    def fee(self, notional: float) -> float:
        return abs(notional) * (self.fee_bps / 10_000)
