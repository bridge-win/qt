"""Allowlisted Coin Metrics Community on-chain signal provider."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Final, Literal

import httpx
from pydantic import TypeAdapter, ValidationError

from btc_backtest.errors import NetworkUnavailableError, ProviderError
from btc_backtest.signals.models import (
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)

COIN_METRICS_ASSET_METRICS_URL: Final = (
    "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
)
_PROVIDER_ID: Final = "coinmetrics"
_SOURCE_TYPE: Final = "onchain"
_SYMBOL: Final = "BTC/USD"
_ASSET: Final = "btc"
_FIELD_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_DATETIME_ADAPTER: Final = TypeAdapter(datetime)

MetricTransform = Literal["centered", "change"]
MetricDirection = Literal["trend", "contrarian"]


@dataclass(frozen=True)
class MetricRule:
    """Auditable allowlist rule for one Coin Metrics response field."""

    source_field: str
    transform: MetricTransform
    direction: MetricDirection
    horizon: str
    expiry: timedelta
    center: Decimal | None = None
    scale: Decimal = Decimal("1")
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        if not _FIELD_PATTERN.fullmatch(self.source_field):
            raise ValueError("metric source field is invalid")
        if self.transform not in ("centered", "change"):
            raise ValueError("metric transform is invalid")
        if self.direction not in ("trend", "contrarian"):
            raise ValueError("metric direction is invalid")
        if not self.horizon.strip():
            raise ValueError("metric horizon cannot be empty")
        if self.expiry <= timedelta(0):
            raise ValueError("metric expiry must be positive")
        if not self.scale.is_finite() or self.scale <= 0:
            raise ValueError("metric scale must be finite and positive")
        if self.transform == "centered" and self.center is None:
            raise ValueError("centered metric transform requires center")
        for name, value in (
            ("center", self.center),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"metric {name} must be finite")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("metric minimum cannot exceed maximum")


@dataclass(frozen=True)
class _CoinMetricsPage:
    rows: tuple[dict[str, object], ...]
    next_page_token: str | None


class CoinMetricsSignalProvider:
    """Fetch configured public BTC metrics with publication-time gating."""

    metadata = SignalProviderMetadata(
        id=_PROVIDER_ID,
        description=(
            "Allowlisted Coin Metrics Community API BTC on-chain metrics"
        ),
        source_types=(_SOURCE_TYPE,),
        historical=True,
        requires_credentials=False,
    )

    def __init__(
        self,
        metrics: Mapping[str, MetricRule],
        client: httpx.Client,
        page_size: int = 10_000,
        *,
        max_retries: int = 3,
        retry_backoff: float = 0.25,
    ) -> None:
        if not metrics:
            raise ValueError("at least one Coin Metrics rule is required")
        if not 1 <= page_size <= 10_000:
            raise ValueError("page_size must be between 1 and 10000")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        copied: dict[str, MetricRule] = {}
        source_fields: set[str] = set()
        for name, rule in metrics.items():
            if not _NAME_PATTERN.fullmatch(name):
                raise ValueError(f"invalid metric rule name: {name}")
            if rule.source_field in source_fields:
                raise ValueError(
                    f"duplicate Coin Metrics source field: "
                    f"{rule.source_field}"
                )
            copied[name] = rule
            source_fields.add(rule.source_field)
        self.metrics = MappingProxyType(dict(sorted(copied.items())))
        self.client = client
        self.page_size = page_size
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        selected = self._selected_rules(query)
        params: dict[str, int | str] = {
            "assets": _ASSET,
            "metrics": ",".join(
                sorted(rule.source_field for rule in selected.values())
            ),
            "frequency": "1d",
            "start_time": _isoformat(query.start),
            "end_time": _isoformat(
                query.end - timedelta(microseconds=1)
            ),
            "page_size": self.page_size,
            "paging_from": "start",
            "sort": "time",
        }
        observations: list[SignalObservation] = []
        previous_values: dict[str, Decimal] = {}
        seen_events: set[tuple[str, datetime]] = set()
        seen_tokens: set[str] = set()
        next_token: str | None = None

        while True:
            page_params = dict(params)
            if next_token is not None:
                page_params["next_page_token"] = next_token
            response = self._request_page(page_params)
            response_time = _response_time(response)
            payload = _response_payload(response)
            for raw in payload.rows:
                observations.extend(
                    self._map_row(
                        raw=raw,
                        query=query,
                        rules=selected,
                        response_time=response_time,
                        previous_values=previous_values,
                        seen_events=seen_events,
                    )
                )
            if payload.next_page_token is None:
                break
            if payload.next_page_token in seen_tokens:
                raise ProviderError(
                    "Coin Metrics pagination token stalled"
                )
            seen_tokens.add(payload.next_page_token)
            next_token = payload.next_page_token

        return tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.effective_at,
                    item.source_event_id,
                ),
            )
        )

    def _map_row(
        self,
        *,
        raw: dict[str, object],
        query: SignalQuery,
        rules: Mapping[str, MetricRule],
        response_time: datetime,
        previous_values: dict[str, Decimal],
        seen_events: set[tuple[str, datetime]],
    ) -> tuple[SignalObservation, ...]:
        if raw.get("asset") != _ASSET:
            raise ProviderError(
                "Coin Metrics response asset does not match btc"
            )
        effective_at = _row_time(raw.get("time"))
        if not query.start <= effective_at < query.end:
            raise ProviderError(
                "Coin Metrics row is outside query interval"
            )
        observations: list[SignalObservation] = []
        for name, rule in rules.items():
            if rule.source_field not in raw:
                raise ProviderError(
                    f"Coin Metrics row is missing {rule.source_field}"
                )
            raw_value = raw[rule.source_field]
            if raw_value is None:
                continue
            value = _metric_value(raw_value, rule)
            direction, transform_flags = _transform(
                value=value,
                previous=previous_values.get(name),
                rule=rule,
            )
            previous_values[name] = value
            event_identity = (rule.source_field, effective_at)
            if event_identity in seen_events:
                raise ProviderError(
                    "Coin Metrics returned a duplicate metric event"
                )
            seen_events.add(event_identity)
            status_key = f"{rule.source_field}-status"
            status_time_key = f"{rule.source_field}-status-time"
            status = raw.get(status_key)
            raw_status_time = raw.get(status_time_key)
            flags: tuple[str, ...] = (
                "historical",
                rule.direction,
                f"transform_{rule.transform}",
                "attribution_required",
                *transform_flags,
            )
            if raw_status_time is None:
                observed_at = response_time
                flags = (*flags, "delayed_observation")
            else:
                observed_at = _row_time(raw_status_time)
            if observed_at < effective_at:
                raise ProviderError(
                    "Coin Metrics observation time precedes metric time"
                )
            if isinstance(status, str) and status.strip():
                flags = (*flags, f"status_{_slug(status)}")
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    raw,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            timestamp_key = _isoformat(effective_at)
            source_event_id = (
                f"{rule.source_field}:{_ASSET}:{timestamp_key}"
            )
            observations.append(
                SignalObservation(
                    id=f"{_PROVIDER_ID}:{source_event_id}",
                    source_event_id=source_event_id,
                    provider=_PROVIDER_ID,
                    source_type=_SOURCE_TYPE,
                    symbol=query.symbol,
                    horizon=rule.horizon,
                    direction=direction,
                    confidence=abs(direction),
                    raw_value=value,
                    observed_at=observed_at,
                    effective_at=effective_at,
                    expires_at=max(effective_at, observed_at) + rule.expiry,
                    provenance=(
                        f"{COIN_METRICS_ASSET_METRICS_URL}"
                        f"#{source_event_id}"
                    ),
                    payload_sha256=payload_sha256,
                    quality_flags=flags,
                )
            )
        return tuple(observations)

    def _request_page(
        self,
        params: dict[str, int | str],
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(
                    COIN_METRICS_ASSET_METRICS_URL,
                    params=params,
                )
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise NetworkUnavailableError(
                        "Coin Metrics was unreachable after bounded retries"
                    ) from error
                self._wait_before_retry(attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise ProviderError(
                        f"Coin Metrics returned HTTP "
                        f"{response.status_code} after bounded retries"
                    )
                self._wait_before_retry(attempt)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    f"Coin Metrics rejected asset metrics request with "
                    f"HTTP {response.status_code}"
                ) from error
            return response
        raise ProviderError(
            "Coin Metrics request failed without a response"
        )

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2**attempt)
        if delay:
            time.sleep(delay)

    def _selected_rules(
        self,
        query: SignalQuery,
    ) -> Mapping[str, MetricRule]:
        if query.symbol != _SYMBOL:
            raise ProviderError(
                f"Coin Metrics does not support symbol {query.symbol}"
            )
        if query.source_types and _SOURCE_TYPE not in query.source_types:
            raise ProviderError(
                "Coin Metrics does not support requested source"
            )
        selected = {
            name: rule
            for name, rule in self.metrics.items()
            if rule.horizon in query.horizons
        }
        if not selected:
            raise ProviderError(
                "Coin Metrics has no rule for requested horizon"
            )
        return MappingProxyType(selected)


def _response_payload(
    response: httpx.Response,
) -> _CoinMetricsPage:
    try:
        payload = TypeAdapter(dict[str, object]).validate_json(
            response.content
        )
        rows = TypeAdapter(
            list[dict[str, object]]
        ).validate_python(payload["data"])
        raw_token = payload.get("next_page_token")
    except (KeyError, ValidationError, ValueError) as error:
        raise ProviderError(
            "Coin Metrics returned malformed asset metrics payload"
        ) from error
    if raw_token is not None and not isinstance(raw_token, str):
        raise ProviderError(
            "Coin Metrics returned malformed pagination token"
        )
    if raw_token == "":
        raise ProviderError(
            "Coin Metrics returned malformed pagination token"
        )
    return _CoinMetricsPage(
        rows=tuple(rows),
        next_page_token=raw_token,
    )


def _response_time(response: httpx.Response) -> datetime:
    raw_date = response.headers.get("Date")
    if raw_date is None:
        return datetime.now(timezone.utc)
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError) as error:
        raise ProviderError(
            "Coin Metrics returned invalid response date"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderError("Coin Metrics response date lacks timezone")
    return parsed.astimezone(timezone.utc)


def _row_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProviderError("Coin Metrics returned invalid metric time")
    try:
        parsed = _DATETIME_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise ProviderError(
            "Coin Metrics returned invalid metric time"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderError("Coin Metrics metric time lacks timezone")
    return parsed.astimezone(timezone.utc)


def _metric_value(value: object, rule: MetricRule) -> Decimal:
    if not isinstance(value, str):
        raise ProviderError(
            f"Coin Metrics returned invalid {rule.source_field}"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ProviderError(
            f"Coin Metrics returned invalid {rule.source_field}"
        ) from error
    if not parsed.is_finite():
        raise ProviderError(
            f"Coin Metrics returned invalid {rule.source_field}"
        )
    if rule.minimum is not None and parsed < rule.minimum:
        raise ProviderError(
            f"Coin Metrics returned invalid {rule.source_field}"
        )
    if rule.maximum is not None and parsed > rule.maximum:
        raise ProviderError(
            f"Coin Metrics returned invalid {rule.source_field}"
        )
    return parsed


def _transform(
    *,
    value: Decimal,
    previous: Decimal | None,
    rule: MetricRule,
) -> tuple[Decimal, tuple[str, ...]]:
    flags: tuple[str, ...] = ()
    if rule.transform == "centered":
        if rule.center is None:
            raise ProviderError("centered metric rule has no center")
        normalized = (value - rule.center) / rule.scale
    elif previous is None or previous == 0:
        normalized = Decimal("0")
        flags = ("insufficient_baseline",)
    else:
        normalized = ((value - previous) / abs(previous)) / rule.scale
    if rule.direction == "contrarian":
        normalized = -normalized
    return _clip(normalized), flags


def _clip(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(Decimal("1"), value))


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return normalized.strip("_") or "unknown"
