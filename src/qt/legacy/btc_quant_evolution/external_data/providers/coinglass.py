# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from ..http import read_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderHttpError,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_BASE_URL = "https://open-api-v4.coinglass.com"
_TIMEOUT_SECONDS = 15
_INTERVAL = "4h"
_MAX_RECORDS = 1_000
_MAX_WINDOWS = 100
_AVAILABILITY_DELAY = timedelta(hours=4)
_WINDOW = timedelta(hours=4 * _MAX_RECORDS)
_DATASETS = {
    "open_interest": {
        "endpoint": "/api/futures/open-interest/aggregated-history",
        "symbol": "BTC",
    },
    "funding_rate": {
        "endpoint": "/api/futures/funding-rate/oi-weight-history",
        "symbol": "BTC",
    },
    "liquidations": {
        "endpoint": "/api/futures/liquidation/history",
        "symbol": "BTCUSDT",
        "exchange": "Binance",
    },
}
_REQUIRED_FIELDS = {
    "open_interest": ("open_interest",),
    "funding_rate": ("funding_rate",),
    "liquidations": ("long_liquidation_usd", "short_liquidation_usd"),
}


class CoinGlassProvider:
    name = "coinglass"
    required_credentials = ("COINGLASS_API_KEY",)

    def capabilities(self) -> Sequence[ProviderCapability]:
        return tuple(
            ProviderCapability(
                provider=self.name,
                dataset=dataset,
                free=False,
                required_credentials=self.required_credentials,
                min_timeframe=_INTERVAL,
                domain="derivatives",
                tier="paid",
                implemented=True,
                required_fields=_REQUIRED_FIELDS[dataset],
            )
            for dataset in _DATASETS
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled" if env.get("COINGLASS_API_KEY", "").strip() else "missing_credentials"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                provider=self.name,
                dataset=dataset,
                symbol=str(config["symbol"]),
                start=request.start_as_datetime(),
                end=request.end_as_datetime(),
                params={
                    "endpoint": str(config["endpoint"]),
                    **({"exchange": str(config["exchange"])} if "exchange" in config else {}),
                    "interval": _INTERVAL,
                    "limit": str(_MAX_RECORDS),
                },
            )
            for dataset, config in _DATASETS.items()
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        api_key = os.environ.get("COINGLASS_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("CoinGlass API key is not configured")

        config = _plan_config(item)
        records_by_time: dict[datetime, dict[str, object]] = {}
        for window_start, window_end in _windows(item):
            query = {
                "symbol": item.symbol,
                "interval": _INTERVAL,
                "limit": str(_MAX_RECORDS),
                "start_time": str(_milliseconds(window_start)),
                "end_time": str(_milliseconds(window_end)),
                **({"exchange": str(config["exchange"])} if "exchange" in config else {}),
            }
            try:
                payload = _read_json(f"{_BASE_URL}{config['endpoint']}?{urlencode(query)}", {"CG-API-KEY": api_key})
            except ProviderHttpError:
                raise
            except Exception:
                raise RuntimeError("CoinGlass request failed") from None
            for record in _records(payload):
                timestamp = _timestamp(record.get("time"))
                _values(item.dataset, record)
                records_by_time.setdefault(timestamp, record)
        return [records_by_time[timestamp] for timestamp in sorted(records_by_time)]

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        _plan_config(item)
        normalized: list[NormalizedRow] = []
        for record in records:
            timestamp = _timestamp(record.get("time"))
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


def _plan_config(item: ProviderPlanItem) -> Mapping[str, str]:
    config = _DATASETS.get(item.dataset)
    if config is None or item.symbol != config["symbol"]:
        raise RuntimeError("CoinGlass plan must specify a supported dataset")
    if item.params.get("endpoint") != config["endpoint"]:
        raise RuntimeError("CoinGlass plan must specify a fixed endpoint")
    if item.params.get("interval") != _INTERVAL or item.params.get("limit") != str(_MAX_RECORDS):
        raise RuntimeError("CoinGlass plan must use the bounded 4h interval")
    if config.get("exchange") != item.params.get("exchange"):
        raise RuntimeError("CoinGlass plan must specify the fixed liquidation exchange")
    return config


def _records(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise RuntimeError("CoinGlass response was not successful")
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(record, dict) for record in data):
        raise RuntimeError("CoinGlass response missing data list")
    return data


def _windows(item: ProviderPlanItem) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    window_start = item.start
    while window_start < item.end:
        if len(windows) >= _MAX_WINDOWS:
            raise RuntimeError("CoinGlass request exceeded the bounded window limit")
        window_end = min(window_start + _WINDOW, item.end)
        windows.append((window_start, window_end))
        window_start = window_end
    return windows


def _values(dataset: str, record: Mapping[str, object]) -> dict[str, float]:
    if dataset == "open_interest":
        return {"open_interest": _number(record.get("close"))}
    if dataset == "funding_rate":
        return {"funding_rate": _number(record.get("close"))}
    if dataset == "liquidations":
        return {
            "long_liquidation_usd": _number(record.get("long_liquidation_usd")),
            "short_liquidation_usd": _number(record.get("short_liquidation_usd")),
        }
    raise RuntimeError("CoinGlass plan must specify a supported dataset")


def _timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(_number(value) / 1_000, tz=timezone.utc)


def _milliseconds(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1_000)


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise RuntimeError("CoinGlass response values must be finite numbers")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise RuntimeError("CoinGlass response values must be finite numbers") from None
    if not math.isfinite(result):
        raise RuntimeError("CoinGlass response values must be finite numbers")
    return result


def _read_json(url: str, headers: dict[str, str]) -> object:
    return read_json(
        url,
        provider="coinglass",
        timeout_seconds=_TIMEOUT_SECONDS,
        headers=headers,
    )

