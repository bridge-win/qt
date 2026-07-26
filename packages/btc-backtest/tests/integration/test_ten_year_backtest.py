from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from btc_backtest.acceptance import (
    BITSTAMP_TEN_YEAR_END,
    BITSTAMP_TEN_YEAR_START,
    expected_slots,
    fetch_bitstamp_ten_year,
)
from btc_backtest.data.models import Timeframe
from btc_backtest.errors import NetworkUnavailableError

START = datetime(2016, 7, 25, tzinfo=timezone.utc)
END = datetime(2026, 7, 25, tzinfo=timezone.utc)
EXPECTED_SLOTS = (("1d", 3652), ("1h", 87648))


@pytest.mark.integration
@pytest.mark.parametrize(("timeframe", "slots"), EXPECTED_SLOTS)
def test_bitstamp_live_ten_year_coverage(
    tmp_path: Path,
    timeframe: Timeframe,
    slots: int,
) -> None:
    assert BITSTAMP_TEN_YEAR_START == START
    assert BITSTAMP_TEN_YEAR_END == END
    assert expected_slots(START, END, timeframe) == slots

    try:
        dataset = fetch_bitstamp_ten_year(
            timeframe,
            cache_dir=tmp_path / "cache",
            timeout=30.0,
        )
    except NetworkUnavailableError as error:
        pytest.skip(str(error))

    missing = sum(gap.missing_bars for gap in dataset.manifest.gaps)
    missing_ratio = missing / slots

    assert dataset.manifest.provider == "bitstamp"
    assert dataset.manifest.market == "spot"
    assert dataset.manifest.symbol == "BTC/USD"
    assert dataset.manifest.timeframe == timeframe
    assert dataset.manifest.real_data is True
    assert dataset.manifest.delivered_start == START
    assert dataset.manifest.delivered_end == END
    assert len(dataset.frame) + missing == slots
    assert missing_ratio <= 0.001
