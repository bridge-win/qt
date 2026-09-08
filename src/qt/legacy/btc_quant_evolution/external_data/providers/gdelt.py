# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from statistics import median
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

_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TIMEOUT_SECONDS = 15
_DAILY_AVAILABILITY_DELAY = timedelta(days=1)
_WINDOW = timedelta(days=1)
_MAX_RECORDS = 250
_ROLLING_HISTORY = timedelta(days=90)


class GdeltProvider:
    name = "gdelt"
    required_credentials: tuple[str, ...] = ()

    def capabilities(self) -> Sequence[ProviderCapability]:
        return (
            ProviderCapability(
                self.name,
                "macro_news",
                True,
                self.required_credentials,
                "1d",
                required_fields=("tone", "article_count"),
            ),
        )

    def credential_status(self, env: Mapping[str, str]) -> CredentialStatus:
        return "enabled"

    def plan_sync(self, request: ProviderSyncRequest) -> list[ProviderPlanItem]:
        if request.start_as_datetime() < _utc_now() - _ROLLING_HISTORY:
            return []
        return [ProviderPlanItem(self.name, "macro_news", request.symbol, request.start_as_datetime(), request.end_as_datetime(), {"query": "bitcoin", "endpoint": "/api/v2/doc/doc", "coverage_period": "1d"})]

    def unsupported_datasets(self, request: ProviderSyncRequest) -> dict[str, str]:
        if request.start_as_datetime() < _utc_now() - _ROLLING_HISTORY:
            return {
                "macro_news": (
                    "GDELT DOC precise-date searches are limited to a rolling recent-history window; "
                    "the requested range is unsupported"
                )
            }
        return {}

    def fetch(self, item: ProviderPlanItem) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        window_start = item.start
        while window_start < item.end:
            window_end = min(window_start + _WINDOW, item.end)
            query = urlencode(
                {
                    "query": item.params["query"],
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": str(_MAX_RECORDS),
                    "startdatetime": window_start.strftime("%Y%m%d%H%M%S"),
                    "enddatetime": window_end.strftime("%Y%m%d%H%M%S"),
                }
            )
            payload = _read_json(f"{_BASE_URL}?{query}")
            if not isinstance(payload, dict):
                raise RuntimeError("GDELT response must be an object")
            articles = cast(list[dict[str, object]], payload.get("articles", []))
            if len(articles) == _MAX_RECORDS:
                raise RuntimeError("GDELT window reached the record limit; narrow the requested range")
            records.extend(articles)
            window_start = window_end
        return records

    def normalize(self, item: ProviderPlanItem, records: list[dict[str, object]]) -> list[NormalizedRow]:
        tones_by_day: defaultdict[datetime, list[float]] = defaultdict(list)
        for record in records:
            observed_at = _timestamp(record["seendate"])
            day = observed_at.replace(hour=0, minute=0, second=0, microsecond=0)
            if item.start <= day < item.end:
                tones_by_day[day].append(float(record.get("tone", 0.0)))

        rows: list[NormalizedRow] = []
        day = item.start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < item.end:
            tones = tones_by_day[day]
            # Publish only after the timezone.utc bucket closes so article count and tone stay causal.
            rows.append(
                NormalizedRow(
                    timestamp=day,
                    available_at=day + _DAILY_AVAILABILITY_DELAY,
                    source=self.name,
                    dataset=item.dataset,
                    symbol=item.symbol,
                    values={
                        "tone": float(median(tones)) if tones else 0.0,
                        "article_count": float(len(tones)),
                    },
                )
            )
            day += _WINDOW
        return rows


def _timestamp(value: object) -> datetime:
    return datetime.strptime(str(value), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(url: str) -> object:
    return read_json(url, provider="gdelt", timeout_seconds=_TIMEOUT_SECONDS)

