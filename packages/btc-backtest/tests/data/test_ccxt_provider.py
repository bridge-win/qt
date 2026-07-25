from __future__ import annotations

import importlib
from collections.abc import Sequence
from datetime import datetime, timezone

import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest
from btc_backtest.data.providers.ccxt import CCXTProvider
from btc_backtest.errors import DataCoverageError, DataValidationError, ProviderError

UTC = timezone.utc
START_MS = 1_704_067_200_000
HOUR_MS = 3_600_000


def hourly_request(hours: int = 3, *, provider: str = "ccxt:fixture") -> DataRequest:
    return DataRequest(
        provider=provider,
        market="spot",
        symbol="BTC/USDT",
        timeframe="1h",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 1, hours, tzinfo=UTC),
    )


def ohlcv_page(offset: int, count: int) -> list[list[float | int]]:
    return [
        [
            START_MS + HOUR_MS * index,
            100.0 + index,
            102.0 + index,
            99.0 + index,
            101.0 + index,
            10.0 + index,
        ]
        for index in range(offset, offset + count)
    ]


class FixtureExchange:
    def __init__(self, pages: Sequence[list[list[float | int]]]) -> None:
        self.has = {"fetchOHLCV": True}
        self.timeframes = {"1h": "1h", "1d": "1d"}
        self.pages = list(pages)
        self.cursors: list[int] = []

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float | int]]:
        assert symbol == "BTC/USDT"
        assert timeframe == "1h"
        assert limit == 1_000
        self.cursors.append(since)
        if not self.pages:
            return []
        return self.pages.pop(0)


class NoOHLCVExchange:
    def __init__(self) -> None:
        self.has = {"fetchOHLCV": False}
        self.timeframes: dict[str, str] = {}

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float | int]]:
        raise AssertionError("fetch_ohlcv must not be called")


def test_ccxt_provider_paginates_without_cursor_replay() -> None:
    exchange = FixtureExchange(
        pages=[ohlcv_page(0, 2), ohlcv_page(2, 1), []]
    )

    result = CCXTProvider("fixture", exchange=exchange).fetch(
        hourly_request(3)
    )

    assert len(result.frame) == 3
    assert exchange.cursors == sorted(set(exchange.cursors))
    assert result.manifest.source == "ccxt://fixture"
    assert "exchange-dependent" in (result.manifest.license_note or "")


def test_ccxt_provider_rejects_exchange_without_ohlcv() -> None:
    with pytest.raises(ProviderError, match="fetchOHLCV"):
        CCXTProvider("fixture", exchange=NoOHLCVExchange())


def test_ccxt_provider_rejects_unsupported_timeframe() -> None:
    exchange = FixtureExchange(pages=[])
    exchange.timeframes = {"1d": "1d"}
    provider = CCXTProvider("fixture", exchange=exchange)

    with pytest.raises(ProviderError, match="timeframe"):
        provider.fetch(hourly_request())


def test_ccxt_provider_rejects_cursor_replay() -> None:
    exchange = FixtureExchange(
        pages=[ohlcv_page(0, 2), ohlcv_page(0, 2)]
    )

    with pytest.raises(ProviderError, match="cursor"):
        CCXTProvider("fixture", exchange=exchange).fetch(hourly_request())


def test_ccxt_provider_rejects_duplicate_timestamp() -> None:
    duplicate = ohlcv_page(0, 1) * 2

    with pytest.raises(DataValidationError, match="duplicate"):
        CCXTProvider(
            "fixture",
            exchange=FixtureExchange([duplicate]),
        ).fetch(hourly_request(1))


def test_ccxt_provider_rejects_non_finite_timestamp() -> None:
    page = ohlcv_page(0, 1)
    page[0][0] = float("inf")

    with pytest.raises(DataValidationError, match="timestamp"):
        CCXTProvider(
            "fixture",
            exchange=FixtureExchange([page]),
        ).fetch(hourly_request(1))


def test_ccxt_provider_filters_bars_at_closed_open_end() -> None:
    result = CCXTProvider(
        "fixture",
        exchange=FixtureExchange([ohlcv_page(0, 4)]),
    ).fetch(hourly_request(3))

    assert result.frame.index.tolist() == list(
        pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    )


def test_ccxt_provider_rejects_retention_gap() -> None:
    exchange = FixtureExchange([ohlcv_page(1, 2)])

    with pytest.raises(DataCoverageError, match="missing"):
        CCXTProvider("fixture", exchange=exchange).fetch(hourly_request(3))


def test_ccxt_provider_requires_matching_provider_identity() -> None:
    provider = CCXTProvider("fixture", exchange=FixtureExchange([]))

    with pytest.raises(ProviderError, match="provider"):
        provider.fetch(hourly_request(provider="ccxt:other"))


def test_ccxt_optional_import_has_actionable_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = importlib.import_module

    def fail_ccxt(name: str, package: str | None = None) -> object:
        if name == "ccxt":
            raise ModuleNotFoundError("ccxt")
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fail_ccxt)

    with pytest.raises(ProviderError, match=r"btc-backtest\[exchanges\]"):
        CCXTProvider("fixture")
