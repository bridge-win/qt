# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import urlencode

from ..http import read_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_BASE_URL = "https://api.binance.com"
_TIMEOUT_SECONDS = 15
_MAX_RECORDS = 1_000
_MAX_PAGES = 10_000


class BinanceSpotProvider:
    name = "binance_spot"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                self.name,
                "ohlcv",
                True,
                self.required_credentials,
                "4h",
                required_fields=("open", "high", "low", "close", "volume"),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                self.name,
                "ohlcv",
                request.symbol,
                request.start_as_datetime(),
                request.end_as_datetime(),
                {"endpoint": "/api/v3/klines", "interval": request.timeframe},
            )
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        cursor = item.start
        interval = item.params.get("interval", "4h")
        records: list[dict[str, object]] = []
        for _ in range(_MAX_PAGES):
            query = urlencode(
                {
                    "symbol": item.symbol,
                    "interval": interval,
                    "startTime": _milliseconds(cursor),
                    "endTime": _milliseconds(item.end),
                    "limit": _MAX_RECORDS,
                }
            )
            payload = cast(list[list[object]], _read_json(f"{_BASE_URL}{item.params['endpoint']}?{query}"))
            if not payload:
                return records
            records.extend(
                {"open_time": record[0], "open": record[1], "high": record[2], "low": record[3], "close": record[4], "volume": record[5]}
                for record in payload
            )
            if len(payload) < _MAX_RECORDS:
                return records
            cursor = _from_milliseconds(payload[-1][0]) + _interval_duration(interval)
            if cursor >= item.end:
                return records
        raise RuntimeError("Binance spot pagination exceeded the bounded page limit")

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        normalized: list[NormalizedRow] = []
        interval = _interval_duration(item.params.get("interval", "4h"))
        for record in records:
            timestamp = _from_milliseconds(record["open_time"])
            if not item.start <= timestamp < item.end:
                continue
            normalized.append(
                NormalizedRow(
                    timestamp=timestamp,
                    available_at=timestamp + interval,
                    source=self.name,
                    dataset=item.dataset,
                    symbol=item.symbol,
                    values={key: float(record[key]) for key in ("open", "high", "low", "close", "volume")},
                )
            )
        return normalized


def _read_json(url: str) -> object:
    return read_json(url, provider="binance_spot", timeout_seconds=_TIMEOUT_SECONDS)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def _from_milliseconds(value: object) -> datetime:
    return datetime.fromtimestamp(int(cast(int | str, value)) / 1_000, tz=timezone.utc)


def _interval_duration(interval: str) -> timedelta:
    unit = interval[-1]
    amount = int(interval[:-1])
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    raise ValueError(f"unsupported Binance interval: {interval}")

