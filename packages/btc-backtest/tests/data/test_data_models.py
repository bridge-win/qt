from datetime import datetime, timedelta, timezone
from typing import cast

import pandas as pd
import pytest
from btc_backtest.data.models import (
    DataGap,
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from pydantic import ValidationError

UTC = timezone.utc


def test_data_request_normalizes_aware_timestamps_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))

    request = DataRequest(
        provider="fixture",
        symbol="BTC/USD",
        timeframe="1d",
        start=datetime(2024, 1, 1, 19, tzinfo=eastern),
        end=datetime(2024, 1, 3, 19, tzinfo=eastern),
    )

    assert request.start == datetime(2024, 1, 2, tzinfo=UTC)
    assert request.end == datetime(2024, 1, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (datetime(2024, 1, 1), datetime(2024, 1, 2, tzinfo=UTC), "timezone-aware"),
        (datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC), "after start"),
        (
            datetime(2024, 1, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, 1, tzinfo=UTC),
            "aligned",
        ),
    ],
)
def test_data_request_rejects_invalid_daily_interval(
    start: datetime,
    end: datetime,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        DataRequest(
            provider="fixture",
            symbol="BTC/USD",
            timeframe="1d",
            start=start,
            end=end,
        )


def test_manifest_is_frozen_and_requires_valid_fingerprints() -> None:
    gap = DataGap(
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
        missing_bars=1,
    )
    manifest = DataManifest(
        provider="fixture",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        requested_start=datetime(2024, 1, 1, tzinfo=UTC),
        requested_end=datetime(2024, 1, 4, tzinfo=UTC),
        delivered_start=datetime(2024, 1, 1, tzinfo=UTC),
        delivered_end=datetime(2024, 1, 4, tzinfo=UTC),
        retrieved_at=datetime(2024, 1, 5, tzinfo=UTC),
        real_data=True,
        raw_sha256=("a" * 64,),
        normalized_sha256="b" * 64,
        gaps=(gap,),
    )

    with pytest.raises(ValidationError):
        manifest.provider = "changed"
    invalid_values = manifest.model_dump()
    invalid_values["normalized_sha256"] = "short"
    with pytest.raises(ValidationError, match="sha256"):
        DataManifest(**invalid_values)


def test_market_bundle_copies_auxiliary_mapping() -> None:
    frame = pd.DataFrame()
    manifest = DataManifest(
        provider="fixture",
        market="spot",
        symbol="BTC/USD",
        timeframe="1d",
        requested_start=datetime(2024, 1, 1, tzinfo=UTC),
        requested_end=datetime(2024, 1, 2, tzinfo=UTC),
        delivered_start=datetime(2024, 1, 1, tzinfo=UTC),
        delivered_end=datetime(2024, 1, 2, tzinfo=UTC),
        retrieved_at=datetime(2024, 1, 3, tzinfo=UTC),
        real_data=True,
        raw_sha256=("a" * 64,),
        normalized_sha256="b" * 64,
    )
    dataset = MarketDataset(frame=frame, manifest=manifest)
    auxiliary = {"funding": dataset}

    bundle = MarketBundle(primary=dataset, auxiliary=auxiliary)
    auxiliary.clear()

    assert bundle.auxiliary == {"funding": dataset}
    with pytest.raises(TypeError):
        cast(dict[str, MarketDataset], bundle.auxiliary)["other"] = dataset
