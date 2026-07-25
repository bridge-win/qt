from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import DataManifest, DataRequest, MarketDataset
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import DataValidationError

UTC = timezone.utc


def request(*, symbol: str = "BTC/USD") -> DataRequest:
    return DataRequest(
        provider="fixture",
        symbol=symbol,
        timeframe="1d",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 4, tzinfo=UTC),
    )


def dataset(data_request: DataRequest | None = None) -> MarketDataset:
    active_request = data_request or request()
    frame, gaps = validate_ohlcv(
        pd.DataFrame(
            {
                "open": [10, 11, 12],
                "high": [12, 13, 14],
                "low": [9, 10, 11],
                "close": [11, 12, 13],
                "volume": [1, 2, 3],
            },
            index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
        ),
        active_request,
    )
    manifest = DataManifest(
        provider=active_request.provider,
        market=active_request.market,
        symbol=active_request.symbol,
        timeframe=active_request.timeframe,
        requested_start=active_request.start,
        requested_end=active_request.end,
        delivered_start=active_request.start,
        delivered_end=active_request.end,
        retrieved_at=datetime(2024, 1, 5, tzinfo=UTC),
        real_data=True,
        raw_sha256=("a" * 64,),
        normalized_sha256=frame_fingerprint(frame),
        gaps=gaps,
    )
    return MarketDataset(frame=frame, manifest=manifest)


def test_publish_round_trips_validated_dataset(tmp_path: Path) -> None:
    data_request = request()
    expected = dataset(data_request)
    cache = DataCache(tmp_path)

    path = cache.publish(data_request, expected)
    loaded = cache.load(data_request)

    assert path.suffix == ".parquet"
    assert loaded is not None
    assert loaded.manifest == expected.manifest
    pd.testing.assert_frame_equal(loaded.frame, expected.frame)


def test_load_returns_none_for_unknown_request(tmp_path: Path) -> None:
    assert DataCache(tmp_path).load(request()) is None


def test_request_identity_isolated_by_canonical_key(tmp_path: Path) -> None:
    btc_request = request()
    cache = DataCache(tmp_path)
    cache.publish(btc_request, dataset(btc_request))

    assert cache.load(request(symbol="ETH/USD")) is None


def test_failed_publication_keeps_previous_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_request = request()
    expected = dataset(data_request)
    cache = DataCache(tmp_path)
    original_path = cache.publish(data_request, expected)

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("disk")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="disk"):
        cache.publish(data_request, expected)

    loaded = cache.load(data_request)
    assert loaded is not None
    assert loaded.manifest == expected.manifest
    assert original_path.exists()


def test_failed_first_publication_leaves_no_visible_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_request = request()
    cache = DataCache(tmp_path)

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("disk")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="disk"):
        cache.publish(data_request, dataset(data_request))

    assert cache.load(data_request) is None
    assert list((tmp_path / "versions").iterdir()) == []


def test_republishing_same_content_preserves_immutable_version(tmp_path: Path) -> None:
    data_request = request()
    expected = dataset(data_request)
    cache = DataCache(tmp_path)
    first = cache.publish(data_request, expected)
    first_inode = first.stat().st_ino

    second = cache.publish(data_request, expected)

    assert second == first
    assert second.stat().st_ino == first_inode


def test_load_rejects_corrupt_cached_data(tmp_path: Path) -> None:
    data_request = request()
    cache = DataCache(tmp_path)
    path = cache.publish(data_request, dataset(data_request))
    corrupted = pd.read_parquet(path)
    corrupted.loc[corrupted.index[1], "close"] = 12.5
    corrupted.to_parquet(path)

    with pytest.raises(DataValidationError, match="fingerprint"):
        cache.load(data_request)


def test_load_wraps_unreadable_parquet_as_validation_error(tmp_path: Path) -> None:
    data_request = request()
    cache = DataCache(tmp_path)
    path = cache.publish(data_request, dataset(data_request))
    path.write_bytes(b"not parquet")

    with pytest.raises(DataValidationError, match="corrupt"):
        cache.load(data_request)


def test_load_rejects_corrupt_pointer(tmp_path: Path) -> None:
    data_request = request()
    cache = DataCache(tmp_path)
    cache.publish(data_request, dataset(data_request))
    pointer = next((tmp_path / "requests").iterdir())
    pointer.write_text('{"version":"outside"}', encoding="utf-8")

    with pytest.raises(DataValidationError, match="corrupt"):
        cache.load(data_request)


def test_publish_rejects_dataset_for_another_request(tmp_path: Path) -> None:
    with pytest.raises(DataValidationError, match="identity"):
        DataCache(tmp_path).publish(request(symbol="ETH/USD"), dataset())
