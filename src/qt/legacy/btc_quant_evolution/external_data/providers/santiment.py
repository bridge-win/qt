# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone

from ..http import post_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderHttpError,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_BASE_URL = "https://api.santiment.net/graphql"
_TIMEOUT_SECONDS = 15
_DAILY_AVAILABILITY_DELAY = timedelta(days=1)
_DATASET_METRICS = {
    "onchain": {
        "active_addresses": "active_addresses_24h",
        "transaction_volume": "transaction_volume",
    },
    "sentiment": {
        "social_volume": "social_volume_total",
    },
}
_QUERY = """
query GetMetric($metric: String!, $slug: String!, $from: DateTime!, $to: DateTime!, $interval: String!) {
  getMetric(metric: $metric) {
    timeseriesDataJson(slug: $slug, from: $from, to: $to, interval: $interval)
  }
}
"""


class SantimentProvider:
    name = "santiment"
    required_credentials = ("SANTIMENT_API_KEY",)

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
                required_fields=tuple(_DATASET_METRICS["onchain"]),
            ),
            ProviderCapability(
                provider=self.name,
                dataset="sentiment",
                free=False,
                required_credentials=self.required_credentials,
                min_timeframe="1d",
                domain="sentiment",
                tier="paid",
                implemented=True,
                required_fields=tuple(_DATASET_METRICS["sentiment"]),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled" if env.get("SANTIMENT_API_KEY", "").strip() else "missing_credentials"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                provider=self.name,
                dataset=dataset,
                symbol="BTC",
                start=request.start_as_datetime(),
                end=request.end_as_datetime(),
                params={"metrics": ",".join(metrics), "coverage_period": "1d"},
            )
            for dataset, metrics in _DATASET_METRICS.items()
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        api_key = os.environ.get("SANTIMENT_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Santiment API key is not configured")

        headers = {"Authorization": f"Apikey {api_key}", "Content-Type": "application/json"}
        merged: dict[datetime, dict[str, object]] = {}
        for value_name, metric in _requested_metrics(item):
            payload = {"query": _QUERY, "variables": _variables(metric, item.start, item.end)}
            try:
                response = _post_json(_BASE_URL, headers, payload)
            except ProviderHttpError:
                raise
            except Exception:
                raise RuntimeError("Santiment request failed") from None
            for timestamp, value in _timeseries(response):
                record = merged.setdefault(timestamp, {"timestamp": timestamp})
                record[value_name] = value
        return list(merged.values())

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        normalized: list[NormalizedRow] = []
        for record in records:
            timestamp = _timestamp(record.get("timestamp"))
            if not item.start <= timestamp < item.end:
                continue
            values = {
                value_name: _measurement(record[value_name])
                for value_name in _dataset_metrics(item.dataset)
                if value_name in record
            }
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


def _requested_metrics(item: ProviderPlanItem) -> tuple[tuple[str, str], ...]:
    metrics = _dataset_metrics(item.dataset)
    value_names = tuple(metric for metric in item.params.get("metrics", ",".join(metrics)).split(",") if metric)
    if not value_names or len(value_names) > len(metrics) or len(set(value_names)) != len(value_names) or any(metric not in metrics for metric in value_names):
        raise RuntimeError("Santiment plan must specify supported metrics")
    return tuple((value_name, metrics[value_name]) for value_name in value_names)


def _dataset_metrics(dataset: str) -> Mapping[str, str]:
    metrics = _DATASET_METRICS.get(dataset)
    if metrics is None:
        raise RuntimeError("Santiment plan must specify a supported dataset")
    return metrics


def _variables(metric: str, start: datetime, end: datetime) -> dict[str, str]:
    return {
        "metric": metric,
        "slug": "bitcoin",
        "from": _api_time(start),
        "to": _api_time(end),
        "interval": "1d",
    }


def _api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timeseries(payload: object) -> list[tuple[datetime, float]]:
    if not isinstance(payload, dict):
        raise RuntimeError("Santiment response must be an object")
    if "errors" in payload:
        raise RuntimeError("Santiment GraphQL response contains errors")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Santiment response missing data")
    metric = data.get("getMetric")
    if not isinstance(metric, dict):
        raise RuntimeError("Santiment response missing metric")
    points = metric.get("timeseriesDataJson")
    if not isinstance(points, list):
        raise RuntimeError("Santiment timeseries data must be a list")
    return [_point(point) for point in points]


def _point(point: object) -> tuple[datetime, float]:
    if not isinstance(point, dict):
        raise RuntimeError("Santiment timeseries points must be objects")
    return _timestamp(point.get("datetime")), _measurement(point.get("value"))


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        raise RuntimeError("Santiment timestamps must be ISO-8601 strings")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise RuntimeError("Santiment timestamps must be ISO-8601 strings") from None


def _measurement(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise RuntimeError("Santiment values must be numeric")
    return float(value)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, object]) -> object:
    return post_json(
        url,
        provider="santiment",
        timeout_seconds=_TIMEOUT_SECONDS,
        headers=headers,
        payload=payload,
    )

