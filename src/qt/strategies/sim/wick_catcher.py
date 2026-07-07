"""Batch simulator for the wick-catcher limit ladder."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from qt.strategies.sim.base import StrategyResult


@dataclass
class WickCatcherConfig:
    ladder_drawdowns: tuple[float, ...] = (0.05, 0.08, 0.12)
    take_profit_pct: float = 0.03
    rung_risk_pct: float = 0.02
    max_open_rungs: int = 3
    max_hold_bars: int = 72
    fee_bps: float = 7.5
    slippage_bps: float = 5.0
    initial_cash: float = 10_000.0


class WickCatcher:
    """Replay a deep resting-limit ladder over OHLCV bars."""

    def __init__(self, cfg: WickCatcherConfig | None = None) -> None:
        self.cfg = cfg or WickCatcherConfig()

    def run(self, ohlcv: pd.DataFrame) -> StrategyResult:
        cfg = self.cfg
        if ohlcv.empty:
            empty = pd.Series(dtype="float64")
            return StrategyResult(
                equity=empty,
                target_weight=empty,
                short_weight=empty,
                trades=pd.DataFrame(),
                diagnostics=pd.DataFrame(),
            )

        close = ohlcv["close"].astype("float64")
        high = ohlcv["high"].astype("float64")
        low = ohlcv["low"].astype("float64")
        cash = cfg.initial_cash
        lots: list[dict[str, float | int | pd.Timestamp]] = []
        trades: list[dict[str, object]] = []
        equity_pts: list[float] = []
        weight_pts: list[float] = []
        open_lots_pts: list[int] = []
        filled_rungs_pts: list[int] = []
        cost = (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
        rungs = sorted(drawdown for drawdown in cfg.ladder_drawdowns if drawdown > 0)

        for i, ts in enumerate(close.index):
            px = float(close.iloc[i])
            hi = float(high.iloc[i])
            lo = float(low.iloc[i])

            remaining: list[dict[str, float | int | pd.Timestamp]] = []
            for lot in lots:
                entry = float(lot["entry"])
                qty = float(lot["qty"])
                entry_i = int(lot["entry_i"])
                tp = entry * (1.0 + cfg.take_profit_pct)
                should_take_profit = hi >= tp
                should_time_exit = i - entry_i >= cfg.max_hold_bars
                if should_take_profit or should_time_exit:
                    exit_px = tp if should_take_profit else px
                    proceeds = qty * exit_px
                    fee = proceeds * cost
                    cash += proceeds - fee
                    trades.append(
                        {
                            "ts": ts,
                            "side": "sell",
                            "qty": -qty,
                            "price": exit_px,
                            "fee": fee,
                            "leg": "spot",
                            "pnl": (exit_px - entry) * qty - fee,
                            "holding_bars": i - entry_i,
                            "note": "take_profit" if should_take_profit else "time_exit",
                        }
                    )
                else:
                    remaining.append(lot)
            lots = remaining

            filled_this_bar = 0
            if i > 0 and len(lots) < cfg.max_open_rungs:
                base = float(close.iloc[i - 1])
                equity_now = cash + sum(float(lot["qty"]) * px for lot in lots)
                for drawdown in rungs:
                    if len(lots) >= cfg.max_open_rungs:
                        break
                    fill_px = base * (1.0 - drawdown)
                    already_open = any(abs(float(lot["rung"]) - drawdown) < 1e-12 for lot in lots)
                    if already_open or lo > fill_px:
                        continue
                    notional = min(cash, equity_now * cfg.rung_risk_pct)
                    if notional <= 0:
                        continue
                    fee = notional * cost
                    qty = (notional - fee) / fill_px
                    cash -= notional
                    lots.append(
                        {
                            "entry": fill_px,
                            "qty": qty,
                            "rung": drawdown,
                            "entry_i": i,
                            "ts": ts,
                        }
                    )
                    trades.append(
                        {
                            "ts": ts,
                            "side": "buy",
                            "qty": qty,
                            "price": fill_px,
                            "fee": fee,
                            "leg": "spot",
                            "pnl": 0.0,
                            "holding_bars": 0,
                            "note": f"wick_rung_{drawdown:.2%}",
                        }
                    )
                    filled_this_bar += 1

            exposure = sum(float(lot["qty"]) * px for lot in lots)
            equity_now = cash + exposure
            equity_pts.append(equity_now)
            weight_pts.append(exposure / equity_now if equity_now > 0 else 0.0)
            open_lots_pts.append(len(lots))
            filled_rungs_pts.append(filled_this_bar)

        equity = pd.Series(equity_pts, index=close.index, name="equity")
        target_weight = pd.Series(weight_pts, index=close.index, name="target_weight")
        diagnostics = pd.DataFrame(
            {
                "open_lots": open_lots_pts,
                "filled_rungs": filled_rungs_pts,
            },
            index=close.index,
        )
        return StrategyResult(
            equity=equity,
            target_weight=target_weight,
            short_weight=pd.Series(0.0, index=close.index),
            trades=pd.DataFrame(trades),
            diagnostics=diagnostics,
        )


__all__ = ["WickCatcher", "WickCatcherConfig"]
