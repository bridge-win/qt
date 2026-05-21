"""Position sizing primitives.

We default to *volatility-targeted* sizing scaled by *fractional* Kelly.
Pure Kelly is famously over-aggressive on noisy edge estimates; fractional
Kelly (typically 0.25–0.5) is the practitioner standard. See:

- Thorp (2006), "The Kelly Criterion in Blackjack, Sports Betting, and the
  Stock Market".
- MacLean, Thorp, Ziemba (2010), *The Kelly Capital Growth Investment
  Criterion*, World Scientific.
"""

from __future__ import annotations

import math


def fractional_kelly(
    edge: float,
    win_rate: float,
    win_loss_ratio: float,
    fraction: float = 0.25,
) -> float:
    """Fractional Kelly bet size in [0, 1] of equity.

    Args:
        edge: prior expectation in [0, 1]. If you don't have one, pass 1.0.
        win_rate: empirical hit rate of the signal in [0, 1].
        win_loss_ratio: average winner / average loser, both positive.
        fraction: Kelly fraction (0.25 = quarter-Kelly).
    """

    if win_loss_ratio <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    p = win_rate
    b = win_loss_ratio
    raw = (p * (b + 1) - 1) / b
    return max(0.0, min(1.0, fraction * edge * raw))


def vol_target_size(
    realized_vol: float,
    target_vol: float = 0.40,
    max_leverage: float = 1.0,
) -> float:
    """Inverse-vol scalar in [0, max_leverage].

    When current realized vol is double the target, allocation is halved.
    """

    if realized_vol <= 0 or not math.isfinite(realized_vol):
        return 0.0
    return max(0.0, min(max_leverage, target_vol / realized_vol))


def combine_sizers(
    score_alloc: float,
    kelly_alloc: float,
    vol_alloc: float,
    hard_cap: float,
) -> float:
    """Take the *minimum* of three independent sizers, then hard-cap.

    The min-of-sizers convention is conservative: any sizer that says "be
    smaller" wins. This is how systematic CTAs typically combine signal
    confidence, edge-Kelly, and vol-targeting.
    """

    return min(score_alloc, kelly_alloc, vol_alloc, hard_cap)
