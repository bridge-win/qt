"""Replay historical extreme events to test signal coverage.

Each scenario is a single window of public OHLCV around a known crash;
the script downloads the OHLCV via ccxt, runs the composite score, and
reports whether the signal would have fired and at what relative offset
from the actual low.

Scenarios covered (BTC/USDT spot):
- 2020-03-12: COVID crash, BTC -50% in 24h
- 2021-05-19: China mining ban; ~$8.6B aggregated long liquidations
- 2022-06-12: Celsius freeze; BTC -25% in 4 days
- 2022-11-08 to 2022-11-13: FTX collapse; BTC -25%
- 2024-08-05: Yen carry-trade unwind; BTC -16% in 12h
- 2025-10-10 to 2025-10-11: largest single-day liquidation cascade ($19B crypto)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from rich.console import Console
from rich.table import Table

from qt.core.config import ThresholdConfig
from qt.data.market import fetch_ohlcv
from qt.indicators.composite import compute_extreme_score


@dataclass
class Scenario:
    name: str
    start: datetime
    end: datetime
    low_ts: datetime  # known cycle/event low


SCENARIOS = [
    Scenario("COVID crash 2020-03",
             datetime(2020, 3, 1, tzinfo=timezone.utc),
             datetime(2020, 3, 31, tzinfo=timezone.utc),
             datetime(2020, 3, 13, tzinfo=timezone.utc)),
    Scenario("May 2021 mining-ban cascade",
             datetime(2021, 5, 10, tzinfo=timezone.utc),
             datetime(2021, 6, 5, tzinfo=timezone.utc),
             datetime(2021, 5, 19, tzinfo=timezone.utc)),
    Scenario("Celsius freeze (Jun 2022)",
             datetime(2022, 6, 5, tzinfo=timezone.utc),
             datetime(2022, 6, 25, tzinfo=timezone.utc),
             datetime(2022, 6, 18, tzinfo=timezone.utc)),
    Scenario("FTX collapse (Nov 2022)",
             datetime(2022, 11, 1, tzinfo=timezone.utc),
             datetime(2022, 11, 30, tzinfo=timezone.utc),
             datetime(2022, 11, 21, tzinfo=timezone.utc)),
    Scenario("Yen carry unwind (Aug 2024)",
             datetime(2024, 8, 1, tzinfo=timezone.utc),
             datetime(2024, 8, 15, tzinfo=timezone.utc),
             datetime(2024, 8, 5, tzinfo=timezone.utc)),
    Scenario("Oct 2025 tariff cascade",
             datetime(2025, 10, 1, tzinfo=timezone.utc),
             datetime(2025, 10, 31, tzinfo=timezone.utc),
             datetime(2025, 10, 11, tzinfo=timezone.utc)),
]


def evaluate_scenario(s: Scenario, cfg: ThresholdConfig, timeframe: str = "1h") -> dict:
    ohlcv = fetch_ohlcv("binance", "BTC/USDT", timeframe=timeframe,
                        since=s.start, until=s.end)
    if ohlcv.empty:
        return {"scenario": s.name, "data": "missing"}
    score = compute_extreme_score(ohlcv, cfg=cfg)
    s_series = score.score.fillna(0.0)
    fired = s_series[s_series >= cfg.entry_score_min]
    if fired.empty:
        return {
            "scenario": s.name,
            "max_score": float(s_series.max()),
            "fired": False,
            "bars_to_low": None,
            "low_ts": s.low_ts,
        }
    first_fire = fired.index[0]
    delta = (first_fire - pd.Timestamp(s.low_ts, tz="UTC")).total_seconds() / 3600
    return {
        "scenario": s.name,
        "max_score": float(s_series.max()),
        "fired": True,
        "first_fire": first_fire,
        "low_ts": s.low_ts,
        "bars_to_low_h": delta,
        "fired_count": int(len(fired)),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--entry-score-min", type=float, default=0.35)
    p.add_argument("--min-groups", type=int, default=2)
    args = p.parse_args()

    cfg = ThresholdConfig(
        # The historical scenarios will not have on-chain/derivatives data in
        # this lightweight script (only OHLCV); loosen thresholds accordingly.
        entry_score_min=args.entry_score_min,
        min_factor_groups=args.min_groups,
        rsi_oversold=25, bb_std=2.0, drawdown_30d_min=0.10,
        wick_body_ratio_min=2.0, rv_ratio_min=1.3,
        vix_max=999, dxy_z_max=999,
    )
    console = Console()
    table = Table(title="QT stress-test (OHLCV-only)")
    for col in ["scenario", "max_score", "fired", "first_fire", "low_ts", "hours_offset"]:
        table.add_column(col)
    for sc in SCENARIOS:
        try:
            r = evaluate_scenario(sc, cfg, args.timeframe)
        except Exception as e:  # noqa: BLE001
            r = {"scenario": sc.name, "data": f"err: {e}"}
        table.add_row(
            r.get("scenario", "?"),
            f"{r.get('max_score', float('nan')):.2f}" if "max_score" in r else "-",
            str(r.get("fired", r.get("data", "?"))),
            str(r.get("first_fire", "-"))[:19],
            str(r.get("low_ts", "-"))[:19],
            f"{r.get('bars_to_low_h', float('nan')):.1f}" if "bars_to_low_h" in r and r.get("bars_to_low_h") is not None else "-",
        )
    console.print(table)


if __name__ == "__main__":
    main()
