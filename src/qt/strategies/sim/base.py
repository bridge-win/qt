"""Shared types and a tiny target-position simulator used by all four
solution-gallery strategies.

The main ``Backtester`` in ``qt.backtest.engine`` is tied to the extreme-event
``SignalEngine``; the solution-gallery strategies emit *target positions*
(fraction of equity 0..1 long, possibly with a market-neutral short leg) on
each bar, which is easier to reason about for DCA / trend / carry style
systems. ``simulate_target_positions`` turns that target series into
fills + equity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class StrategyResult:
    """Common return type for all gallery strategies.

    Attributes:
        equity: equity curve (in quote currency, e.g. USDT), one point per bar.
        target_weight: target long weight ∈ [0, 1] per bar (1 = fully long
            spot). For market-neutral strategies (e.g. basis carry) this is
            the *spot* leg; ``short_weight`` mirrors it.
        short_weight: target short perp weight (≥ 0). Only used by carry-like
            strategies; otherwise zero.
        trades: rows = filled trades, columns = entry/exit ts, prices,
            qty, reason, pnl.
        diagnostics: free-form per-bar diagnostics (signal score, multiplier,
            funding payment, …) — useful for plotting and tuning.
    """

    equity: pd.Series
    target_weight: pd.Series
    short_weight: pd.Series
    trades: pd.DataFrame
    diagnostics: pd.DataFrame = field(default_factory=pd.DataFrame)


def simulate_target_positions(
    close: pd.Series,
    target_weight: pd.Series,
    *,
    initial_cash: float = 10_000.0,
    fee_bps: float = 7.5,
    slippage_bps: float = 5.0,
    short_weight: pd.Series | None = None,
    funding: pd.Series | None = None,
    funding_periods_per_bar: float = 0.0,
    rebalance_tolerance: float = 0.05,
) -> tuple[pd.Series, pd.DataFrame]:
    """Translate a per-bar target-weight series into equity + trades.

    Parameters
    ----------
    close:
        Close price series (quote/base) indexed by timestamp.
    target_weight:
        Desired *long* allocation as a fraction of equity. ``0`` = flat,
        ``1`` = fully long. Values > 1 allowed but not encouraged.
    initial_cash:
        Starting equity.
    fee_bps / slippage_bps:
        Per-side trading costs in basis points. Defaults (7.5 + 5 = 12.5 bps)
        match retail Binance spot taker + ~5 bps realistic slippage on
        weekly-or-rarer trades.
    short_weight:
        Optional short-perp weight per bar; used by the basis-carry strategy.
        Funding payments on the short leg are added to equity each bar
        (positive funding when shorting perp = income).
    funding:
        Per-bar funding rate (already aligned to the bar). For 8h funding
        on 1h bars, set ``funding_periods_per_bar = 1/8``.
    funding_periods_per_bar:
        Fraction of a funding interval represented by each bar. Used to
        scale ``funding`` into a per-bar payment.
    rebalance_tolerance:
        Skip rebalancing if |target - current| < this fraction of equity;
        reduces churn from tiny target changes.
    """

    if close.empty:
        return pd.Series(dtype="float64"), pd.DataFrame()

    target_weight = target_weight.reindex(close.index).fillna(0.0).clip(lower=0.0)
    if short_weight is None:
        short_weight = pd.Series(0.0, index=close.index)
    else:
        short_weight = short_weight.reindex(close.index).fillna(0.0).clip(lower=0.0)
    if funding is None:
        funding = pd.Series(0.0, index=close.index)
    else:
        funding = funding.reindex(close.index).fillna(0.0)

    cash = float(initial_cash)
    qty_long = 0.0          # BTC held on spot
    qty_short = 0.0         # BTC notional shorted on perp
    short_avg = 0.0         # weighted average entry price for the short leg
    cost = (fee_bps + slippage_bps) / 10_000.0
    equity_pts: list[float] = []
    trade_rows: list[dict] = []

    def _equity(px: float) -> float:
        long_val = qty_long * px
        short_pnl = (short_avg - px) * qty_short if qty_short > 0 else 0.0
        return cash + long_val + short_pnl

    for ts, px in close.items():
        px_f = float(px)

        # --- 1. Funding accrual on the short leg (we collect when funding > 0)
        if qty_short > 0 and funding_periods_per_bar > 0:
            short_notional = qty_short * px_f
            cash += short_notional * float(funding.loc[ts]) * funding_periods_per_bar

        equity = _equity(px_f)
        if equity <= 0:
            equity_pts.append(equity)
            continue

        # --- 2. Spot-leg rebalance
        tgt_long = float(target_weight.loc[ts])
        cur_w = (qty_long * px_f) / equity
        if abs(tgt_long - cur_w) >= rebalance_tolerance or (
            tgt_long == 0.0 and qty_long > 0
        ):
            target_long_qty = (tgt_long * equity) / px_f
            delta = target_long_qty - qty_long
            fee_paid = abs(delta) * px_f * cost
            cash -= delta * px_f + fee_paid
            qty_long = target_long_qty
            trade_rows.append(
                {
                    "ts": ts, "side": "buy" if delta > 0 else "sell",
                    "qty": delta, "price": px_f, "fee": fee_paid, "leg": "spot",
                }
            )

        # --- 3. Short-perp rebalance
        tgt_short = float(short_weight.loc[ts])
        cur_sw = (qty_short * px_f) / max(_equity(px_f), 1e-9)
        if abs(tgt_short - cur_sw) >= rebalance_tolerance or (
            tgt_short == 0.0 and qty_short > 0
        ):
            target_short_qty = (tgt_short * _equity(px_f)) / px_f
            delta_s = target_short_qty - qty_short
            fee_paid_s = abs(delta_s) * px_f * cost
            cash -= fee_paid_s
            if delta_s > 0:
                # Adding to short: weight new contracts in to avg entry.
                short_avg = (
                    (short_avg * qty_short + px_f * delta_s) / target_short_qty
                    if target_short_qty > 0 else 0.0
                )
            elif qty_short > 0:
                # Reducing short: realize PnL on the closed portion.
                closed = -delta_s
                realized = (short_avg - px_f) * closed
                cash += realized
            qty_short = target_short_qty
            if qty_short == 0:
                short_avg = 0.0
            trade_rows.append(
                {
                    "ts": ts,
                    "side": "short_open" if delta_s > 0 else "short_close",
                    "qty": delta_s, "price": px_f, "fee": fee_paid_s, "leg": "perp",
                }
            )

        equity_pts.append(_equity(px_f))

    eq = pd.Series(equity_pts, index=close.index, name="equity")
    trades_df = pd.DataFrame(trade_rows)
    if not trades_df.empty:
        trades_df["ts"] = pd.to_datetime(trades_df["ts"], utc=True)
    return eq, trades_df


def vol_target_weight(
    close: pd.Series, *, target_vol_annual: float = 0.15,
    window: int = 30, max_weight: float = 1.0,
    bars_per_year: int = 365 * 24,
) -> pd.Series:
    """Inverse-volatility weight: w = target_vol / realized_vol_annual.

    Clipped to ``[0, max_weight]``. Standard practice from Carver
    (*Systematic Trading*) — keeps risk constant across regimes.
    """

    ret = close.pct_change()
    rv = ret.rolling(window).std(ddof=0) * np.sqrt(bars_per_year)
    w = (target_vol_annual / rv).clip(lower=0.0, upper=max_weight)
    return w.fillna(0.0)
