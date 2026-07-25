from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
from btc_backtest.data.models import DataRequest, Timeframe
from btc_backtest.data.providers.binance_archive import BinanceArchiveProvider
from btc_backtest.errors import (
    DataValidationError,
    NetworkUnavailableError,
    ProviderError,
)
from pytest_httpx import HTTPXMock

UTC = timezone.utc
BASE_URL = "https://data.binance.vision/data/spot"


def archive_name(timeframe: str, period: str) -> str:
    return f"BTCUSDT-{timeframe}-{period}.zip"


def archive_url(timeframe: str, period: str, *, frequency: str = "monthly") -> str:
    name = archive_name(timeframe, period)
    return f"{BASE_URL}/{frequency}/klines/BTCUSDT/{timeframe}/{name}"


def make_kline_zip(
    *timestamps: int,
    filename: str = "BTCUSDT-1d-2025-01.csv",
    extra_columns: int = 0,
) -> bytes:
    rows = []
    for index, timestamp in enumerate(timestamps or (1_735_689_600_000_000,)):
        row = [
            timestamp,
            f"{100 + index}.0",
            f"{102 + index}.0",
            f"{99 + index}.0",
            f"{101 + index}.0",
            f"{10 + index}.0",
            timestamp + 1,
            "0",
            "1",
            "0",
            "0",
            "0",
        ]
        row.extend("extra" for _ in range(extra_columns))
        rows.append(",".join(str(value) for value in row))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, "\n".join(rows) + "\n")
    return buffer.getvalue()


def add_archive(
    httpx_mock: HTTPXMock,
    *,
    timeframe: str,
    period: str,
    content: bytes,
    frequency: str = "monthly",
    checksum: str | None = None,
) -> None:
    url = archive_url(timeframe, period, frequency=frequency)
    name = archive_name(timeframe, period)
    httpx_mock.add_response(url=url, content=content)
    httpx_mock.add_response(
        url=f"{url}.CHECKSUM",
        text=f"{checksum or hashlib.sha256(content).hexdigest()}  {name}\n",
    )


def request(
    start: datetime,
    end: datetime,
    *,
    timeframe: Timeframe = "1d",
) -> DataRequest:
    return DataRequest(
        provider="binance_archive",
        market="spot",
        symbol="BTC/USDT",
        timeframe=timeframe,
        start=start,
        end=end,
    )


@pytest.mark.parametrize(
    ("timestamp", "start", "end", "expected"),
    [
        (
            1_735_689_600_000_000,
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 2, tzinfo=UTC),
            pd.Timestamp("2025-01-01", tz="UTC"),
        ),
        (
            1_704_067_200_000,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
            pd.Timestamp("2024-01-01", tz="UTC"),
        ),
    ],
)
def test_archive_verifies_checksum_and_normalizes_timestamp_units(
    httpx_mock: HTTPXMock,
    timestamp: int,
    start: datetime,
    end: datetime,
    expected: pd.Timestamp,
) -> None:
    period = f"{start.year:04d}-{start.month:02d}"
    content = make_kline_zip(
        timestamp,
        filename=f"BTCUSDT-1d-{period}.csv",
    )
    add_archive(
        httpx_mock,
        timeframe="1d",
        period=period,
        content=content,
    )

    result = BinanceArchiveProvider(httpx.Client()).fetch(
        request(start, end)
    )

    assert result.frame.index[0] == expected
    assert result.manifest.raw_sha256 == (
        hashlib.sha256(content).hexdigest(),
    )
    assert result.manifest.real_data is True


def test_archive_rejects_checksum_mismatch(httpx_mock: HTTPXMock) -> None:
    content = make_kline_zip(
        1_704_067_200_000,
        filename="BTCUSDT-1d-2024-01.csv",
    )
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2024-01",
        content=content,
        checksum="0" * 64,
    )

    with pytest.raises(ProviderError, match="checksum"):
        BinanceArchiveProvider(httpx.Client()).fetch(
            request(
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 1, 2, tzinfo=UTC),
            )
        )


def test_archive_supports_hourly_kline_files(httpx_mock: HTTPXMock) -> None:
    content = make_kline_zip(
        1_735_689_600_000_000,
        1_735_693_200_000_000,
        filename="BTCUSDT-1h-2025-01.csv",
    )
    add_archive(
        httpx_mock,
        timeframe="1h",
        period="2025-01",
        content=content,
    )

    result = BinanceArchiveProvider(httpx.Client()).fetch(
        request(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, 2, tzinfo=UTC),
            timeframe="1h",
        )
    )

    assert result.frame.index.tolist() == list(
        pd.date_range("2025-01-01", periods=2, freq="1h", tz="UTC")
    )
    assert str(httpx_mock.get_requests()[0].url) == archive_url(
        "1h",
        "2025-01",
    )


def test_archive_bounds_transport_retries() -> None:
    attempts = 0

    def fail_transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(fail_transport)) as client,
        pytest.raises(NetworkUnavailableError, match="unreachable"),
    ):
        BinanceArchiveProvider(
            client,
            max_retries=2,
            retry_backoff=0.0,
        ).fetch(
            request(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

    assert attempts == 3


def test_archive_uses_monthly_then_current_month_daily_files(
    httpx_mock: HTTPXMock,
) -> None:
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-01",
        content=make_kline_zip(
            1_738_281_600_000_000,
            filename="BTCUSDT-1d-2025-01.csv",
        ),
    )
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-02-01",
        frequency="daily",
        content=make_kline_zip(
            1_738_368_000_000_000,
            filename="BTCUSDT-1d-2025-02-01.csv",
        ),
    )
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-02-02",
        frequency="daily",
        content=make_kline_zip(
            1_738_454_400_000_000,
            filename="BTCUSDT-1d-2025-02-02.csv",
        ),
    )

    result = BinanceArchiveProvider(
        httpx.Client(),
        now=lambda: datetime(2025, 2, 3, tzinfo=UTC),
    ).fetch(
        request(
            datetime(2025, 1, 31, tzinfo=UTC),
            datetime(2025, 2, 3, tzinfo=UTC),
        )
    )

    assert len(result.frame) == 3
    requested_urls = [str(item.url) for item in httpx_mock.get_requests()]
    assert archive_url("1d", "2025-01") in requested_urls
    assert archive_url("1d", "2025-02-01", frequency="daily") in requested_urls
    assert archive_url("1d", "2025-02-02", frequency="daily") in requested_urls


def test_archive_falls_back_to_daily_when_monthly_file_is_not_published(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=archive_url("1d", "2025-01"),
        status_code=404,
    )
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-01-31",
        frequency="daily",
        content=make_kline_zip(
            1_738_281_600_000_000,
            filename="BTCUSDT-1d-2025-01-31.csv",
        ),
    )

    result = BinanceArchiveProvider(
        httpx.Client(),
        now=lambda: datetime(2025, 2, 2, tzinfo=UTC),
    ).fetch(
        request(
            datetime(2025, 1, 31, tzinfo=UTC),
            datetime(2025, 2, 1, tzinfo=UTC),
        )
    )

    assert len(result.frame) == 1
    assert len(result.manifest.raw_sha256) == 1


def test_archive_rejects_zip_slip_member(httpx_mock: HTTPXMock) -> None:
    content = make_kline_zip(filename="../outside.csv")
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-01",
        content=content,
    )

    with pytest.raises(DataValidationError, match="unsafe"):
        BinanceArchiveProvider(httpx.Client()).fetch(
            request(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            )
        )


def test_archive_rejects_invalid_zip(httpx_mock: HTTPXMock) -> None:
    content = b"not a zip"
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-01",
        content=content,
    )

    with pytest.raises(DataValidationError, match="valid ZIP"):
        BinanceArchiveProvider(httpx.Client()).fetch(
            request(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            )
        )


def test_archive_rejects_uncompressed_payload_over_limit(
    httpx_mock: HTTPXMock,
) -> None:
    content = make_kline_zip()
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-01",
        content=content,
    )

    with pytest.raises(DataValidationError, match="uncompressed"):
        BinanceArchiveProvider(
            httpx.Client(),
            max_uncompressed_bytes=10,
        ).fetch(
            request(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            )
        )


def test_archive_requires_exact_kline_column_count(
    httpx_mock: HTTPXMock,
) -> None:
    content = make_kline_zip(extra_columns=1)
    add_archive(
        httpx_mock,
        timeframe="1d",
        period="2025-01",
        content=content,
    )

    with pytest.raises(DataValidationError, match="12 columns"):
        BinanceArchiveProvider(httpx.Client()).fetch(
            request(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            )
        )


def test_archive_rejects_checksum_for_another_filename(
    httpx_mock: HTTPXMock,
) -> None:
    content = make_kline_zip()
    url = archive_url("1d", "2025-01")
    httpx_mock.add_response(url=url, content=content)
    httpx_mock.add_response(
        url=f"{url}.CHECKSUM",
        text=f"{hashlib.sha256(content).hexdigest()}  other.zip\n",
    )

    with pytest.raises(ProviderError, match="filename"):
        BinanceArchiveProvider(httpx.Client()).fetch(
            request(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 2, tzinfo=UTC),
            )
        )


def test_archive_rejects_non_usdt_identity() -> None:
    data_request = DataRequest(
        provider="binance_archive",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 2, tzinfo=UTC),
    )

    with pytest.raises(ProviderError, match="support"):
        BinanceArchiveProvider(httpx.Client()).fetch(data_request)
