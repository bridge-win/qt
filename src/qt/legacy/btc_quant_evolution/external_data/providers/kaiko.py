# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from ..http import read_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderHttpError,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_BASE_URL = "https://us.market-api.kaiko.io"
_HOST = "us.market-api.kaiko.io"
_TIMEOUT_SECONDS = 15
_MAX_PAGES = 1_000
_INTERVAL = "4h"
_AVAILABILITY_DELAY = timedelta(hours=4)
_DATASETS = {
    "market_ohlcv": "/v2/data/trades.v1/exchanges/binc/spot/btc-usdt/aggregations/count_ohlcv_vwap",
    "market_depth": "/v2/data/order_book_snapshots.v1/exchanges/binc/spot/btc-usdt/ob_aggregations/depth",
}
_REQUIRED_FIELDS = {
    "market_ohlcv": ("trade_count", "vwap"),
    "market_depth": ("bid_depth", "ask_depth"),
}
_CANONICAL_SYMBOL = "BTCUSDT"
_SUPPORTED_SYMBOLS = frozenset({_CANONICAL_SYMBOL, "BTC/USDT", "BTC-USDT"})


class KaikoProvider:
    name = "kaiko"
    required_credentials = ("KAIKO_API_KEY",)

    def capabilities(self) -> Sequence[ProviderCapability]:
        return tuple(
            ProviderCapability(
                provider=self.name,
                dataset=dataset,
                free=False,
                required_credentials=self.required_credentials,
                min_timeframe=_INTERVAL,
                domain="liquidity",
                tier="paid",
                implemented=True,
                required_fields=_REQUIRED_FIELDS[dataset],
            )
            for dataset in _DATASETS
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled" if env.get("KAIKO_API_KEY", "").strip() else "missing_credentials"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        if request.symbol not in _SUPPORTED_SYMBOLS:
            return []
        return [
            ProviderPlanItem(
                provider=self.name,
                dataset=dataset,
                symbol=_CANONICAL_SYMBOL,
                start=request.start_as_datetime(),
                end=request.end_as_datetime(),
                params={
                    "endpoint": endpoint,
                    "interval": _INTERVAL,
                    **(
                        {"page_size": "100", "sort": "asc"}
                        if dataset == "market_depth"
                        else {}
                    ),
                },
            )
            for dataset, endpoint in _DATASETS.items()
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        api_key = os.environ.get("KAIKO_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Kaiko API key is not configured")

        url = _initial_url(item)
        headers = {"X-Api-Key": api_key}
        records: list[dict[str, object]] = []
        visited: set[str] = set()
        for _ in range(_MAX_PAGES):
            if url in visited:
                raise RuntimeError("Kaiko pagination cycle detected")
            visited.add(url)
            try:
                payload = _read_json(url, headers)
            except ProviderHttpError:
                raise
            except Exception:
                raise RuntimeError("Kaiko request failed") from None
            page, next_url = _page(payload)
            records.extend(page)
            if next_url is None:
                return records
            url = _next_url(next_url)
        raise RuntimeError("Kaiko pagination exceeded the bounded page limit")

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        _endpoint(item)
        normalized: list[NormalizedRow] = []
        for record in records:
            timestamp = _record_timestamp(item.dataset, record)
            if not item.start <= timestamp < item.end:
                continue
            normalized.append(
                NormalizedRow(
                    timestamp=timestamp,
                    available_at=timestamp + _AVAILABILITY_DELAY,
                    source=self.name,
                    dataset=item.dataset,
                    symbol=item.symbol,
                    values=_values(item.dataset, record),
                )
            )
        return sorted(normalized, key=lambda row: row.timestamp)


def _initial_url(item: ProviderPlanItem) -> str:
    endpoint = _endpoint(item)
    query_params = {
        "interval": _INTERVAL,
        "start_time": _api_time(item.start),
        "end_time": _api_time(item.end),
    }
    if item.dataset == "market_depth":
        query_params.update({"page_size": "100", "sort": "asc"})
    query = urlencode(query_params)
    return f"{_BASE_URL}{endpoint}?{query}"


def _endpoint(item: ProviderPlanItem) -> str:
    endpoint = _DATASETS.get(item.dataset)
    if (
        endpoint is None
        or item.symbol != _CANONICAL_SYMBOL
        or item.params.get("endpoint") != endpoint
        or item.params.get("interval") != _INTERVAL
        or (
            item.dataset == "market_depth"
            and (
                item.params.get("page_size") != "100"
                or item.params.get("sort") != "asc"
            )
        )
    ):
        raise RuntimeError("Kaiko plan must specify a supported fixed endpoint")
    return endpoint


def _page(payload: object) -> tuple[list[dict[str, object]], str | None]:
    if not isinstance(payload, dict):
        raise RuntimeError("Kaiko response must be an object")
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(record, dict) for record in data):
        raise RuntimeError("Kaiko response missing data list")
    next_url = payload.get("next_url")
    if next_url is None:
        return data, None
    if not isinstance(next_url, str) or not next_url:
        raise RuntimeError("Kaiko returned an invalid next_url")
    return data, next_url


def _next_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != _HOST:
        raise RuntimeError("Kaiko returned an invalid next_url")
    return value


def _values(dataset: str, record: Mapping[str, object]) -> dict[str, float]:
    if dataset == "market_ohlcv":
        return {"trade_count": _number(record.get("count")), "vwap": _number(record.get("price"))}
    if dataset == "market_depth":
        return {
            "bid_depth": _number(record.get("bid_volume1")),
            "ask_depth": _number(record.get("ask_volume1")),
        }
    raise RuntimeError("Kaiko plan must specify a supported fixed endpoint")


def _record_timestamp(dataset: str, record: Mapping[str, object]) -> datetime:
    key = "poll_timestamp" if dataset == "market_depth" else "timestamp"
    return _timestamp(record.get(key))


def _timestamp(value: object) -> datetime:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise RuntimeError("Kaiko timestamps must be finite numbers")
    return datetime.fromtimestamp(value / 1_000, tz=timezone.utc)


def _api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise RuntimeError("Kaiko response values must be finite numbers")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise RuntimeError("Kaiko response values must be finite numbers") from None
    if not math.isfinite(result):
        raise RuntimeError("Kaiko response values must be finite numbers")
    return result


def _read_json(url: str, headers: dict[str, str]) -> object:
    return read_json(
        url,
        provider="kaiko",
        timeout_seconds=_TIMEOUT_SECONDS,
        headers=headers,
    )

