"""Historical Alternative.me Fear & Greed signal provider."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from btc_backtest.errors import NetworkUnavailableError, ProviderError
from btc_backtest.signals.models import (
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)

ALTERNATIVE_FEAR_GREED_URL: Final = "https://api.alternative.me/fng/"
_PROVIDER_ID: Final = "alternative_fear_greed"
_SOURCE_TYPE: Final = "sentiment"
_SYMBOL: Final = "BTC/USD"
_HORIZON: Final = "1d"
_EXPIRY: Final = timedelta(hours=36)


class _FearGreedRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: str
    timestamp: str
    value_classification: str | None = None
    time_until_update: str | None = None


class FearGreedSignalProvider:
    """Normalize Alternative.me's public daily sentiment archive."""

    metadata = SignalProviderMetadata(
        id=_PROVIDER_ID,
        description="Alternative.me historical Crypto Fear & Greed index",
        source_types=(_SOURCE_TYPE,),
        historical=True,
        requires_credentials=False,
    )

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_retries: int = 3,
        retry_backoff: float = 0.25,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        self.client = client
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        self._validate_query(query)
        response = self._request()
        try:
            payload = TypeAdapter(dict[str, object]).validate_json(
                response.content
            )
            raw_data = payload["data"]
            rows = TypeAdapter(
                list[dict[str, object]]
            ).validate_python(raw_data)
        except (KeyError, ValidationError, ValueError) as error:
            raise ProviderError(
                "Alternative.me returned malformed Fear & Greed payload"
            ) from error

        observations: list[SignalObservation] = []
        seen_timestamps: set[int] = set()
        for raw in rows:
            try:
                row = _FearGreedRow.model_validate(raw)
            except ValidationError as error:
                raise ProviderError(
                    "Alternative.me returned malformed Fear & Greed row"
                ) from error
            timestamp = _timestamp(row.timestamp)
            if timestamp in seen_timestamps:
                raise ProviderError(
                    "Alternative.me returned duplicate Fear & Greed timestamp"
                )
            seen_timestamps.add(timestamp)
            observed_at = datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )
            if not query.start <= observed_at < query.end:
                continue
            value = _value(row.value)
            direction = _clip(
                (Decimal("50") - value) / Decimal("50")
            )
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    raw,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            event_key = f"fear_greed:{timestamp}"
            observations.append(
                SignalObservation(
                    id=f"{_PROVIDER_ID}:{event_key}",
                    source_event_id=event_key,
                    provider=_PROVIDER_ID,
                    source_type=_SOURCE_TYPE,
                    symbol=query.symbol,
                    horizon=_HORIZON,
                    direction=direction,
                    confidence=abs(direction),
                    raw_value=value,
                    observed_at=observed_at,
                    effective_at=observed_at,
                    expires_at=observed_at + _EXPIRY,
                    provenance=(
                        f"{ALTERNATIVE_FEAR_GREED_URL}#{event_key}"
                    ),
                    payload_sha256=payload_sha256,
                    quality_flags=(
                        "historical",
                        "contrarian",
                        "native_1d",
                        "attribution_required",
                    ),
                )
            )
        return tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.effective_at,
                    item.source_event_id,
                ),
            )
        )

    def _request(self) -> httpx.Response:
        params: dict[str, int | str] = {
            "limit": 0,
            "format": "json",
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(
                    ALTERNATIVE_FEAR_GREED_URL,
                    params=params,
                )
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise NetworkUnavailableError(
                        "Alternative.me was unreachable after bounded retries"
                    ) from error
                self._wait_before_retry(attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise ProviderError(
                        f"Alternative.me returned HTTP "
                        f"{response.status_code} after bounded retries"
                    )
                self._wait_before_retry(attempt)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    f"Alternative.me rejected Fear & Greed request with "
                    f"HTTP {response.status_code}"
                ) from error
            return response
        raise ProviderError(
            "Alternative.me request failed without a response"
        )

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2**attempt)
        if delay:
            time.sleep(delay)

    def _validate_query(self, query: SignalQuery) -> None:
        if query.symbol != _SYMBOL:
            raise ProviderError(
                f"Alternative.me does not support symbol {query.symbol}"
            )
        if _HORIZON not in query.horizons:
            raise ProviderError(
                "Alternative.me requires the 1d signal horizon"
            )
        if query.source_types and _SOURCE_TYPE not in query.source_types:
            raise ProviderError(
                "Alternative.me does not support requested source"
            )


def _timestamp(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProviderError(
            "Alternative.me returned invalid timestamp"
        ) from error
    if str(parsed) != value or parsed < 0:
        raise ProviderError("Alternative.me returned invalid timestamp")
    return parsed


def _value(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ProviderError(
            "Alternative.me returned invalid Fear & Greed value"
        ) from error
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("100"):
        raise ProviderError(
            "Alternative.me returned invalid Fear & Greed value"
        )
    return parsed


def _clip(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(Decimal("1"), value))
