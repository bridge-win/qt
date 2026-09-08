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

_BASE_URL = "https://api.glassnode.com"
_TIMEOUT_SECONDS = 15
_DAILY_AVAILABILITY_DELAY = timedelta(days=1)
_METRICS = {
    "active_addresses": "/v1/metrics/addresses/active_count",
    "exchange_netflow": "/v1/metrics/transactions/transfers_volume_exchanges_net",
}


class GlassnodeProvider:
    name = "glassnode"
    required_credentials = ("GLASSNODE_API_KEY",)

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                provider=self.name,
                dataset="onchain",
                free=False,
                required_credentials=self.required_credentials,
                min_timeframe="1d",
                domain="onchain",
                tier="paid",
                implemented=True,
                required_fields=tuple(_METRICS),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled" if env.get("GLASSNODE_API_KEY", "").strip() else "missing_credentials"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                provider=self.name,
                dataset="onchain",
                symbol="BTC",
                start=request.start_as_datetime(),
                end=request.end_as_datetime(),
                params={"metrics": ",".join(_METRICS), "coverage_period": "1d"},
            )
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        api_key = os.environ.get("GLASSNODE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Glassnode API key is not configured")

        merged: dict[datetime, dict[str, object]] = {}
        for metric in _requested_metrics(item):
            try:
                url = _metric_url(_METRICS[metric], item.start, item.end, api_key)
                payload = _read_json(url)
            except ProviderHttpError:
                raise
            except Exception:
                raise RuntimeError("Glassnode request failed") from None
            for timestamp, value in _series(payload):
                record = merged.setdefault(timestamp, {"timestamp": timestamp})
                record[metric] = value
        return list(merged.values())

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        normalized: list[NormalizedRow] = []
        for record in records:
            timestamp = _timestamp(record.get("timestamp"))
            if not item.start <= timestamp < item.end:
                continue
            values = {metric: _measurement(record[metric]) for metric in _METRICS if metric in record}
            normalized.append(
                NormalizedRow(
                    timestamp=timestamp,
                    available_at=timestamp + _DAILY_AVAILABILITY_DELAY,
                    source=self.name,
                    dataset=item.dataset,
                    symbol=item.symbol,
                    values=values,
                )
            )
        return sorted(normalized, key=lambda row: row.timestamp)


def _requested_metrics(item: ProviderPlanItem) -> tuple[str, ...]:
    metrics = tuple(metric for metric in item.params.get("metrics", ",".join(_METRICS)).split(",") if metric)
    if not metrics or len(metrics) > len(_METRICS) or len(set(metrics)) != len(metrics) or any(metric not in _METRICS for metric in metrics):
        raise RuntimeError("Glassnode plan must specify supported metrics")
    return metrics


def _metric_url(endpoint: str, start: datetime, end: datetime, api_key: str) -> str:
    query = urlencode(
        {
            "a": "BTC",
            "s": int(start.timestamp()),
            "u": int(end.timestamp()),
            "i": "24h",
            "f": "json",
            "api_key": api_key,
        }
    )
    return f"{_BASE_URL}{endpoint}?{query}"


def _series(payload: object) -> list[tuple[datetime, float]]:
    if not isinstance(payload, list):
        raise RuntimeError("Glassnode response must be a list")
    points: list[tuple[datetime, float]] = []
    for point in payload:
        if not isinstance(point, dict):
            raise RuntimeError("Glassnode response points must be objects")
        points.append((_timestamp(point.get("t")), _measurement(point.get("v"))))
    return points


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not _is_number(value):
        raise RuntimeError("Glassnode timestamps must be numeric")
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _measurement(value: object) -> float:
    if not _is_number(value):
        raise RuntimeError("Glassnode values must be numeric")
    return float(value)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _read_json(url: str) -> object:
    return read_json(url, provider="glassnode", timeout_seconds=_TIMEOUT_SECONDS)

