# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import cast

from ..http import read_json
from ..schemas import (
    CredentialStatus,
    NormalizedRow,
    ProviderCapability,
    ProviderPlanItem,
    ProviderSyncRequest,
)

_URL = "https://api.alternative.me/fng/?limit=0&format=json"
_TIMEOUT_SECONDS = 15
_DAILY_AVAILABILITY_DELAY = timedelta(days=1)


class AlternativeMeProvider:
    name = "alternative_me"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                self.name,
                "fear_greed",
                True,
                self.required_credentials,
                "1d",
                required_fields=("fear_greed_value",),
                max_missing_fraction=0.005,
                max_consecutive_missing_intervals=2,
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        return [
            ProviderPlanItem(
                self.name,
                "fear_greed",
                "BTC",
                request.start_as_datetime(),
                request.end_as_datetime(),
                {"coverage_period": "1d", "endpoint": "/fng/"},
            )
        ]

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        payload = read_json(
            _URL,
            provider=self.name,
            timeout_seconds=_TIMEOUT_SECONDS,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Alternative.me response must be an object")
        records = cast(list[dict[str, object]], payload["data"])
        return [record for record in records if item.start <= _timestamp(record["timestamp"]) < item.end]

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        return [
            NormalizedRow(
                timestamp=_timestamp(record["timestamp"]),
                available_at=_timestamp(record["timestamp"]) + _DAILY_AVAILABILITY_DELAY,
                source=self.name,
                dataset=item.dataset,
                symbol=item.symbol,
                values={"fear_greed_value": float(record["value"]), "fear_greed_classification": str(record["value_classification"])},
            )
            for record in records
        ]


def _timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(int(cast(int | str, value)), tz=timezone.utc)

