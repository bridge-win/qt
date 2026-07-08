"""Batch backtest of the Wick Catcher ladder (see qt.strategies.wick_catcher
for the live version and docs/RESEARCH-EARNING.md §2 for the evidence).

Event-driven over OHLCV bars:

- flat: if a bar's low pierces rung(s), fill AT the rung price(s) (that is
  the whole point of a resting limit order) minus fees; deepest rung sets
  the position's entry for exit logic.
- long: exit at take-profit (+tp% from volume-weighted entry), hard stop
  (hard_stop% below the deepest configured rung — a regime break), or a
  time stop after max_hold_bars.

Costs: taker-equivalent fee+slippage on exits; maker fee on rung fills
(limit orders earn maker pricing; we still charge slippage_bps=0 there
because a resting limit fills at its own price by definition).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class WickCatcherConfig:
    initial_cash: float = 10_000.0
    rungs_pct: tuple[float, ...] = (0.05, 0.08, 0.12)
    reference_bars: int = 24
    take_profit_pct: float = 0.05
    hard_stop_pct: float = 0.05
    max_hold_bars: int = 72
    per_rung_alloc: float = 0.03
    maker_fee_bps: float = 2.0     # limit fills earn maker pricing
    taker_fee_bps: float = 10.0    # exits cross the spread
    slippage_bps: float = 5.0      # exit slippage


@dataclass
class WickCatcherResult:
    equity: pd.Series
    trades: pd.DataFrame
    diagnostics: pd.DataFrame = field(default_factory=pd.DataFrame)


class WickCatcher:
    """Ladder backtest. `run(ohlcv)` -> WickCatcherResult."""

    def __init__(self, cfg: WickCatcherConfig | None = None) -> None:
        self.cfg = cfg or WickCatcherConfig()

    def run(self, ohlcv: pd.DataFrame) -> WickCatcherResult:
        cfg = self.cfg
        close = ohlcv["close"]
        low = ohlcv["low"]
        reference = close.rolling(cfg.reference_bars, min_periods=1).median()

        cash = float(cfg.initial_cash)
        qty = 0.0
        entry_cost = 0.0            # quote spent on the open position (incl fees)
        entry_price = 0.0           # volume-weighted fill price
        ladder_bottom = 0.0         # frozen at entry — stop must not chase price down
        bars_held = 0
        maker = cfg.maker_fee_bps / 10_000.0
        taker = (cfg.taker_fee_bps + cfg.slippage_bps) / 10_000.0

        equity_pts: list[float] = []
        trade_rows: list[dict[str, object]] = []

        for ts in ohlcv.index:
            px = float(close.loc[ts])
            lo = float(low.loc[ts])
            ref = float(reference.loc[ts])
            rungs = [ref * (1.0 - d) for d in cfg.rungs_pct]

            if qty > 0:
                bars_held += 1
                hard_stop = ladder_bottom * (1.0 - cfg.hard_stop_pct)
                tp = entry_price * (1.0 + cfg.take_profit_pct)
                exit_reason = None
                exit_px = px
                if px >= tp:
                    exit_reason = "take_profit"
                elif px <= hard_stop:
                    exit_reason = "hard_stop"
                elif bars_held >= cfg.max_hold_bars:
                    exit_reason = "time_stop"
                if exit_reason:
                    proceeds = qty * exit_px * (1.0 - taker)
                    pnl = proceeds - entry_cost
                    cash += proceeds
                    trade_rows.append({
                        "ts": ts, "side": "sell", "qty": qty, "price": exit_px,
                        "fee": qty * exit_px * taker, "pnl": pnl,
                        "reason": exit_reason, "holding_bars": bars_held,
                    })
                    qty = 0.0
                    entry_cost = 0.0
                    entry_price = 0.0
                    ladder_bottom = 0.0
                    bars_held = 0
            else:
                pierced = [r for r in rungs if lo <= r]
                if pierced:
                    equity_now = cash  # flat, so equity == cash
                    spend_per_rung = equity_now * cfg.per_rung_alloc
                    total_qty = 0.0
                    total_cost = 0.0
                    for r in pierced:
                        spend = min(spend_per_rung, cash - total_cost)
                        if spend <= 0:
                            break
                        fill_qty = (spend * (1.0 - maker)) / r
                        total_qty += fill_qty
                        total_cost += spend
                        trade_rows.append({
                            "ts": ts, "side": "buy", "qty": fill_qty, "price": r,
                            "fee": spend * maker, "pnl": 0.0,
                            "reason": "rung_fill", "holding_bars": 0,
                        })
                    if total_qty > 0:
                        cash -= total_cost
                        qty = total_qty
                        entry_cost = total_cost
                        entry_price = total_cost / total_qty
                        ladder_bottom = min(rungs)
                        bars_held = 0

            equity_pts.append(cash + qty * px)

        equity = pd.Series(equity_pts, index=ohlcv.index, name="equity")
        trades = pd.DataFrame(trade_rows)
        diags = pd.DataFrame({"reference": reference})
        return WickCatcherResult(equity=equity, trades=trades, diagnostics=diags)
