from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest

from qt.data import market


class _FakeExchange:
    has: ClassVar[dict[str, bool]] = {"fetchOHLCV": True}

    def __init__(self, config: dict[str, bool]) -> None:
        self.config = config

    def fetch_ohlcv(
        self,
        symbol: str,
        *,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        assert symbol == "BTC/USDT"
        assert timeframe == "1h"
        assert limit == 1000
        assert since == int(pd.Timestamp("2024-01-01T00:00:00Z").timestamp() * 1000)
        return [
            [int(pd.Timestamp("2024-01-01T01:00:00Z").timestamp() * 1000), 1, 2, 1, 2, 10],
            [int(pd.Timestamp("2024-01-01T02:00:00Z").timestamp() * 1000), 2, 3, 2, 3, 11],
        ]


def test_fetch_ohlcv_accepts_timezone_aware_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market, "ccxt", SimpleNamespace(okx=_FakeExchange))

    df = market.fetch_ohlcv(
        "okx",
        "BTC/USDT",
        "1h",
        since=datetime(2024, 1, 1, tzinfo=timezone.utc),
        until=datetime(2024, 1, 1, 3, tzinfo=timezone.utc),
    )

    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None
    assert df.index[-1] == pd.Timestamp("2024-01-01T02:00:00Z")
