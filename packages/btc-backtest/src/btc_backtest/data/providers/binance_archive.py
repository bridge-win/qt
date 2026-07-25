"""Checksummed Binance spot kline archive provider."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import httpx
import pandas as pd

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

BINANCE_DATA_ROOT = "https://data.binance.vision/data/"
BINANCE_SPOT_ROOT = f"{BINANCE_DATA_ROOT}spot"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_base_volume",
    "taker_quote_volume",
    "ignore",
)


@dataclass(frozen=True)
class _ArchiveSpec:
    frequency: str
    timeframe: str
    period: str
    url: str
    filename: str


@dataclass(frozen=True)
class _ArchiveChunk:
    frame: pd.DataFrame
    raw_sha256: str
    source: str


class _ArchiveNotFoundError(ProviderError):
    pass


class BinanceArchiveProvider:
    """Download, verify, and normalize Binance's public BTC/USDT archives."""

    metadata = ProviderMetadata(
        id="binance_archive",
        real_data=True,
        timeframes=("1h", "1d"),
        markets=("spot",),
        symbols=("BTC/USDT",),
    )

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_uncompressed_bytes: int = 512 * 1024 * 1024,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_uncompressed_bytes <= 0:
            raise ValueError("max_uncompressed_bytes must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be non-negative")
        self.client = client
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self, request: DataRequest) -> MarketDataset:
        self._validate_request(request)
        specs = self._select_archives(request)
        downloaded: list[_ArchiveChunk] = []
        for spec in specs:
            try:
                downloaded.append(self._fetch_archive(spec, request))
            except _ArchiveNotFoundError as error:
                if spec.frequency != "monthly":
                    raise ProviderError(
                        f"Binance archive is not published: {spec.filename}"
                    ) from error
                month = datetime.strptime(spec.period, "%Y-%m").replace(
                    tzinfo=timezone.utc
                )
                daily_specs = self._daily_specs_for_month(
                    request,
                    month,
                )
                for daily_spec in daily_specs:
                    try:
                        downloaded.append(
                            self._fetch_archive(daily_spec, request)
                        )
                    except _ArchiveNotFoundError as daily_error:
                        raise ProviderError(
                            "Binance monthly archive is unavailable and a required "
                            f"daily fallback is missing: {daily_spec.filename}"
                        ) from daily_error
        chunks = tuple(downloaded)
        frames = [chunk.frame for chunk in chunks if not chunk.frame.empty]
        if not frames:
            raise DataCoverageError(
                "Binance archives contained no candles for requested interval"
            )

        combined = pd.concat(frames)
        if not combined.index.is_unique:
            duplicate = combined.index[combined.index.duplicated()][0]
            raise DataValidationError(
                f"Binance archives contain duplicate timestamp {duplicate.isoformat()}"
            )
        normalized, gaps = validate_ohlcv(combined.sort_index(), request)
        fingerprint = frame_fingerprint(normalized)
        delta = (
            timedelta(hours=1)
            if request.timeframe == "1h"
            else timedelta(days=1)
        )
        segments = tuple(
            self._segment_for_chunk(chunk, request, delta)
            for chunk in chunks
            if not chunk.frame.empty
        )
        manifest = DataManifest(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            requested_start=request.start,
            requested_end=request.end,
            delivered_start=normalized.index[0].to_pydatetime(),
            delivered_end=(normalized.index[-1] + delta).to_pydatetime(),
            retrieved_at=datetime.now(timezone.utc),
            real_data=True,
            raw_sha256=tuple(chunk.raw_sha256 for chunk in chunks),
            normalized_sha256=fingerprint,
            source=BINANCE_SPOT_ROOT,
            license_note=(
                "Binance public data archive; repository tooling is MIT licensed "
                "and exchange data terms may also apply."
            ),
            gaps=gaps,
            segments=segments,
        )
        return MarketDataset(frame=normalized, manifest=manifest)

    def _fetch_archive(
        self,
        spec: _ArchiveSpec,
        request: DataRequest,
    ) -> _ArchiveChunk:
        self._require_allowlisted_url(spec.url)
        archive_response = self._download(spec.url)
        checksum_response = self._download(f"{spec.url}.CHECKSUM")
        digest = hashlib.sha256(archive_response.content).hexdigest()
        expected = self._parse_checksum(checksum_response.text, spec.filename)
        if digest != expected:
            raise ProviderError(
                f"Binance archive checksum mismatch for {spec.filename}"
            )

        frame = self._read_archive(archive_response.content, spec.filename)
        start = pd.Timestamp(request.start)
        end = pd.Timestamp(request.end)
        frame = frame.loc[(frame.index >= start) & (frame.index < end)]
        return _ArchiveChunk(frame=frame, raw_sha256=digest, source=spec.url)

    def _download(self, url: str) -> httpx.Response:
        self._require_allowlisted_url(url.removesuffix(".CHECKSUM"))
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(url)
            except httpx.TransportError as error:
                if attempt == self.max_retries:
                    raise NetworkUnavailableError(
                        "Binance public archives were unreachable after bounded retries"
                    ) from error
                self._wait_before_retry(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_retries:
                    raise ProviderError(
                        f"Binance archive returned HTTP {response.status_code} "
                        "after bounded retries"
                    )
                self._wait_before_retry(attempt)
                continue
            if response.status_code == 404:
                raise _ArchiveNotFoundError(f"Binance archive was not found: {url}")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ProviderError(
                    f"Binance archive request failed with HTTP {response.status_code}"
                ) from error
            return response
        raise ProviderError("Binance archive request failed without a response")

    def _read_archive(self, payload: bytes, filename: str) -> pd.DataFrame:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                files = [member for member in archive.infolist() if not member.is_dir()]
                if len(files) != 1:
                    raise DataValidationError(
                        "Binance archive must contain exactly one CSV file"
                    )
                member = files[0]
                member_path = PurePosixPath(member.filename)
                expected_csv = f"{Path(filename).stem}.csv"
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or len(member_path.parts) != 1
                    or member_path.name != expected_csv
                ):
                    raise DataValidationError(
                        f"Binance archive contains unsafe member {member.filename}"
                    )
                if member.file_size > self.max_uncompressed_bytes:
                    raise DataValidationError(
                        "Binance archive uncompressed payload exceeds configured limit"
                    )
                raw_csv = archive.read(member)
        except zipfile.BadZipFile as error:
            raise DataValidationError("Binance archive is not a valid ZIP file") from error
        if len(raw_csv) > self.max_uncompressed_bytes:
            raise DataValidationError(
                "Binance archive uncompressed payload exceeds configured limit"
            )
        return self._parse_csv(raw_csv)

    @staticmethod
    def _parse_csv(payload: bytes) -> pd.DataFrame:
        try:
            rows = [
                row
                for row in csv.reader(io.StringIO(payload.decode("utf-8")))
                if row
            ]
        except UnicodeDecodeError as error:
            raise DataValidationError("Binance kline CSV is not UTF-8") from error
        if not rows:
            raise DataValidationError("Binance kline CSV is empty")
        if any(len(row) != len(_KLINE_COLUMNS) for row in rows):
            raise DataValidationError("Binance kline CSV must contain exactly 12 columns")

        timestamps: list[pd.Timestamp] = []
        records: list[dict[str, str]] = []
        try:
            for row in rows:
                raw_timestamp = int(row[0])
                if raw_timestamp >= 1_000_000_000_000_000:
                    timestamp = pd.to_datetime(raw_timestamp, unit="us", utc=True)
                elif raw_timestamp >= 1_000_000_000_000:
                    timestamp = pd.to_datetime(raw_timestamp, unit="ms", utc=True)
                else:
                    raise ValueError("unsupported timestamp unit")
                timestamps.append(timestamp)
                records.append(
                    {
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "volume": row[5],
                    }
                )
        except (OverflowError, TypeError, ValueError) as error:
            raise DataValidationError(
                "Binance kline CSV contains an invalid timestamp"
            ) from error
        return pd.DataFrame(
            records,
            index=pd.DatetimeIndex(timestamps, name="timestamp"),
        )

    @staticmethod
    def _parse_checksum(payload: str, filename: str) -> str:
        parts = payload.strip().split()
        if len(parts) != 2:
            raise ProviderError("Binance checksum file is malformed")
        digest, declared_filename = parts
        if not _SHA256_RE.fullmatch(digest):
            raise ProviderError("Binance checksum file contains an invalid SHA-256")
        if declared_filename.lstrip("*") != filename:
            raise ProviderError("Binance checksum filename does not match archive")
        return digest

    def _select_archives(self, request: DataRequest) -> tuple[_ArchiveSpec, ...]:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now() must return a timezone-aware datetime")
        current_month = now.astimezone(timezone.utc).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        month = request.start.replace(day=1)
        last_inclusive = request.end - timedelta(microseconds=1)
        specs: list[_ArchiveSpec] = []
        while month <= last_inclusive.replace(day=1):
            if month < current_month:
                period = f"{month.year:04d}-{month.month:02d}"
                specs.append(self._archive_spec("monthly", request.timeframe, period))
            elif month == current_month:
                first_day = max(request.start.date(), month.date())
                final_day = min(last_inclusive.date(), now.astimezone(timezone.utc).date())
                day = first_day
                while day <= final_day:
                    specs.append(
                        self._archive_spec(
                            "daily",
                            request.timeframe,
                            day.isoformat(),
                        )
                    )
                    day += timedelta(days=1)
            else:
                raise ProviderError("Binance archive request extends into the future")
            month = _next_month(month)
        return tuple(specs)

    @staticmethod
    def _archive_spec(
        frequency: str,
        timeframe: str,
        period: str,
    ) -> _ArchiveSpec:
        filename = f"BTCUSDT-{timeframe}-{period}.zip"
        url = (
            f"{BINANCE_SPOT_ROOT}/{frequency}/klines/"
            f"BTCUSDT/{timeframe}/{filename}"
        )
        return _ArchiveSpec(
            frequency=frequency,
            timeframe=timeframe,
            period=period,
            url=url,
            filename=filename,
        )

    def _daily_specs_for_month(
        self,
        request: DataRequest,
        month: datetime,
    ) -> tuple[_ArchiveSpec, ...]:
        first_day = max(request.start.date(), month.date())
        last_inclusive = request.end - timedelta(microseconds=1)
        final_day = min(
            last_inclusive.date(),
            (_next_month(month) - timedelta(days=1)).date(),
        )
        specs: list[_ArchiveSpec] = []
        day = first_day
        while day <= final_day:
            specs.append(
                self._archive_spec(
                    "daily",
                    request.timeframe,
                    day.isoformat(),
                )
            )
            day += timedelta(days=1)
        return tuple(specs)

    @staticmethod
    def _require_allowlisted_url(url: str) -> None:
        if not url.startswith(f"{BINANCE_SPOT_ROOT}/"):
            raise ProviderError("Binance archive URL is outside the allowlisted root")
        if "\\" in url or ".." in url:
            raise ProviderError("Binance archive URL contains an unsafe path")

    @staticmethod
    def _segment_for_chunk(
        chunk: _ArchiveChunk,
        request: DataRequest,
        delta: timedelta,
    ) -> DataSegment:
        permissive_request = request.model_copy(
            update={"require_complete": False, "max_missing_ratio": 1.0}
        )
        normalized, _ = validate_ohlcv(chunk.frame, permissive_request)
        return DataSegment(
            provider=BinanceArchiveProvider.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start=normalized.index[0].to_pydatetime(),
            end=(normalized.index[-1] + delta).to_pydatetime(),
            real_data=True,
            normalized_sha256=frame_fingerprint(normalized),
            source=chunk.source,
        )

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2**attempt)
        if delay:
            time.sleep(delay)

    def _validate_request(self, request: DataRequest) -> None:
        if request.provider != self.metadata.id:
            raise ProviderError(
                f"Binance archives do not support provider {request.provider}"
            )
        if request.market not in self.metadata.markets:
            raise ProviderError(
                f"Binance archives do not support market {request.market}"
            )
        if request.symbol not in self.metadata.symbols:
            raise ProviderError(
                f"Binance archives do not support symbol {request.symbol}"
            )
        if request.timeframe not in self.metadata.timeframes:
            raise ProviderError(
                f"Binance archives do not support timeframe {request.timeframe}"
            )


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)
