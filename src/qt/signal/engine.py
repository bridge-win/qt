"""Signal engine: turn composite extreme score + multi-factor confirmation
into discrete `Signal` objects with explicit reasons."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from qt.core.config import ThresholdConfig
from qt.core.types import Signal, SignalKind
from qt.indicators.composite import ExtremeScore, compute_extreme_score


@dataclass
class SignalEngine:
    """Stateful wrapper around `compute_extreme_score` that emits Signals.

    The engine is deliberately stateless across bars (besides cooldown
    tracking, which lives in the risk engine). Each `step` call returns a
    Signal computed only from data up to and including the current bar.
    """

    thresholds: ThresholdConfig

    def evaluate(
        self,
        *,
        ohlcv: pd.DataFrame,
        funding: pd.Series | None = None,
        oi: pd.Series | None = None,
        long_short_ratio: pd.Series | None = None,
        sopr: pd.Series | None = None,
        mvrv_z: pd.Series | None = None,
        nupl: pd.Series | None = None,
        puell: pd.Series | None = None,
        reserve_risk: pd.Series | None = None,
        exchange_netflow: pd.Series | None = None,
        fear_greed: pd.Series | None = None,
        social_sentiment: pd.Series | None = None,
        vix: pd.Series | None = None,
        dxy: pd.Series | None = None,
    ) -> ExtremeScore:
        return compute_extreme_score(
            ohlcv=ohlcv,
            funding=funding,
            oi=oi,
            long_short_ratio=long_short_ratio,
            sopr=sopr,
            mvrv_z=mvrv_z,
            nupl=nupl,
            puell=puell,
            reserve_risk=reserve_risk,
            exchange_netflow=exchange_netflow,
            fear_greed=fear_greed,
            social_sentiment=social_sentiment,
            vix=vix,
            dxy=dxy,
            cfg=self.thresholds,
        )

    def to_signals(self, score: ExtremeScore) -> list[Signal]:
        """Convert a precomputed `ExtremeScore` into a list of `Signal` objects.

        Only bars at or above `thresholds.entry_score_min` *and* with at
        least `thresholds.min_factor_groups` groups firing produce an
        ENTRY_LONG signal. All other bars produce no signal (we keep the
        list sparse to make audit logs readable).
        """

        s = score.score
        groups_fired = score.group_flags.sum(axis=1)
        mask = (s >= self.thresholds.entry_score_min) & (
            groups_fired >= self.thresholds.min_factor_groups
        ) & score.macro_ok
        out: list[Signal] = []
        for ts in s.index[mask.fillna(False)]:
            ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            factors = {
                col: float(score.factor_flags.loc[ts, col])
                for col in score.factor_flags.columns
                if bool(score.factor_flags.loc[ts, col])
            }
            reasons = tuple(score.reasons.get(ts, []))
            out.append(
                Signal(
                    ts=ts_py,
                    kind=SignalKind.ENTRY_LONG,
                    score=float(s.loc[ts]),
                    reasons=reasons,
                    factors=factors,
                    target_quote_alloc=_score_to_alloc(float(s.loc[ts])),
                )
            )
        return out


def _score_to_alloc(score: float) -> float:
    """Map a composite score in [entry_min, 1] to a target allocation in [0.1, 1].

    This is *advisory only* — the risk engine still caps actual position
    size via `max_position_pct` and the volatility-target.
    """

    # Linear ramp: score 0.65 -> 0.4 alloc, score 1.0 -> 1.0 alloc
    return max(0.1, min(1.0, (score - 0.5) * 2.0))


def generate_signals(
    thresholds: ThresholdConfig,
    ohlcv: pd.DataFrame,
    *,
    funding: pd.Series | None = None,
    oi: pd.Series | None = None,
    long_short_ratio: pd.Series | None = None,
    sopr: pd.Series | None = None,
    mvrv_z: pd.Series | None = None,
    nupl: pd.Series | None = None,
    puell: pd.Series | None = None,
    reserve_risk: pd.Series | None = None,
    exchange_netflow: pd.Series | None = None,
    fear_greed: pd.Series | None = None,
    social_sentiment: pd.Series | None = None,
    vix: pd.Series | None = None,
    dxy: pd.Series | None = None,
) -> list[Signal]:
    """One-shot helper for non-streaming research / backtest use."""

    eng = SignalEngine(thresholds=thresholds)
    score = eng.evaluate(
        ohlcv=ohlcv,
        funding=funding,
        oi=oi,
        long_short_ratio=long_short_ratio,
        sopr=sopr,
        mvrv_z=mvrv_z,
        nupl=nupl,
        puell=puell,
        reserve_risk=reserve_risk,
        exchange_netflow=exchange_netflow,
        fear_greed=fear_greed,
        social_sentiment=social_sentiment,
        vix=vix,
        dxy=dxy,
    )
    return eng.to_signals(score)
