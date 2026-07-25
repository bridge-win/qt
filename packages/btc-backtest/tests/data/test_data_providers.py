from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from btc_backtest.data.cache import DataCache
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketDataset,
    Timeframe,
)
from btc_backtest.data.providers.base import ProviderMetadata, ProviderRegistry
from btc_backtest.data.providers.composite import CompositeProvider
from btc_backtest.data.providers.local import LocalParquetProvider
from btc_backtest.data.providers.synthetic import SyntheticProvider
from btc_backtest.data.validation import frame_fingerprint, validate_ohlcv
from btc_backtest.errors import DataValidationError, ProviderError

UTC = timezone.utc


def request(
    *,
    provider: str = "fixture",
    symbol: str = "BTC/USD",
    timeframe: Timeframe = "1d",
    market: str = "spot",
    require_real: bool = True,
) -> DataRequest:
    return DataRequest(
        provider=provider,
        symbol=symbol,
        timeframe=timeframe,
        market=market,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 5, tzinfo=UTC),
        require_real=require_real,
    )


def frame_for_days(*days: int, close_offset: float = 0.0) -> pd.DataFrame:
    closes = [100.0 + day + close_offset for day in days]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 2 for value in closes],
            "low": [value - 2 for value in closes],
            "close": [value + 1 for value in closes],
            "volume": [10.0 + day for day in days],
        },
        index=pd.DatetimeIndex(
            [datetime(2024, 1, day, tzinfo=UTC) for day in days],
            name="timestamp",
        ),
    )


def make_dataset(
    data_request: DataRequest,
    frame: pd.DataFrame | None = None,
    *,
    provider: str | None = None,
    symbol: str | None = None,
    real_data: bool = True,
    fingerprint: str | None = None,
) -> MarketDataset:
    raw_frame = frame if frame is not None else frame_for_days(1, 2, 3, 4)
    permissive_request = data_request.model_copy(
        update={"require_complete": False, "max_missing_ratio": 1.0}
    )
    normalized, gaps = validate_ohlcv(raw_frame, permissive_request)
    delta = timedelta(days=1) if data_request.timeframe == "1d" else timedelta(hours=1)
    manifest = DataManifest(
        provider=provider or data_request.provider,
        market=data_request.market,
        symbol=symbol or data_request.symbol,
        timeframe=data_request.timeframe,
        requested_start=data_request.start,
        requested_end=data_request.end,
        delivered_start=normalized.index[0].to_pydatetime(),
        delivered_end=(normalized.index[-1] + delta).to_pydatetime(),
        retrieved_at=datetime(2024, 1, 6, tzinfo=UTC),
        real_data=real_data,
        raw_sha256=("a" * 64,),
        normalized_sha256=fingerprint or frame_fingerprint(normalized),
        gaps=gaps,
    )
    return MarketDataset(frame=normalized, manifest=manifest)


class FixtureProvider:
    def __init__(
        self,
        provider_id: str = "fixture",
        *,
        frame: pd.DataFrame | None = None,
        real_data: bool = True,
        symbols: tuple[str, ...] = ("BTC/USD",),
        returned_symbol: str | None = None,
        returned_real_data: bool = True,
        returned_fingerprint: str | None = None,
    ) -> None:
        self.metadata = ProviderMetadata(
            id=provider_id,
            real_data=real_data,
            timeframes=("1d",),
            markets=("spot",),
            symbols=symbols,
        )
        self.frame = frame
        self.returned_symbol = returned_symbol
        self.returned_real_data = returned_real_data
        self.returned_fingerprint = returned_fingerprint
        self.calls = 0

    def fetch(self, data_request: DataRequest) -> MarketDataset:
        self.calls += 1
        return make_dataset(
            data_request,
            self.frame,
            provider=self.metadata.id,
            symbol=self.returned_symbol,
            real_data=self.returned_real_data,
            fingerprint=self.returned_fingerprint,
        )


def test_registry_caches_provider_result(tmp_path: Path) -> None:
    provider = FixtureProvider()
    registry = ProviderRegistry([provider])
    data_request = request()
    cache = DataCache(tmp_path)

    first = registry.fetch(data_request, cache)
    second = registry.fetch(data_request, cache)

    assert first.manifest.normalized_sha256 == second.manifest.normalized_sha256
    assert provider.calls == 1


def test_registry_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="unknown provider"):
        ProviderRegistry([]).fetch(request(), DataCache(tmp_path))


@pytest.mark.parametrize(
    ("data_request", "message"),
    [
        (request(timeframe="1h"), "timeframe"),
        (request(market="futures"), "market"),
        (request(symbol="ETH/USD"), "symbol"),
    ],
)
def test_registry_rejects_unsupported_request(
    tmp_path: Path,
    data_request: DataRequest,
    message: str,
) -> None:
    with pytest.raises(ProviderError, match=message):
        ProviderRegistry([FixtureProvider()]).fetch(data_request, DataCache(tmp_path))


def test_registry_rejects_synthetic_for_real_request(tmp_path: Path) -> None:
    registry = ProviderRegistry([SyntheticProvider(seed=7)])

    with pytest.raises(ProviderError, match="requires real data"):
        registry.fetch(request(provider="synthetic"), DataCache(tmp_path))


def test_registry_rejects_provider_result_with_wrong_identity(tmp_path: Path) -> None:
    provider = FixtureProvider(returned_symbol="BTC/USDT")

    with pytest.raises(DataValidationError, match="identity"):
        ProviderRegistry([provider]).fetch(request(), DataCache(tmp_path))


def test_local_parquet_hashes_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "btc.parquet"
    original = frame_for_days(1, 2, 3, 4)
    original.to_parquet(source)
    before = source.read_bytes()
    data_request = request(provider="local")

    result = LocalParquetProvider(source).fetch(data_request)

    assert source.read_bytes() == before
    assert result.manifest.source == str(source.resolve())
    assert result.manifest.raw_sha256 == (hashlib.sha256(before).hexdigest(),)
    pd.testing.assert_frame_equal(result.frame, original)


def test_synthetic_provider_is_deterministic_and_explicitly_labeled() -> None:
    data_request = request(provider="synthetic", require_real=False)

    first = SyntheticProvider(seed=7).fetch(data_request)
    second = SyntheticProvider(seed=7).fetch(data_request)

    assert first.manifest.real_data is False
    assert first.manifest.source == "synthetic://seed/7"
    assert frame_fingerprint(first.frame) == frame_fingerprint(second.frame)


def test_composite_stitches_explicit_segments() -> None:
    first = FixtureProvider("first", frame=frame_for_days(1, 2))
    second = FixtureProvider("second", frame=frame_for_days(3, 4))
    composite = CompositeProvider([first, second], overlap_tolerance=0.0)
    data_request = request(provider="composite")

    result = composite.fetch(data_request)

    assert result.frame.index.tolist() == list(
        pd.date_range("2024-01-01", periods=4, freq="1D", tz="UTC")
    )
    assert [segment.provider for segment in result.manifest.segments] == [
        "first",
        "second",
    ]


def test_composite_rejects_conflicting_overlap() -> None:
    first = FixtureProvider("first", frame=frame_for_days(1, 2, 3))
    second = FixtureProvider(
        "second",
        frame=frame_for_days(3, 4, close_offset=10.0),
    )

    with pytest.raises(DataValidationError, match="overlap"):
        CompositeProvider([first, second], overlap_tolerance=0.0).fetch(
            request(provider="composite")
        )


def test_composite_never_silently_combines_usd_and_usdt() -> None:
    usd = FixtureProvider("usd", symbols=("BTC/USD",))
    usdt = FixtureProvider(
        "usdt",
        symbols=("BTC/USDT",),
        returned_symbol="BTC/USDT",
    )

    with pytest.raises(DataValidationError, match="symbol"):
        CompositeProvider([usd, usdt]).fetch(request(provider="composite"))


def test_composite_inherits_restricted_child_symbols() -> None:
    unrestricted = FixtureProvider("unrestricted", symbols=())
    btc_only = FixtureProvider("btc", symbols=("BTC/USD",))
    composite = CompositeProvider([unrestricted, btc_only])

    with pytest.raises(DataValidationError, match="symbol"):
        composite.fetch(
            request(provider="composite", symbol="ETH/USD")
        )


def test_composite_rejects_child_that_mislabels_real_data() -> None:
    dishonest = FixtureProvider(
        "dishonest",
        real_data=True,
        returned_real_data=False,
    )

    with pytest.raises(DataValidationError, match="real-data"):
        CompositeProvider([dishonest]).fetch(request(provider="composite"))


def test_composite_revalidates_child_fingerprint() -> None:
    corrupt = FixtureProvider(
        "corrupt",
        returned_fingerprint="b" * 64,
    )

    with pytest.raises(DataValidationError, match="fingerprint"):
        CompositeProvider([corrupt]).fetch(request(provider="composite"))
