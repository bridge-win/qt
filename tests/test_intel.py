"""Intel discovery scanners, ranking, and persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from qt.intel.ranker import persist_opportunities, rank_opportunities, read_opportunities
from qt.intel.scanners import (
    DepegScanner,
    ExchangeClient,
    FundingScanner,
    Opportunity,
    ScannerSettings,
    SpreadScanner,
    WickScanner,
)


class _FakeExchange:
    def __init__(self, exchange_id: str) -> None:
        self.id = exchange_id
        self.has: Mapping[str, object] = {
            "fetchFundingRates": True,
            "fetchTickers": True,
            "fetchTicker": True,
        }

    def fetch_funding_rates(
        self,
        symbols: Sequence[str] | None = None,
        params: Mapping[str, object] | None = None,
    ) -> list[dict[str, object]]:
        rates = {
            "binance": 0.00030,
            "okx": -0.00010,
            "bybit": 0.00005,
        }
        return [
            {
                "symbol": symbol,
                "fundingRate": rates[self.id],
                "timestamp": 1_704_067_200_000,
            }
            for symbol in (symbols or ["BTC/USDT"])
        ]

    def fetch_tickers(
        self,
        symbols: Sequence[str] | None = None,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, dict[str, object]]:
        data: dict[str, dict[str, dict[str, object]]] = {
            "binance": {
                "BTC/USDT": {"bid": 99.8, "ask": 100.0, "last": 99.9, "bidVolume": 3.0},
            },
            "okx": {
                "BTC/USDT": {"bid": 101.0, "ask": 101.2, "last": 101.1, "bidVolume": 2.0},
            },
            "bybit": {
                "BTC/USDT": {"bid": 100.1, "ask": 100.4, "last": 100.2, "bidVolume": 1.0},
            },
        }
        tickers = data[self.id]
        if symbols is None:
            return tickers
        return {symbol: tickers[symbol] for symbol in symbols if symbol in tickers}

    def fetch_ticker(
        self,
        symbol: str,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if symbol == "USDC/USDT":
            return {"symbol": symbol, "bid": 0.9958, "ask": 0.9962, "last": 0.996}
        if symbol == "BTC/USDT:USDT-240927":
            return {
                "symbol": symbol,
                "bid": 31_200.0,
                "ask": 31_260.0,
                "last": 31_230.0,
                "expiry": 1_727_395_200_000,
            }
        return {"symbol": symbol, "bid": 30_000.0, "ask": 30_030.0, "last": 30_015.0}


def _factory(exchange_id: str) -> ExchangeClient:
    return _FakeExchange(exchange_id)


def test_funding_scanner_flags_absolute_and_cross_venue_edges() -> None:
    settings = ScannerSettings(
        venues=["binance", "okx", "bybit"],
        symbols=["BTC/USDT"],
        funding_apr_threshold=0.10,
        funding_diff_apr_threshold=0.15,
    )
    opps = FundingScanner(_factory).scan(settings)

    kinds = {opp.kind for opp in opps}
    assert "funding" in kinds
    assert "funding_diff" in kinds
    assert max(opp.edge_bps for opp in opps) > 1_000
    assert any("short binance" in opp.action for opp in opps)


def test_spread_and_depeg_scanners_return_net_of_fee_edges() -> None:
    settings = ScannerSettings(
        venues=["binance", "okx"],
        symbols=["BTC/USDT"],
        stable_symbols=["USDC/USDT"],
        taker_fee_bps=5.0,
        spread_buffer_bps=2.0,
        depeg_threshold_bps=30.0,
    )

    spread = SpreadScanner(_factory).scan(settings)
    depeg = DepegScanner(_factory).scan(settings)

    assert len(spread) == 1
    assert spread[0].edge_bps > 80.0
    assert spread[0].details["buy_venue"] == "binance"
    assert len(depeg) == 2
    assert {opp.action for opp in depeg} == {"buy stable below peg"}


def test_ranker_persists_top_opportunities(tmp_path: Path) -> None:
    opps = [
        Opportunity(
            kind="funding",
            venue="binance",
            symbol="BTC/USDT",
            edge_bps=120.0,
            confidence=0.8,
            capacity_usd=10_000.0,
            action="short perp + long spot",
            why="funding APR is above threshold",
        ),
        Opportunity(
            kind="spread",
            venue="binance->okx",
            symbol="BTC/USDT",
            edge_bps=40.0,
            confidence=0.9,
            capacity_usd=1_000.0,
            action="buy low venue, sell high venue",
            why="net cross-venue spread is positive",
        ),
    ]

    ranked = rank_opportunities(opps, top_n=1)
    assert [opp.kind for opp in ranked] == ["funding"]

    path = persist_opportunities(tmp_path, ranked)
    raw = json.loads(path.read_text())
    assert raw["count"] == 1
    assert raw["opportunities"][0]["score"] > 0
    loaded = read_opportunities(tmp_path)
    loaded_opportunities = loaded["opportunities"]
    assert isinstance(loaded_opportunities, list)
    loaded_top = loaded_opportunities[0]
    assert isinstance(loaded_top, dict)
    assert loaded_top["kind"] == "funding"


def test_wick_scanner_flags_recent_flash_wick() -> None:
    idx = pd.date_range("2024-01-01", periods=80, freq="1h", tz="UTC")
    close = pd.Series(np.full(len(idx), 100.0), index=idx)
    ohlcv = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.ones(len(idx)),
        }
    )
    ohlcv.iloc[-1, ohlcv.columns.get_loc("low")] = 88.0
    ohlcv.iloc[-1, ohlcv.columns.get_loc("close")] = 96.0
    ohlcv.iloc[-1, ohlcv.columns.get_loc("open")] = 100.0

    scanner = WickScanner(ohlcv_provider=lambda _: ohlcv)
    opps = scanner.scan(ScannerSettings(wick_recent_bars=3))

    assert len(opps) == 1
    assert opps[0].kind == "wick"
    assert opps[0].ts <= datetime.now(tz=timezone.utc)
    assert "wick regime" in opps[0].why
