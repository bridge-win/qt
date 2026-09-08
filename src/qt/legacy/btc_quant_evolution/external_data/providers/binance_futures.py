# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import urlencode

from qt.legacy.btc_quant_evolution.ohlcv_integrity import timeframe_to_seconds

from ..http import read_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_BASE_URL = "https://fapi.binance.com"
_TIMEOUT_SECONDS = 15
_MAX_RECORDS = 500
_MAX_PAGES = 10_000
_RECENT_HISTORY_LIMIT = timedelta(days=30)
_RECENT_ONLY_DATASETS = ("open_interest", "long_short_ratio", "taker_buy_sell")
_BTCUSDT_FUNDING_START = datetime(2019, 9, 10, tzinfo=timezone.utc)
_REQUIRED_FIELDS = {
    "funding_rate": ("funding_rate",),
    "open_interest": ("sumOpenInterest", "sumOpenInterestValue"),
    "long_short_ratio": ("longShortRatio", "longAccount", "shortAccount"),
    "taker_buy_sell": ("buySellRatio", "buyVol", "sellVol"),
}


class BinanceFuturesProvider:
    name = "binance_futures"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return tuple(
            ProviderCapability(
                self.name,
                dataset,
                True,
                self.required_credentials,
                "4h",
                required_fields=_REQUIRED_FIELDS[dataset],
            )
            for dataset in ("funding_rate", "open_interest", "long_short_ratio", "taker_buy_sell")
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        start = request.start_as_datetime()
        end = request.end_as_datetime()
        items = []
        funding_start = max(start, _BTCUSDT_FUNDING_START)
        if funding_start < end:
            items.append(
                ProviderPlanItem(
                    self.name,
                    "funding_rate",
                    request.symbol,
                    funding_start,
                    end,
                    {
                        "coverage_period": "8h",
                        "endpoint": "/fapi/v1/fundingRate",
                        "historical_start": _BTCUSDT_FUNDING_START.isoformat(),
                    },
                )
            )
        if self.unsupported_datasets(request):
            return items
        items.extend(
            [
                ProviderPlanItem(
                    self.name,
                    "open_interest",
                    request.symbol,
                    start,
                    end,
                    {"endpoint": "/futures/data/openInterestHist", "period": request.timeframe},
                ),
                ProviderPlanItem(
                    self.name,
                    "long_short_ratio",
                    request.symbol,
                    start,
                    end,
                    {
                        "endpoint": "/futures/data/globalLongShortAccountRatio",
                        "period": request.timeframe,
                    },
                ),
                ProviderPlanItem(
                    self.name,
                    "taker_buy_sell",
                    request.symbol,
                    start,
                    end,
                    {
                        "endpoint": "/futures/data/takerlongshortRatio",
                        "period": request.timeframe,
                    },
                ),
            ]
        )
        return items

    def unsupported_datasets(self, request: ProviderSyncRequest) -> dict[str, str]:
        start = request.start_as_datetime()
        end = request.end_as_datetime()
        now = _now()
        if end <= now and start >= now - _RECENT_HISTORY_LIMIT:
            return {}
        reason = (
            "Binance official futures statistics expose only the latest 30 days; "
            "the requested range is unsupported"
        )
        return {dataset: reason for dataset in _RECENT_ONLY_DATASETS}

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        cursor = item.start
        records: list[dict[str, object]] = []
        timestamp_key = "fundingTime" if item.dataset == "funding_rate" else "timestamp"
        for _ in range(_MAX_PAGES):
            query = urlencode(
                {
                    "symbol": item.symbol,
                    "startTime": _milliseconds(cursor),
                    "endTime": _milliseconds(item.end),
                    "limit": _MAX_RECORDS,
                    **({"period": item.params["period"]} if item.dataset != "funding_rate" else {}),
                }
            )
            payload = cast(list[dict[str, object]], _read_json(f"{_BASE_URL}{item.params['endpoint']}?{query}"))
            if not payload:
                return records
            records.extend(payload)
            if len(payload) < _MAX_RECORDS:
                return records
            cursor = _from_milliseconds(payload[-1][timestamp_key]) + timedelta(milliseconds=1)
            if cursor >= item.end:
                return records
        raise RuntimeError("Binance futures pagination exceeded the bounded page limit")

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        normalized = [_normalize_record(self.name, item, record) for record in records]
        return [row for row in normalized if item.start <= row.timestamp < item.end]


def _normalize_record(provider: str, item: ProviderPlanItem, record: dict[str, object]) -> NormalizedRow:
    timestamp_key = "fundingTime" if item.dataset == "funding_rate" else "timestamp"
    observed_at = _from_milliseconds(record[timestamp_key])
    timestamp = observed_at
    value_keys = {
        "funding_rate": ("fundingRate",),
        "open_interest": ("sumOpenInterest", "sumOpenInterestValue"),
        "long_short_ratio": ("longShortRatio", "longAccount", "shortAccount"),
        "taker_buy_sell": ("buySellRatio", "buyVol", "sellVol"),
    }[item.dataset]
    values = {key: float(record[key]) for key in value_keys if key in record}
    if item.dataset == "funding_rate":
        values = {"funding_rate": values["fundingRate"]}
        period_seconds = timeframe_to_seconds(item.params.get("coverage_period", "8h"))
        epoch_seconds = int(observed_at.timestamp())
        timestamp = datetime.fromtimestamp(epoch_seconds - epoch_seconds % period_seconds, tz=timezone.utc)
    available_at = observed_at
    if item.dataset == "taker_buy_sell":
        period = item.params.get("period")
        if period is None:
            raise ValueError("Binance taker volume requires its aggregation period")
        available_at += timedelta(seconds=timeframe_to_seconds(period))
    return NormalizedRow(timestamp, available_at, provider, item.dataset, item.symbol, values)


def _read_json(url: str) -> object:
    return read_json(url, provider="binance_futures", timeout_seconds=_TIMEOUT_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def _from_milliseconds(value: object) -> datetime:
    return datetime.fromtimestamp(int(cast(int | str, value)) / 1_000, tz=timezone.utc)

