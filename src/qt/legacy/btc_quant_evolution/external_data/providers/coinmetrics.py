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

_BASE_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
_TIMEOUT_SECONDS = 15
_DAILY_AVAILABILITY_DELAY = timedelta(days=1)
_MAX_PAGES = 10_000


class CoinMetricsProvider:
    name = "coinmetrics"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                self.name,
                "onchain",
                True,
                self.required_credentials,
                "1d",
                required_fields=("active_addresses", "transaction_count"),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                self.name,
                "onchain",
                "BTC",
                request.start_as_datetime(),
                request.end_as_datetime(),
                {"assets": "btc", "coverage_period": "1d", "metrics": "AdrActCnt,TxCnt"},
            )
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        query = urlencode(
            {
                "assets": item.params["assets"],
                "metrics": item.params["metrics"],
                "frequency": "1d",
                "start_time": _api_time(item.start),
                "end_time": _api_time(item.end),
            }
        )
        url = f"{_BASE_URL}?{query}"
        records: list[dict[str, object]] = []
        for _ in range(_MAX_PAGES):
            payload = cast(dict[str, object], _read_json(url))
            page = payload.get("data")
            if not isinstance(page, list):
                raise RuntimeError("CoinMetrics response missing data list")
            records.extend(cast(list[dict[str, object]], page))
            next_page_url = payload.get("next_page_url")
            if next_page_url is None:
                return records
            if not isinstance(next_page_url, str) or not next_page_url:
                raise RuntimeError("CoinMetrics returned an invalid next_page_url")
            url = next_page_url
        raise RuntimeError("CoinMetrics pagination exceeded the bounded page limit")

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        normalized: list[NormalizedRow] = []
        for record in records:
            timestamp = _timestamp(record["time"])
            if not item.start <= timestamp < item.end:
                continue
            normalized.append(
                NormalizedRow(
                    timestamp=timestamp,
                    available_at=timestamp + _DAILY_AVAILABILITY_DELAY,
                    source=self.name,
                    dataset=item.dataset,
                    symbol=item.symbol,
                    values={"active_addresses": float(record["AdrActCnt"]), "transaction_count": float(record["TxCnt"])},
                )
            )
        return normalized


def _timestamp(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(url: str) -> object:
    return read_json(url, provider="coinmetrics", timeout_seconds=_TIMEOUT_SECONDS)

