# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from ..http import read_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_BASE_URL = "https://api.blockchain.info/charts"
_TIMEOUT_SECONDS = 15
_DAILY_AVAILABILITY_DELAY = timedelta(days=1)
_CHART_FIELDS = {
    "n-unique-addresses": "active_addresses",
    "n-transactions": "transaction_count",
    "hash-rate": "hash_rate",
}


class BlockchainComProvider:
    name = "blockchain_com"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                provider=self.name,
                dataset="onchain",
                free=True,
                required_credentials=self.required_credentials,
                min_timeframe="1d",
                domain="onchain",
                tier="public",
                implemented=True,
                required_fields=tuple(_CHART_FIELDS.values()),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                provider=self.name,
                dataset="onchain",
                symbol="BTC",
                start=request.start_as_datetime(),
                end=request.end_as_datetime(),
                params={"charts": ",".join(_CHART_FIELDS), "coverage_period": "1d"},
            )
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        merged: dict[datetime, dict[str, object]] = {}
        for chart in _charts(item):
            payload = _read_json(_chart_url(chart, item.start, item.end))
            values = _values(payload)
            field = _CHART_FIELDS[chart]
            for value in values:
                timestamp, measurement = _point(value)
                day = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
                record = merged.setdefault(day, {"timestamp": day})
                record[field] = measurement
        return list(merged.values())

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        normalized: list[NormalizedRow] = []
        for record in records:
            timestamp = _timestamp(record["timestamp"])
            if not item.start <= timestamp < item.end:
                continue
            values = {field: float(record[field]) for field in _CHART_FIELDS.values() if field in record}
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


def _charts(item: ProviderPlanItem) -> tuple[str, ...]:
    charts = tuple(chart for chart in item.params.get("charts", "").split(",") if chart)
    if not charts:
        raise ValueError("Blockchain.com plan must specify supported charts")
    if len(charts) > len(_CHART_FIELDS):
        raise ValueError("Blockchain.com plan accepts at most three chart IDs")
    if len(set(charts)) != len(charts):
        raise ValueError("Blockchain.com plan must not contain duplicate chart IDs")
    if any(chart not in _CHART_FIELDS for chart in charts):
        raise ValueError("Blockchain.com plan must specify supported charts")
    return charts


def _chart_url(chart: str, start: datetime, end: datetime) -> str:
    timespan_days = max(1, math.ceil((end - start).total_seconds() / timedelta(days=1).total_seconds()))
    query = urlencode(
        {
            "timespan": f"{timespan_days}days",
            "format": "json",
            "sampled": "false",
            "start": start.astimezone(timezone.utc).date().isoformat(),
        }
    )
    return f"{_BASE_URL}/{chart}?{query}"


def _values(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise RuntimeError("Blockchain.com response must be an object")
    values = payload.get("values")
    if not isinstance(values, list):
        raise RuntimeError("Blockchain.com response missing values list")
    return values


def _point(value: object) -> tuple[datetime, float]:
    if not isinstance(value, dict):
        raise RuntimeError("Blockchain.com values must be objects")
    timestamp = value.get("x")
    measurement = value.get("y")
    if not _is_number(timestamp) or not _is_number(measurement):
        raise RuntimeError("Blockchain.com values require numeric x and y")
    return datetime.fromtimestamp(timestamp, tz=timezone.utc), float(measurement)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not _is_number(value):
        raise RuntimeError("Blockchain.com record timestamp must be numeric")
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _read_json(url: str) -> object:
    return read_json(url, provider="blockchain_com", timeout_seconds=_TIMEOUT_SECONDS)

