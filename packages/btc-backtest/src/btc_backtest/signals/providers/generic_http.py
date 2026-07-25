"""Declarative, credential-safe generic JSON signal provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.parse import urlparse

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from btc_backtest.errors import NetworkUnavailableError, ProviderError
from btc_backtest.signals.models import (
    IDENTIFIER_PATTERN,
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)

_PATH_PATTERN: Final = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$"
)
_ENV_PATTERN: Final = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_DATETIME_ADAPTER: Final = TypeAdapter(datetime)


class EnvironmentHeader(BaseModel):
    """HTTP header whose value is resolved from the process environment."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    prefix: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise ValueError("header name is invalid")
        return value

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        value = value.strip()
        if not _ENV_PATTERN.fullmatch(value):
            raise ValueError("header environment reference is invalid")
        return value


class GenericJSONFieldMap(BaseModel):
    """Dotted paths from one JSON item to the normalized signal contract."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    source_event_id: str
    source_type: str
    symbol: str
    horizon: str
    direction: str
    confidence: str
    raw_value: str | None = None
    effective_at: str
    observed_at: str
    expires_at: str
    provenance: str

    @field_validator("*")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is not None and not _PATH_PATTERN.fullmatch(value):
            raise ValueError("JSON field path is invalid")
        return value


class JSONProviderConfig(BaseModel):
    """Serializable provider mapping containing references, never secrets."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = Field(min_length=1)
    url: str = Field(min_length=1)
    allowed_hosts: tuple[str, ...] = Field(min_length=1)
    source_types: tuple[str, ...] = Field(min_length=1)
    items_path: str
    next_cursor_path: str | None = None
    cursor_param: str | None = None
    start_param: str | None = None
    end_param: str | None = None
    headers: tuple[EnvironmentHeader, ...] = ()
    fields: GenericJSONFieldMap
    max_pages: int = Field(default=100, ge=1, le=10_000)
    historical: bool = True

    @field_validator("items_path")
    @classmethod
    def validate_items_path(cls, value: str) -> str:
        if not _PATH_PATTERN.fullmatch(value):
            raise ValueError("items path is invalid")
        return value

    @field_validator(
        "next_cursor_path",
        "cursor_param",
        "start_param",
        "end_param",
    )
    @classmethod
    def validate_optional_path_or_parameter(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not _PATH_PATTERN.fullmatch(value):
            raise ValueError("JSON path or parameter is invalid")
        return value

    @field_validator("allowed_hosts", "source_types")
    @classmethod
    def validate_unique_values(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("configured values must be unique")
        return values

    @model_validator(mode="after")
    def validate_security_boundary(self) -> JSONProviderConfig:
        parsed = urlparse(self.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("generic JSON URL must be safe HTTPS")
        allowed = {host.casefold() for host in self.allowed_hosts}
        if parsed.hostname.casefold() not in allowed:
            raise ValueError("generic JSON URL host is not allowlisted")
        if any(
            re.fullmatch(IDENTIFIER_PATTERN, source_type) is None
            for source_type in self.source_types
        ):
            raise ValueError("generic JSON source type is invalid")
        if (self.next_cursor_path is None) != (self.cursor_param is None):
            raise ValueError(
                "cursor path and parameter must be configured together"
            )
        header_names = [header.name.casefold() for header in self.headers]
        if len(set(header_names)) != len(header_names):
            raise ValueError("duplicate environment header")
        return self


class GenericJSONSignalProvider:
    """Fetch normalized signals from one explicitly allowlisted JSON feed."""

    def __init__(
        self,
        config: JSONProviderConfig,
        client: httpx.Client,
        *,
        max_retries: int = 3,
        retry_backoff: float = 0.25,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        self.config = config
        self.client = client
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.metadata = SignalProviderMetadata(
            id=config.id,
            description=config.description,
            source_types=config.source_types,
            historical=config.historical,
            requires_credentials=bool(config.headers),
        )

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        if query.require_historical and not self.config.historical:
            raise ProviderError(
                f"generic JSON provider {self.config.id} is live-only"
            )
        headers = self._resolve_headers()
        base_params: dict[str, str] = {}
        if self.config.start_param is not None:
            base_params[self.config.start_param] = _isoformat(query.start)
        if self.config.end_param is not None:
            base_params[self.config.end_param] = _isoformat(query.end)
        observations: list[SignalObservation] = []
        identities: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None

        for _page in range(self.config.max_pages):
            params = dict(base_params)
            if cursor is not None and self.config.cursor_param is not None:
                params[self.config.cursor_param] = cursor
            response = self._request(headers=headers, params=params)
            payload = _json_object(response)
            items = _items(payload, self.config.items_path)
            for raw in items:
                item = self._map_item(raw, query)
                if item is None:
                    continue
                if item.source_event_id in identities:
                    raise ProviderError(
                        "generic JSON feed returned duplicate event"
                    )
                identities.add(item.source_event_id)
                observations.append(item)
            next_cursor = _next_cursor(
                payload,
                self.config.next_cursor_path,
            )
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise ProviderError("generic JSON pagination cursor stalled")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise ProviderError("generic JSON pagination exceeded max_pages")

        return tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.effective_at,
                    item.source_event_id,
                ),
            )
        )

    def _map_item(
        self,
        raw: dict[str, object],
        query: SignalQuery,
    ) -> SignalObservation | None:
        fields = self.config.fields
        source_event_id = _string(
            _resolve(raw, fields.source_event_id, "source_event_id"),
            "source_event_id",
        )
        source_type = _string(
            _resolve(raw, fields.source_type, "source_type"),
            "source_type",
        )
        if source_type not in self.config.source_types:
            raise ProviderError(
                f"generic JSON source type {source_type} is not allowlisted"
            )
        symbol = _string(
            _resolve(raw, fields.symbol, "symbol"),
            "symbol",
        )
        if symbol != query.symbol:
            raise ProviderError("generic JSON signal symbol does not match query")
        horizon = _string(
            _resolve(raw, fields.horizon, "horizon"),
            "horizon",
        )
        if horizon not in query.horizons:
            return None
        if query.source_types and source_type not in query.source_types:
            return None
        effective_at = _datetime_value(
            _resolve(raw, fields.effective_at, "effective_at"),
            "effective_at",
        )
        if not query.start <= effective_at < query.end:
            raise ProviderError(
                "generic JSON signal is outside query interval"
            )
        observed_at = _datetime_value(
            _resolve(raw, fields.observed_at, "observed_at"),
            "observed_at",
        )
        expires_at = _datetime_value(
            _resolve(raw, fields.expires_at, "expires_at"),
            "expires_at",
        )
        raw_value = (
            None
            if fields.raw_value is None
            else _decimal_value(
                _resolve(raw, fields.raw_value, "raw_value"),
                "raw_value",
            )
        )
        provenance = _string(
            _resolve(raw, fields.provenance, "provenance"),
            "provenance",
        )
        parsed_provenance = urlparse(provenance)
        if parsed_provenance.scheme != "https":
            raise ProviderError(
                "generic JSON provenance must use HTTPS"
            )
        canonical = json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            return SignalObservation(
                id=f"{self.config.id}:{source_event_id}",
                source_event_id=source_event_id,
                provider=self.config.id,
                source_type=source_type,
                symbol=symbol,
                horizon=horizon,
                direction=_decimal_value(
                    _resolve(raw, fields.direction, "direction"),
                    "direction",
                ),
                confidence=_decimal_value(
                    _resolve(raw, fields.confidence, "confidence"),
                    "confidence",
                ),
                raw_value=raw_value,
                effective_at=effective_at,
                observed_at=observed_at,
                expires_at=expires_at,
                provenance=provenance,
                payload_sha256=hashlib.sha256(canonical).hexdigest(),
                quality_flags=("generic_https_json",),
            )
        except ValueError as error:
            raise ProviderError(
                f"generic JSON event {source_event_id} is invalid"
            ) from error

    def _resolve_headers(self) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for header in self.config.headers:
            secret = os.environ.get(header.environment)
            if not secret:
                raise ProviderError(
                    f"missing environment variable {header.environment}"
                )
            resolved[header.name] = f"{header.prefix}{secret}"
        return resolved

    def _request(
        self,
        *,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(
                    self.config.url,
                    headers=headers,
                    params=params,
                )
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise NetworkUnavailableError(
                        f"generic JSON provider {self.config.id} was "
                        "unreachable after bounded retries"
                    ) from error
                self._wait_before_retry(attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise ProviderError(
                        f"generic JSON provider {self.config.id} returned "
                        f"HTTP {response.status_code} after bounded retries"
                    )
                self._wait_before_retry(attempt)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    f"generic JSON provider {self.config.id} rejected "
                    f"request with HTTP {response.status_code}"
                ) from error
            response_host = urlparse(str(response.url)).hostname
            allowed_hosts = {
                host.casefold() for host in self.config.allowed_hosts
            }
            if (
                response_host is None
                or response_host.casefold() not in allowed_hosts
            ):
                raise ProviderError(
                    "generic JSON response escaped the host allowlist"
                )
            return response
        raise ProviderError("generic JSON request failed without a response")

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2**attempt)
        if delay:
            time.sleep(delay)


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        return TypeAdapter(
            dict[str, object]
        ).validate_json(response.content)
    except (ValidationError, ValueError) as error:
        raise ProviderError(
            "generic JSON provider returned malformed JSON"
        ) from error


def _items(
    payload: dict[str, object],
    path: str,
) -> tuple[dict[str, object], ...]:
    raw_items = _resolve(payload, path, "items")
    try:
        items = TypeAdapter(
            list[dict[str, object]]
        ).validate_python(raw_items)
    except ValidationError as error:
        raise ProviderError(
            "generic JSON items path does not contain an object list"
        ) from error
    return tuple(items)


def _next_cursor(
    payload: dict[str, object],
    path: str | None,
) -> str | None:
    if path is None:
        return None
    value = _resolve(payload, path, "next_cursor")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProviderError("generic JSON next cursor is invalid")
    return value


def _resolve(
    value: dict[str, object],
    path: str,
    field: str,
) -> object:
    current: object = value
    for component in path.split("."):
        if not isinstance(current, dict) or component not in current:
            raise ProviderError(
                f"generic JSON field {field} is missing"
            )
        current = current[component]
    return current


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"generic JSON field {field} is invalid")
    return value.strip()


def _decimal_value(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderError(f"generic JSON field {field} is invalid")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ProviderError(
            f"generic JSON field {field} is invalid"
        ) from error
    if not parsed.is_finite():
        raise ProviderError(f"generic JSON field {field} is invalid")
    return parsed


def _datetime_value(value: object, field: str) -> datetime:
    try:
        parsed = _DATETIME_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise ProviderError(
            f"generic JSON field {field} is invalid"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderError(
            f"generic JSON field {field} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
