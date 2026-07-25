"""Paginated Bitstamp public OHLC provider for real BTC/USD history."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
from pydantic import BaseModel, ValidationError

from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    DataSegment,
    MarketDataset,
)
from btc_backtest.data.providers.base import ProviderMetadata
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import (
    DataCoverageError,
    DataValidationError,
    NetworkUnavailableError,
    ProviderError,
)

BITSTAMP_OHLC_URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
_TIMEFRAME_SECONDS = {"1h": 3_600, "1d": 86_400}


class _BitstampCandle(BaseModel):
    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: str


class _BitstampData(BaseModel):
    pair: str
    market: str | None = None
    ohlc: tuple[_BitstampCandle, ...]


class _BitstampResponse(BaseModel):
    data: _BitstampData


class BitstampProvider:
    """Fetch bounded, checksummed pages from Bitstamp's public OHLC API."""

    metadata = ProviderMetadata(
        id="bitstamp",
        real_data=True,
        timeframes=("1h", "1d"),
        markets=("spot",),
        symbols=("BTC/USD",),
    )

    def __init__(
        self,
        client: httpx.Client,
        page_size: int = 1_000,
        *,
        max_retries: int = 3,
        retry_backoff: float = 0.25,
    ) -> None:
        if not 1 <= page_size <= 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        self.client = client
        self.page_size = page_size
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def fetch(self, request: DataRequest) -> MarketDataset:
        self._validate_request(request)
        step = _TIMEFRAME_SECONDS[request.timeframe]
        cursor = int(request.start.timestamp())
        exclusive_end = int(request.end.timestamp())
        raw_hashes: list[str] = []
        pages: list[pd.DataFrame] = []

        while cursor < exclusive_end:
            bars_remaining = (exclusive_end - cursor) // step
            page_limit = min(self.page_size, bars_remaining)
            inclusive_page_end = cursor + step * (page_limit - 1)
            response = self._request_page(
                {
                    "step": step,
                    "limit": page_limit,
                    "start": cursor,
                    "exclude_current_candle": "true",
                }
            )
            raw_hashes.append(hashlib.sha256(response.content).hexdigest())
            page = self._parse_page(response, cursor, inclusive_page_end)
            if not page.empty:
                pages.append(page)
            cursor = inclusive_page_end + step

        if not pages:
            raise DataCoverageError("Bitstamp returned no candles for requested interval")
        combined = pd.concat(pages)
        if not combined.index.is_unique:
            duplicate = combined.index[combined.index.duplicated()][0]
            raise DataValidationError(
                f"Bitstamp returned duplicate timestamp {duplicate.isoformat()}"
            )
        normalized, gaps = validate_ohlcv(combined.sort_index(), request)
        fingerprint = frame_fingerprint(normalized)
        delta = timedelta(seconds=step)
        delivered_start = normalized.index[0].to_pydatetime()
        delivered_end = (normalized.index[-1] + delta).to_pydatetime()
        segment = DataSegment(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start=delivered_start,
            end=delivered_end,
            real_data=True,
            normalized_sha256=fingerprint,
            source=BITSTAMP_OHLC_URL,
        )
        manifest = DataManifest(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            requested_start=request.start,
            requested_end=request.end,
            delivered_start=delivered_start,
            delivered_end=delivered_end,
            retrieved_at=datetime.now(timezone.utc),
            real_data=True,
            raw_sha256=tuple(raw_hashes),
            normalized_sha256=fingerprint,
            source=BITSTAMP_OHLC_URL,
            license_note=(
                "Bitstamp public exchange data; commercial use may require "
                "a Bitstamp data license."
            ),
            gaps=gaps,
            segments=(segment,),
        )
        return MarketDataset(frame=normalized, manifest=manifest)

    def _request_page(self, params: dict[str, int | str]) -> httpx.Response:
        transport_error: httpx.TransportError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(BITSTAMP_OHLC_URL, params=params)
            except httpx.TransportError as error:
                transport_error = error
                if attempt == self.max_retries:
                    raise NetworkUnavailableError(
                        "Bitstamp was unreachable after bounded retries"
                    ) from error
                self._wait_before_retry(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise ProviderError(
                        f"Bitstamp returned HTTP {response.status_code} "
                        "after bounded retries"
                    )
                self._wait_before_retry(attempt)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    f"Bitstamp rejected OHLC request with HTTP {response.status_code}"
                ) from error
            return response

        if transport_error is not None:
            raise NetworkUnavailableError("Bitstamp was unreachable") from transport_error
        raise ProviderError("Bitstamp request failed without a response")

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2**attempt)
        if delay:
            time.sleep(delay)

    @staticmethod
    def _parse_page(
        response: httpx.Response,
        cursor: int,
        inclusive_page_end: int,
    ) -> pd.DataFrame:
        try:
            payload = _BitstampResponse.model_validate_json(response.content)
        except (ValidationError, ValueError) as error:
            raise ProviderError("Bitstamp returned malformed OHLC JSON") from error
        if payload.data.pair != "BTC/USD" or payload.data.market not in (
            None,
            "BTC/USD",
        ):
            raise DataValidationError(
                "Bitstamp response pair does not match requested BTC/USD"
            )

        records: list[dict[str, str]] = []
        timestamps: list[datetime] = []
        try:
            for candle in payload.data.ohlc:
                timestamp_value = int(candle.timestamp)
                timestamps.append(
                    datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
                )
                records.append(
                    {
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "volume": candle.volume,
                    }
                )
        except (OverflowError, OSError, ValueError) as error:
            raise DataValidationError(
                "Bitstamp returned an invalid candle timestamp"
            ) from error

        index = pd.DatetimeIndex(timestamps, name="timestamp")
        if not index.is_unique:
            raise DataValidationError("Bitstamp returned duplicate page timestamps")
        if not index.is_monotonic_increasing:
            raise DataValidationError("Bitstamp page timestamps are not ascending")
        if len(index) and int(index[0].timestamp()) < cursor:
            raise ProviderError("Bitstamp pagination cursor did not advance")
        if len(index) and int(index[-1].timestamp()) > inclusive_page_end:
            raise DataValidationError(
                "Bitstamp returned a candle outside the requested page"
            )
        return pd.DataFrame(records, index=index)

    def _validate_request(self, request: DataRequest) -> None:
        if request.provider != self.metadata.id:
            raise ProviderError(
                f"Bitstamp does not support provider {request.provider}"
            )
        if request.market not in self.metadata.markets:
            raise ProviderError(f"Bitstamp does not support market {request.market}")
        if request.symbol not in self.metadata.symbols:
            raise ProviderError(f"Bitstamp does not support symbol {request.symbol}")
        if request.timeframe not in self.metadata.timeframes:
            raise ProviderError(
                f"Bitstamp does not support timeframe {request.timeframe}"
            )
