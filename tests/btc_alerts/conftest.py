from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest

from qt.btc_alerts.models import TIMEFRAMES, Bar, Candidate, Policy, Quote
from qt.btc_alerts.signals import evaluate
from qt.btc_alerts.store import Store

NOW = 1_800_000_000


def packet(*, down: bool = False, volume: float = 150) -> tuple[dict[str, list[Bar]], Quote]:
    bars = {}
    sign = -1 if down else 1
    for timeframe, seconds in TIMEFRAMES.items():
        rows = []
        for index in range(300):
            price = 60000 + sign * (index - 299) * 2
            width = 800 if timeframe == "1h" else 20
            rows.append(Bar(
                open_time=NOW - (300 - index) * seconds, open=price - sign,
                high=price + width, low=price - width, close=price, volume=100,
            ))
        bars[timeframe] = rows
    price = 59950 if down else 60050
    bars["15m"][-1] = Bar(
        open_time=NOW - 900, open=60000, high=max(60010, price + 5),
        low=min(59990, price - 5), close=price, volume=volume,
    )
    quote = Quote(
        venue="okx" if down else "binance", product="perpetual" if down else "spot",
        symbol="BTC-USDT-SWAP" if down else "BTCUSDT", bid=price - 1, ask=price + 1,
        observed_at=float(NOW), source_at=float(NOW),
        mark_price=price if down else None, funding_cost_bps=10 if down else None,
        funding_as_of=NOW - 3600 if down else None, funding_coverage_start=NOW - 30 * 86400 if down else None,
    )
    return bars, quote


@pytest.fixture
def policy() -> Policy:
    return Policy()


@pytest.fixture
def candidate(policy: Policy) -> Candidate:
    bars, quote = packet()
    return evaluate(bars, quote, policy, NOW)[0]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "alerts")


class FakeMarket:
    def __init__(self, *, ready: bool = True):
        self.ready = ready
        self.current = packet()

    def packet(self, product: Literal["spot", "perpetual"], now: float, policy: Policy) -> tuple[dict[str, list[Bar]], Quote]:
        if product == "perpetual":
            raise ValueError("perpetual_context_unavailable")
        return self.current

    def quote(self, product: Literal["spot", "perpetual"], now: float, policy: Policy) -> Quote:
        return self.current[1].model_copy(update={"observed_at": now, "source_at": now})

    def stream_ready(self, now: float) -> bool:
        return self.ready


def approve(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": {"reviews": [{
            "candidate_id": candidate["candidate_id"], "verdict": "approve", "reason_codes": ["consistent"],
            "supporting_evidence_ids": candidate["evidence_ids"], "opposing_evidence_ids": [],
            "missing_evidence": [], "summary": "The supplied frozen evidence supports this research candidate.",
        } for candidate in payload["candidates"]]},
        "model": "fixture-only", "model_pinned": True,
    }
