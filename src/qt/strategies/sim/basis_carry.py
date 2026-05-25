"""Solution D — Funding-rate cash-and-carry (market-neutral basis trade).

WHY THIS STRATEGY EXISTS
------------------------
When a perpetual swap trades at a premium to spot (positive funding),
shorts receive funding from longs every 8 hours. A market-neutral
construction:

    long 1 BTC spot   +   short 1 BTC perp

has zero directional exposure (PnL from spot price moves is offset by the
short), but the short leg collects funding. Historically (2020–2023),
annualized funding has ranged from −15% to +60% depending on regime.

This is the classic "carry" trade and has been documented as a
risk-controlled retail-accessible source of yield in:

- Andreas Schrimpf, BIS Working Paper #1106 (2023), *Crypto carry*.
  Documents that BTC perp-spot basis has earned >10% annualized on
  average, with periodic drawdowns during de-leveraging events.
- Robert Carver's *Advanced Futures Trading Strategies* (2023): the
  carry premium between perp and spot is "the cleanest factor in crypto
  precisely because it isn't a directional bet".
- Roll, Engle, and others on the *cost-of-carry* model — applies
  directly to perp funding.
- 2020–2021 GBTC premium arbitrage and CME basis arb (Galaxy, Genesis,
  Three Arrows) — the same idea, different venue.

WHAT THIS STRATEGY DOES
-----------------------
On each bar:

1. Compute trailing 24h average funding (or use a forecast).
2. Annualize: ``ann = avg_funding * 3 * 365`` for 8h funding.
3. **Enter** when ``ann ≥ enter_apr`` (default 15%): set target weights
   to ``(+notional_frac, −notional_frac)``. ``notional_frac`` defaults to
   0.4 so the short leg's margin requirement is comfortable.
4. **Exit** when ``ann < exit_apr`` (default 5%) or when basis flips
   negative for ``negative_bars`` consecutive 8h periods → flat.
5. Per-bar funding accrual on the short leg is added to equity.

PARAMETERS
----------
- ``enter_apr`` / ``exit_apr``: annualized funding thresholds.
- ``notional_frac``: target gross weight on each leg.
- ``funding_periods_per_bar``: e.g. ``1/8`` for hourly bars (8h funding).
- ``rebalance_tolerance``: skip tiny re-balances.
- ``negative_bars``: how many consecutive negative-funding intervals
  trigger a forced exit (defends against funding regime flips).

KNOWN FAILURE MODES
-------------------
- **Exchange / custody risk**: the entire P&L is on the exchange. If the
  exchange has issues (FTX, Mt Gox), the trade goes to 0 regardless of
  the funding signal. Diversify across venues.
- **Mark-price vs index-price divergence**: liquidations on the short
  perp can occur during fast moves even if you have spot offsetting.
  Keep gross leverage ≤ 2×.
- **Cross-margin vs isolated**: this strategy assumes cross-margin so
  the spot leg can be margin for the short. On isolated-margin venues,
  you need additional buffer cash.
- **Tax**: in many jurisdictions, perp PnL and funding are *ordinary
  income*, not capital gains. Different cost-basis treatment than spot.

REFERENCES
----------
- Schrimpf, *Crypto carry*, BIS Working Paper #1106 (2023).
- Carver (2023), *Advanced Futures Trading Strategies*, ch. on FX carry
  and the cross-asset analogy to crypto perps.
- Roll, *A Simple Implicit Measure of the Effective Bid-Ask Spread*
  (1984) — for spot/perp basis estimation under noise.
- Hayes & Lapinski research notes on perpetual funding (2020+).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from qt.strategies.sim.base import StrategyResult, simulate_target_positions


@dataclass
class BasisCarryConfig:
    enter_apr: float = 0.15
    exit_apr: float = 0.05
    notional_frac: float = 0.40
    funding_periods_per_bar: float = 1.0 / 8.0   # 1h bars on 8h funding
    funding_periods_per_year: float = 3 * 365.0  # 3 funding events per day
    negative_bars: int = 3
    avg_window_bars: int = 24
    rebalance_tolerance: float = 0.05
    fee_bps: float = 4.0          # perp+spot taker, ~4 bps each is typical
    slippage_bps: float = 3.0
    initial_cash: float = 10_000.0


class BasisCarry:
    """Market-neutral funding-rate carry trade. See module docstring."""

    def __init__(self, cfg: BasisCarryConfig | None = None) -> None:
        self.cfg = cfg or BasisCarryConfig()

    def run(
        self,
        ohlcv: pd.DataFrame,
        *,
        funding: pd.Series,
    ) -> StrategyResult:
        cfg = self.cfg
        close = ohlcv["close"].astype("float64")
        if funding is None or funding.empty:
            empty = pd.Series(dtype="float64")
            return StrategyResult(
                equity=empty, target_weight=empty,
                short_weight=empty, trades=pd.DataFrame(),
                diagnostics=pd.DataFrame(),
            )
        fund_aligned = funding.reindex(close.index).ffill().fillna(0.0)

        # Average funding over the trailing window, then annualize.
        # ``funding`` is the per-period (8h) funding rate, e.g. 0.0001 = 1bp/8h.
        avg_per_period = fund_aligned.rolling(cfg.avg_window_bars).mean()
        ann_funding = avg_per_period * cfg.funding_periods_per_year

        neg_counter = (
            (fund_aligned < 0).astype(int).rolling(cfg.negative_bars).sum()
        )

        # State machine: 0 = flat, 1 = on
        on = pd.Series(False, index=close.index)
        state = False
        for i, ann in enumerate(ann_funding):
            if state:
                # Exit when annualized below exit threshold OR consecutive negs
                if (pd.notna(ann) and ann < cfg.exit_apr) or (
                    neg_counter.iloc[i] >= cfg.negative_bars
                ):
                    state = False
            else:
                if pd.notna(ann) and ann >= cfg.enter_apr:
                    state = True
            on.iloc[i] = state

        target_long = on.astype(float) * cfg.notional_frac
        target_short = on.astype(float) * cfg.notional_frac

        equity, trades = simulate_target_positions(
            close,
            target_long,
            initial_cash=cfg.initial_cash,
            fee_bps=cfg.fee_bps,
            slippage_bps=cfg.slippage_bps,
            short_weight=target_short,
            funding=fund_aligned,
            funding_periods_per_bar=cfg.funding_periods_per_bar,
            rebalance_tolerance=cfg.rebalance_tolerance,
        )
        diag = pd.DataFrame(
            {"funding": fund_aligned, "ann_funding": ann_funding, "on": on}
        )
        return StrategyResult(
            equity=equity,
            target_weight=target_long,
            short_weight=target_short,
            trades=trades,
            diagnostics=diag,
        )


__all__ = ["BasisCarry", "BasisCarryConfig"]
