"""Diagnostic daily replay: rules only, next-open fills, and explicit costs.

This baseline excludes Codex review, intraday risk checks, exchange liquidity,
and outages. It does not establish profitability or reproduce live execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from qt.btc_agent.signals import DAY_MS, MIN_CANDLES, Candle, evaluate

INITIAL_CASH = 100.0
ORDER_QUOTE = 25.0
FEE_RATE = 0.001
SLIPPAGE_RATE = 0.001
COOLDOWN_MS = 7 * DAY_MS


@dataclass(frozen=True)
class _Intent:
    side: Literal["buy", "sell"]
    signal_time: int
    reason: str


@dataclass
class _Portfolio:
    cash: float = INITIAL_CASH
    quantity: float = 0.0
    cost: float = 0.0
    entry_price: float = 0.0
    entry_time: int = 0
    peak_close: float = 0.0
    last_buy_time: int | None = None
    fees: float = 0.0
    turnover: float = 0.0
    invested_days: int = 0
    peak_equity: float = INITIAL_CASH
    maximum_drawdown: float = 0.0
    fills: list[dict[str, object]] = field(default_factory=list)
    trades: list[dict[str, object]] = field(default_factory=list)
    equity_curve: list[dict[str, object]] = field(default_factory=list)

    def buy(self, candle: Candle, budget: float, intent: _Intent) -> None:
        spend = min(budget, self.cash)
        if spend <= 1e-10:
            return
        price = candle.open * (1 + SLIPPAGE_RATE)
        notional = spend / (1 + FEE_RATE)
        fee = spend - notional
        quantity = notional / price
        if self.quantity == 0:
            self.entry_time = candle.open_time
            self.peak_close = price
        self.quantity += quantity
        self.cost += spend
        self.entry_price = self.cost / self.quantity
        self.last_buy_time = candle.open_time
        self.cash -= spend
        self.fees += fee
        self.turnover += notional
        self._record_fill(candle, intent, price, quantity, fee, notional)

    def sell(self, candle: Candle, intent: _Intent) -> None:
        price = candle.open * (1 - SLIPPAGE_RATE)
        quantity = self.quantity
        notional = quantity * price
        fee = notional * FEE_RATE
        proceeds = notional - fee
        self.cash += proceeds
        self.fees += fee
        self.turnover += notional
        self._record_fill(candle, intent, price, quantity, fee, notional)
        self.trades.append(
            {
                "entry_time": self.entry_time,
                "exit_time": candle.open_time,
                "quantity": quantity,
                "entry_cost_quote": self.cost,
                "exit_proceeds_quote": proceeds,
                "net_pnl_quote": proceeds - self.cost,
                "return_pct": (proceeds / self.cost - 1) * 100,
                "holding_days": (candle.open_time - self.entry_time) / DAY_MS,
                "reason": intent.reason,
            }
        )
        self.quantity = 0.0
        self.cost = 0.0
        self.entry_price = 0.0
        self.peak_close = 0.0

    def _record_fill(
        self,
        candle: Candle,
        intent: _Intent,
        price: float,
        quantity: float,
        fee: float,
        notional: float,
    ) -> None:
        self.fills.append(
            {
                "side": intent.side,
                "signal_bar_time": intent.signal_time,
                "fill_time": candle.open_time,
                "fill_price": price,
                "quantity": quantity,
                "notional_quote": notional,
                "fee_quote": fee,
                "reason": intent.reason,
            }
        )

    def observe(self, candle: Candle) -> None:
        equity = self.cash + self.quantity * candle.close
        self.peak_equity = max(self.peak_equity, equity)
        self.maximum_drawdown = max(self.maximum_drawdown, 1 - equity / self.peak_equity)
        self.invested_days += int(self.quantity > 0)
        self.equity_curve.append(
            {
                "time": candle.open_time + DAY_MS,
                "equity_quote": equity,
            }
        )

    def report(self, last_close: float) -> dict[str, object]:
        equity = self.cash + self.quantity * last_close
        return {
            "initial_equity_quote": INITIAL_CASH,
            "final_equity_quote": equity,
            "net_return_pct": (equity / INITIAL_CASH - 1) * 100,
            "max_drawdown_pct": self.maximum_drawdown * 100,
            "fees_quote": self.fees,
            "turnover_quote": self.turnover,
            "turnover_multiple": self.turnover / INITIAL_CASH,
            "exposure_days_pct": 100 * self.invested_days / len(self.equity_curve),
            "cash_quote": self.cash,
            "open_quantity_btc": self.quantity,
            "open_position_cost_quote": self.cost,
            "fills_count": len(self.fills),
            "closed_trades_count": len(self.trades),
            "fills": self.fills,
            "closed_trades": self.trades,
            "equity_curve": self.equity_curve,
        }


def _risk_reason(portfolio: _Portfolio, candle: Candle) -> str | None:
    portfolio.peak_close = max(portfolio.peak_close, candle.close)
    if candle.close <= portfolio.entry_price * 0.90:
        return "daily_close_stop_10pct"
    if (
        portfolio.peak_close >= portfolio.entry_price * 1.20
        and candle.close <= portfolio.peak_close * 0.85
    ):
        return "daily_close_trail_15pct_after_20pct_gain"
    if candle.open_time + DAY_MS - portfolio.entry_time >= 90 * DAY_MS:
        return "maximum_hold_90days"
    return None


def run_backtest(candles: list[Candle]) -> dict[str, object]:
    """Replay a fixed rule hypothesis and two equal-initial-capital benchmarks.

    All supplied candles are assumed complete. Signals see only prefixes;
    orders fill at the next supplied day's open with adverse slippage. The
    final position is marked to market, not fictitiously liquidated. Monthly
    DCA spends at most 25 USDT on the first available evaluation day and then
    the first day of each UTC calendar month until initial cash is exhausted.
    """
    if len(candles) < MIN_CANDLES + 1:
        raise ValueError(f"At least {MIN_CANDLES + 1} completed daily candles are required")

    strategy = _Portfolio()
    buy_hold = _Portfolio()
    monthly_dca = _Portfolio()
    pending: _Intent | None = None
    previous_month: tuple[int, int] | None = None

    for index in range(MIN_CANDLES - 1, len(candles)):
        candle = candles[index]
        now_ms = candle.open_time + DAY_MS
        # Validate today's complete prefix before accepting its price as a fill.
        # Its signal is evaluated again after a fill if the holding state changes.
        signal = evaluate(candles[: index + 1], now_ms, has_position=strategy.quantity > 0)
        if index >= MIN_CANDLES:
            if pending is not None:
                if pending.side == "buy":
                    strategy.buy(candle, ORDER_QUOTE, pending)
                else:
                    strategy.sell(candle, pending)
                signal = evaluate(candles[: index + 1], now_ms, has_position=strategy.quantity > 0)
            if index == MIN_CANDLES:
                buy_hold.buy(
                    candle,
                    INITIAL_CASH,
                    _Intent("buy", candles[index - 1].open_time, "buy_and_hold"),
                )
            date = datetime.fromtimestamp(candle.open_time / 1000, tz=timezone.utc)
            month = (date.year, date.month)
            if month != previous_month:
                monthly_dca.buy(
                    candle, ORDER_QUOTE, _Intent("buy", candles[index - 1].open_time, "monthly_dca")
                )
                previous_month = month
            for portfolio in (strategy, buy_hold, monthly_dca):
                portfolio.observe(candle)

        pending = None
        if strategy.quantity > 0:
            reason = _risk_reason(strategy, candle)
            if reason is not None or signal.action == "sell":
                pending = _Intent("sell", candle.open_time, reason or "daily_overheat")
        elif signal.action == "buy" and (
            strategy.last_buy_time is None or now_ms >= strategy.last_buy_time + COOLDOWN_MS
        ):
            pending = _Intent("buy", candle.open_time, "daily_capitulation_recovery")

    return {
        "kind": "rule_only_daily_diagnostic",
        "start_time": candles[MIN_CANDLES].open_time,
        "end_time": candles[-1].open_time + DAY_MS,
        "warmup_candles": MIN_CANDLES,
        "evaluation_days": len(candles) - MIN_CANDLES,
        "assumptions": {
            "fee_bps_per_side": FEE_RATE * 10000,
            "adverse_slippage_bps_per_side": SLIPPAGE_RATE * 10000,
            "entry_budget_including_fee_quote": ORDER_QUOTE,
            "cooldown_days": COOLDOWN_MS // DAY_MS,
            "cooldown_from": "last_buy",
            "risk_entry_basis": "entry_cost_including_fee_per_btc",
            "fill_model": "signal_at_completed_daily_close_then_next_day_open",
            "codex_reviewer_included": False,
            "end_valuation": "mark_to_market_without_forced_liquidation",
            "dca_funding": "same_initial_cash_no_deposits",
        },
        "limitations": [
            "Diagnostic rule baseline only; no profitability evidence or Codex evaluation.",
            "Daily-close exits differ from live polling; intraday stops and liquidity are absent.",
            "Orders assume full fills; capped multi-poll exits, spreads and outages are not simulated.",
            "Benchmarks use the same initial capital but different market exposure.",
            "Open holdings exclude a future exit fee and liquidation slippage.",
        ],
        "strategy": strategy.report(candles[-1].close),
        "buy_and_hold": buy_hold.report(candles[-1].close),
        "monthly_dca": monthly_dca.report(candles[-1].close),
        "pending_unfilled_action": pending.side if pending is not None else None,
    }
