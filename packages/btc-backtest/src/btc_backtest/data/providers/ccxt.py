"""Optional recent-history adapter for CCXT exchanges."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import cast

import pandas as pd

from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    DataSegment,
    MarketDataset,
    Timeframe,
)
from btc_backtest.data.providers.base import ProviderMetadata
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import (
    DataCoverageError,
    DataValidationError,
    ProviderError,
)

_EXCHANGE_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_TIMEFRAME_MILLISECONDS = {"1h": 3_600_000, "1d": 86_400_000}
_ParsedCandle = tuple[int, float, float, float, float, float]
_FetchOHLCV = Callable[[str, str, int, int], object]


class CCXTProvider:
    """Fetch exchange-dependent recent OHLCV history via CCXT's unified API."""

    def __init__(
        self,
        exchange_id: str,
        exchange: object | None = None,
        *,
        page_size: int = 1_000,
    ) -> None:
        if not _EXCHANGE_ID_RE.fullmatch(exchange_id):
            raise ValueError("exchange_id must contain lowercase letters, digits, _ or -")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.exchange_id = exchange_id
        self.page_size = page_size
        active_exchange = exchange or self._create_exchange(exchange_id)
        capabilities = getattr(active_exchange, "has", None)
        if not isinstance(capabilities, Mapping) or not capabilities.get(
            "fetchOHLCV"
        ):
            raise ProviderError(
                f"CCXT exchange {exchange_id} does not declare fetchOHLCV support"
            )
        timeframes = getattr(active_exchange, "timeframes", None)
        if not isinstance(timeframes, Mapping):
            raise ProviderError(
                f"CCXT exchange {exchange_id} does not declare OHLCV timeframes"
            )
        supported: list[Timeframe] = []
        if "1h" in timeframes:
            supported.append("1h")
        if "1d" in timeframes:
            supported.append("1d")
        if not supported:
            raise ProviderError(
                f"CCXT exchange {exchange_id} supports neither 1h nor 1d OHLCV"
            )
        fetch_method = getattr(active_exchange, "fetch_ohlcv", None)
        if not callable(fetch_method):
            raise ProviderError(
                f"CCXT exchange {exchange_id} has no callable fetchOHLCV implementation"
            )
        self._fetch_ohlcv = cast(_FetchOHLCV, fetch_method)
        self.metadata = ProviderMetadata(
            id=f"ccxt:{exchange_id}",
            real_data=True,
            timeframes=tuple(supported),
            markets=("spot",),
        )

    def fetch(self, request: DataRequest) -> MarketDataset:
        self._validate_request(request)
        step_ms = _TIMEFRAME_MILLISECONDS[request.timeframe]
        cursor = int(request.start.timestamp() * 1_000)
        exclusive_end = int(request.end.timestamp() * 1_000)
        pages: list[pd.DataFrame] = []
        raw_hashes: list[str] = []

        while cursor < exclusive_end:
            try:
                response = self._fetch_ohlcv(
                    request.symbol,
                    request.timeframe,
                    cursor,
                    self.page_size,
                )
            except Exception as error:
                raise ProviderError(
                    f"CCXT exchange {self.exchange_id} fetchOHLCV failed"
                ) from error
            candles = self._parse_page(response)
            if not candles:
                break

            timestamps = [candle[0] for candle in candles]
            if len(set(timestamps)) != len(timestamps):
                raise DataValidationError(
                    "CCXT exchange returned duplicate page timestamps"
                )
            if timestamps != sorted(timestamps):
                raise DataValidationError(
                    "CCXT exchange returned non-ascending page timestamps"
                )
            if timestamps[0] < cursor:
                raise ProviderError("CCXT pagination cursor replayed prior candles")

            raw_hashes.append(
                hashlib.sha256(
                    json.dumps(
                        candles,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            eligible = [
                candle for candle in candles if candle[0] < exclusive_end
            ]
            if eligible:
                pages.append(self._frame_from_candles(eligible))

            last_timestamp = timestamps[-1]
            if last_timestamp >= exclusive_end:
                break
            next_cursor = last_timestamp + step_ms
            if next_cursor <= cursor:
                raise ProviderError("CCXT pagination cursor did not advance")
            cursor = next_cursor

        if not pages:
            raise DataCoverageError(
                f"CCXT exchange {self.exchange_id} returned no requested candles"
            )
        combined = pd.concat(pages)
        if not combined.index.is_unique:
            duplicate = combined.index[combined.index.duplicated()][0]
            raise DataValidationError(
                f"CCXT exchange returned duplicate timestamp {duplicate.isoformat()}"
            )
        normalized, gaps = validate_ohlcv(combined.sort_index(), request)
        fingerprint = frame_fingerprint(normalized)
        delta = timedelta(milliseconds=step_ms)
        delivered_start = normalized.index[0].to_pydatetime()
        delivered_end = (normalized.index[-1] + delta).to_pydatetime()
        source = f"ccxt://{self.exchange_id}"
        segment = DataSegment(
            provider=self.metadata.id,
            market=request.market,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start=delivered_start,
            end=delivered_end,
            real_data=True,
            normalized_sha256=fingerprint,
            source=source,
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
            source=source,
            license_note=(
                "CCXT unified market data; retained history and exchange data "
                "licensing are exchange-dependent."
            ),
            gaps=gaps,
            segments=(segment,),
        )
        return MarketDataset(frame=normalized, manifest=manifest)

    @staticmethod
    def _create_exchange(exchange_id: str) -> object:
        try:
            module = importlib.import_module("ccxt")
        except ModuleNotFoundError as error:
            raise ProviderError(
                "CCXT is optional; install btc-backtest[exchanges]"
            ) from error
        factory_value = getattr(module, exchange_id, None)
        if not callable(factory_value):
            raise ProviderError(f"unknown CCXT exchange: {exchange_id}")
        factory = cast(Callable[[Mapping[str, object]], object], factory_value)
        try:
            return factory({"enableRateLimit": True})
        except Exception as error:
            raise ProviderError(
                f"failed to initialize CCXT exchange {exchange_id}"
            ) from error

    @staticmethod
    def _parse_page(value: object) -> list[_ParsedCandle]:
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
        ):
            raise DataValidationError("CCXT fetchOHLCV response must be a sequence")
        candles: list[_ParsedCandle] = []
        for row in value:
            if (
                not isinstance(row, Sequence)
                or isinstance(row, (str, bytes, bytearray))
                or len(row) < 6
            ):
                raise DataValidationError(
                    "CCXT OHLCV candle must contain timestamp, OHLC, and volume"
                )
            timestamp = _integer(row[0], "timestamp")
            candles.append(
                (
                    timestamp,
                    _number(row[1], "open"),
                    _number(row[2], "high"),
                    _number(row[3], "low"),
                    _number(row[4], "close"),
                    _number(row[5], "volume"),
                )
            )
        return candles

    @staticmethod
    def _frame_from_candles(candles: list[_ParsedCandle]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5],
                }
                for candle in candles
            ],
            index=pd.DatetimeIndex(
                pd.to_datetime(
                    [candle[0] for candle in candles],
                    unit="ms",
                    utc=True,
                ),
                name="timestamp",
            ),
        )

    def _validate_request(self, request: DataRequest) -> None:
        if request.provider != self.metadata.id:
            raise ProviderError(
                f"CCXT provider {self.metadata.id} cannot satisfy provider "
                f"{request.provider}"
            )
        if request.market not in self.metadata.markets:
            raise ProviderError(
                f"CCXT provider {self.metadata.id} does not support market "
                f"{request.market}"
            )
        if request.timeframe not in self.metadata.timeframes:
            raise ProviderError(
                f"CCXT exchange {self.exchange_id} does not support timeframe "
                f"{request.timeframe}"
            )


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DataValidationError(f"CCXT OHLCV {field} must be numeric")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as error:
        raise DataValidationError(f"CCXT OHLCV {field} must be numeric") from error
    if not math.isfinite(numeric):
        raise DataValidationError(f"CCXT OHLCV {field} must be finite")
    return numeric


def _integer(value: object, field: str) -> int:
    numeric = _number(value, field)
    integer = int(numeric)
    if integer != numeric:
        raise DataValidationError(f"CCXT OHLCV {field} must be an integer")
    return integer
