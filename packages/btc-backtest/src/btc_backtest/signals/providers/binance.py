"""Public Binance USD-M derivatives signal normalization."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Final, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from btc_backtest.errors import NetworkUnavailableError, ProviderError
from btc_backtest.signals.models import (
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)

BINANCE_FUNDING_URL: Final = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_OPEN_INTEREST_URL: Final = (
    "https://fapi.binance.com/futures/data/openInterestHist"
)
BINANCE_LONG_SHORT_URL: Final = (
    "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
)
BINANCE_TAKER_FLOW_URL: Final = (
    "https://fapi.binance.com/futures/data/takerlongshortRatio"
)
_PROVIDER_ID: Final = "binance_derivatives"
_BINANCE_SYMBOL: Final = "BTCUSDT"
_OUTPUT_SYMBOL: Final = "BTC/USD"
_HORIZON: Final = "1d"
_DAY: Final = timedelta(days=1)
_FUNDING_SCALE: Final = Decimal("0.0005")
_OPEN_INTEREST_CHANGE_SCALE: Final = Decimal("0.10")


@dataclass(frozen=True)
class BinanceSignalRule:
    """Auditable endpoint and normalization declaration."""

    source_type: str
    url: str
    timestamp_field: str
    native_period: str
    normalization: str
    direction_rule: str


BINANCE_SIGNAL_RULES: Final = {
    "funding": BinanceSignalRule(
        source_type="funding",
        url=BINANCE_FUNDING_URL,
        timestamp_field="fundingTime",
        native_period="8h",
        normalization="rate / 0.0005",
        direction_rule="contrarian",
    ),
    "open_interest": BinanceSignalRule(
        source_type="open_interest",
        url=BINANCE_OPEN_INTEREST_URL,
        timestamp_field="timestamp",
        native_period="1h",
        normalization="period change / 0.10",
        direction_rule="trend",
    ),
    "long_short_ratio": BinanceSignalRule(
        source_type="long_short_ratio",
        url=BINANCE_LONG_SHORT_URL,
        timestamp_field="timestamp",
        native_period="1h",
        normalization="(1 - ratio) / (1 + ratio)",
        direction_rule="contrarian",
    ),
    "taker_flow": BinanceSignalRule(
        source_type="taker_flow",
        url=BINANCE_TAKER_FLOW_URL,
        timestamp_field="timestamp",
        native_period="1h",
        normalization="(ratio - 1) / (ratio + 1)",
        direction_rule="trend",
    ),
}


class _FundingRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    funding_time: int = Field(alias="fundingTime")
    funding_rate: str = Field(alias="fundingRate")


class _OpenInterestRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    sum_open_interest: str = Field(alias="sumOpenInterest")
    sum_open_interest_value: str = Field(alias="sumOpenInterestValue")
    timestamp: int


class _LongShortRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    long_short_ratio: str = Field(alias="longShortRatio")
    long_account: str = Field(alias="longAccount")
    short_account: str = Field(alias="shortAccount")
    timestamp: int


class _TakerFlowRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    buy_sell_ratio: str = Field(alias="buySellRatio")
    buy_volume: str = Field(alias="buyVol")
    sell_volume: str = Field(alias="sellVol")
    timestamp: int


RecordT = TypeVar("RecordT", bound=BaseModel)


class BinanceDerivativesSignalProvider:
    """Fetch and normalize Binance public historical derivative statistics."""

    metadata = SignalProviderMetadata(
        id=_PROVIDER_ID,
        description=(
            "Binance USD-M public funding, open-interest, positioning, "
            "and taker-flow history"
        ),
        source_types=tuple(BINANCE_SIGNAL_RULES),
        historical=True,
        requires_credentials=False,
    )

    def __init__(
        self,
        client: httpx.Client,
        page_size: int = 500,
        *,
        max_retries: int = 3,
        retry_backoff: float = 0.25,
    ) -> None:
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        self.client = client
        self.page_size = page_size
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        self._validate_query(query)
        selected = (
            query.source_types
            if query.source_types
            else self.metadata.source_types
        )
        observations: list[SignalObservation] = []
        for source_type in selected:
            if source_type == "funding":
                observations.extend(self._fetch_funding(query))
            elif source_type == "open_interest":
                observations.extend(self._fetch_open_interest(query))
            elif source_type == "long_short_ratio":
                observations.extend(self._fetch_long_short(query))
            elif source_type == "taker_flow":
                observations.extend(self._fetch_taker_flow(query))
            else:
                raise ProviderError(
                    f"Binance derivatives does not support source {source_type}"
                )
        return tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.effective_at,
                    item.source_type,
                    item.source_event_id,
                ),
            )
        )

    def _fetch_funding(
        self,
        query: SignalQuery,
    ) -> tuple[SignalObservation, ...]:
        rule = BINANCE_SIGNAL_RULES["funding"]
        records = self._fetch_records(query, rule, _FundingRecord)
        return tuple(
            self._observation(
                query=query,
                rule=rule,
                raw=raw,
                timestamp_ms=record.funding_time,
                raw_value=_decimal(
                    record.funding_rate,
                    "fundingRate",
                ),
                direction=_clip(
                    -_decimal(record.funding_rate, "fundingRate")
                    / _FUNDING_SCALE
                ),
                flags=("historical", "contrarian", "scale_0_0005"),
            )
            for record, raw in records
        )

    def _fetch_open_interest(
        self,
        query: SignalQuery,
    ) -> tuple[SignalObservation, ...]:
        rule = BINANCE_SIGNAL_RULES["open_interest"]
        records = self._fetch_records(query, rule, _OpenInterestRecord)
        observations: list[SignalObservation] = []
        previous: Decimal | None = None
        for record, raw in records:
            value = _decimal(
                record.sum_open_interest_value,
                "sumOpenInterestValue",
                minimum=Decimal("0"),
            )
            flags: tuple[str, ...] = (
                "historical",
                "trend",
                "scale_0_10_change",
            )
            if previous is None or previous == 0:
                direction = Decimal("0")
                flags += ("insufficient_baseline",)
            else:
                direction = _clip(
                    ((value - previous) / previous)
                    / _OPEN_INTEREST_CHANGE_SCALE
                )
            observations.append(
                self._observation(
                    query=query,
                    rule=rule,
                    raw=raw,
                    timestamp_ms=record.timestamp,
                    raw_value=value,
                    direction=direction,
                    flags=flags,
                )
            )
            previous = value
        return tuple(observations)

    def _fetch_long_short(
        self,
        query: SignalQuery,
    ) -> tuple[SignalObservation, ...]:
        rule = BINANCE_SIGNAL_RULES["long_short_ratio"]
        records = self._fetch_records(query, rule, _LongShortRecord)
        observations: list[SignalObservation] = []
        for record, raw in records:
            ratio = _decimal(
                record.long_short_ratio,
                "longShortRatio",
                minimum=Decimal("0"),
            )
            direction = _ratio_direction(ratio, contrarian=True)
            observations.append(
                self._observation(
                    query=query,
                    rule=rule,
                    raw=raw,
                    timestamp_ms=record.timestamp,
                    raw_value=ratio,
                    direction=direction,
                    flags=(
                        "historical",
                        "contrarian",
                        "ratio_centered",
                    ),
                )
            )
        return tuple(observations)

    def _fetch_taker_flow(
        self,
        query: SignalQuery,
    ) -> tuple[SignalObservation, ...]:
        rule = BINANCE_SIGNAL_RULES["taker_flow"]
        records = self._fetch_records(query, rule, _TakerFlowRecord)
        observations: list[SignalObservation] = []
        for record, raw in records:
            ratio = _decimal(
                record.buy_sell_ratio,
                "buySellRatio",
                minimum=Decimal("0"),
            )
            direction = _ratio_direction(ratio, contrarian=False)
            observations.append(
                self._observation(
                    query=query,
                    rule=rule,
                    raw=raw,
                    timestamp_ms=record.timestamp,
                    raw_value=ratio,
                    direction=direction,
                    flags=("historical", "trend", "ratio_centered"),
                )
            )
        return tuple(observations)

    def _fetch_records(
        self,
        query: SignalQuery,
        rule: BinanceSignalRule,
        record_type: type[RecordT],
    ) -> list[tuple[RecordT, dict[str, object]]]:
        cursor = _to_milliseconds(query.start)
        exclusive_end = _to_milliseconds(query.end)
        records: list[tuple[RecordT, dict[str, object]]] = []
        seen_timestamps: set[int] = set()
        while cursor < exclusive_end:
            params: dict[str, int | str] = {
                "symbol": _BINANCE_SYMBOL,
                "startTime": cursor,
                "endTime": exclusive_end - 1,
                "limit": self.page_size,
            }
            if rule.source_type != "funding":
                params["period"] = rule.native_period
            response = self._request_page(rule, params)
            try:
                raw_page = TypeAdapter(
                    list[dict[str, object]]
                ).validate_json(response.content)
            except (ValidationError, ValueError) as error:
                raise ProviderError(
                    f"Binance returned malformed {rule.source_type} payload"
                ) from error
            if len(raw_page) > self.page_size:
                raise ProviderError(
                    f"Binance {rule.source_type} page exceeds requested limit"
                )
            if not raw_page:
                break

            page_timestamps: list[int] = []
            for raw in raw_page:
                timestamp_ms = _raw_timestamp(raw, rule.timestamp_field)
                if not cursor <= timestamp_ms < exclusive_end:
                    raise ProviderError(
                        f"Binance {rule.source_type} timestamp is outside query"
                    )
                if timestamp_ms in seen_timestamps:
                    raise ProviderError(
                        f"Binance {rule.source_type} returned duplicate timestamp"
                    )
                try:
                    record = record_type.model_validate(raw)
                except ValidationError as error:
                    raise ProviderError(
                        f"Binance returned malformed {rule.source_type} payload"
                    ) from error
                self._validate_record_symbol(record, rule.source_type)
                page_timestamps.append(timestamp_ms)
                seen_timestamps.add(timestamp_ms)
                records.append((record, raw))

            if page_timestamps != sorted(page_timestamps):
                raise ProviderError(
                    f"Binance {rule.source_type} timestamps are not ascending"
                )
            if len(raw_page) < self.page_size:
                break
            next_cursor = page_timestamps[-1] + 1
            if next_cursor <= cursor:
                raise ProviderError(
                    f"Binance {rule.source_type} pagination cursor stalled"
                )
            cursor = next_cursor
        return records

    def _request_page(
        self,
        rule: BinanceSignalRule,
        params: dict[str, int | str],
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(rule.url, params=params)
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise NetworkUnavailableError(
                        "Binance derivatives was unreachable after "
                        "bounded retries"
                    ) from error
                self._wait_before_retry(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise ProviderError(
                        f"Binance returned HTTP {response.status_code} "
                        "after bounded retries"
                    )
                self._wait_before_retry(attempt)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    f"Binance rejected {rule.source_type} request with "
                    f"HTTP {response.status_code}"
                ) from error
            return response
        raise ProviderError("Binance request failed without a response")

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2**attempt)
        if delay:
            time.sleep(delay)

    @staticmethod
    def _validate_record_symbol(
        record: BaseModel,
        source_type: str,
    ) -> None:
        symbol = getattr(record, "symbol", _BINANCE_SYMBOL)
        if symbol != _BINANCE_SYMBOL:
            raise ProviderError(
                f"Binance {source_type} response symbol does not match BTCUSDT"
            )

    @staticmethod
    def _observation(
        *,
        query: SignalQuery,
        rule: BinanceSignalRule,
        raw: dict[str, object],
        timestamp_ms: int,
        raw_value: Decimal,
        direction: Decimal,
        flags: tuple[str, ...],
    ) -> SignalObservation:
        observed_at = _from_milliseconds(timestamp_ms)
        canonical_payload = json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        event_key = f"{rule.source_type}:{_BINANCE_SYMBOL}:{timestamp_ms}"
        return SignalObservation(
            id=f"{_PROVIDER_ID}:{event_key}",
            source_event_id=event_key,
            provider=_PROVIDER_ID,
            source_type=rule.source_type,
            symbol=query.symbol,
            horizon=_HORIZON,
            direction=direction,
            confidence=abs(direction),
            raw_value=raw_value,
            observed_at=observed_at,
            effective_at=observed_at,
            expires_at=observed_at + _DAY,
            provenance=f"{rule.url}#{event_key}",
            payload_sha256=hashlib.sha256(canonical_payload).hexdigest(),
            quality_flags=(*flags, f"native_{rule.native_period}"),
        )

    def _validate_query(self, query: SignalQuery) -> None:
        if query.symbol != _OUTPUT_SYMBOL:
            raise ProviderError(
                f"Binance derivatives does not support symbol {query.symbol}"
            )
        if _HORIZON not in query.horizons:
            raise ProviderError(
                "Binance derivatives requires the 1d signal horizon"
            )
        unsupported = set(query.source_types).difference(
            self.metadata.source_types
        )
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ProviderError(
                f"Binance derivatives does not support source {names}"
            )


def _decimal(
    value: str,
    field: str,
    *,
    minimum: Decimal | None = None,
) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ProviderError(f"Binance returned invalid {field}") from error
    if not parsed.is_finite():
        raise ProviderError(f"Binance returned invalid {field}")
    if minimum is not None and parsed < minimum:
        raise ProviderError(f"Binance returned invalid {field}")
    return parsed


def _clip(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(Decimal("1"), value))


def _ratio_direction(
    ratio: Decimal,
    *,
    contrarian: bool,
) -> Decimal:
    denominator = Decimal("1") + ratio
    if denominator == 0:
        raise ProviderError("Binance returned invalid ratio")
    numerator = (
        Decimal("1") - ratio
        if contrarian
        else ratio - Decimal("1")
    )
    return _clip(numerator / denominator)


def _raw_timestamp(raw: dict[str, object], field: str) -> int:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError(f"Binance returned invalid {field}")
    return value


def _to_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def _from_milliseconds(value: int) -> datetime:
    seconds, milliseconds = divmod(value, 1_000)
    return datetime.fromtimestamp(
        seconds,
        tz=timezone.utc,
    ) + timedelta(milliseconds=milliseconds)
