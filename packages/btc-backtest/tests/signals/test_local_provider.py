from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from btc_backtest.errors import DataValidationError, ProviderError
from btc_backtest.signals.models import SignalQuery
from btc_backtest.signals.providers.local import (
    QTIntelArchiveProvider,
    SignalArchiveProvider,
)

UTC = timezone.utc


def query(**updates: object) -> SignalQuery:
    values: dict[str, object] = {
        "start": datetime(2024, 1, 1, tzinfo=UTC),
        "end": datetime(2024, 1, 3, tzinfo=UTC),
        "symbol": "BTC/USD",
        "horizons": ("1d",),
    }
    values.update(updates)
    return SignalQuery.model_validate(values)


def archive_row(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_event_id": "event-1",
        "source_type": "research",
        "symbol": "BTC/USD",
        "horizon": "1d",
        "direction": "0.75",
        "confidence": "0.8",
        "raw_value": "12.5",
        "effective_at": "2024-01-01T00:00:00Z",
        "observed_at": "2024-01-02T00:00:00Z",
        "expires_at": "2024-01-04T00:00:00Z",
        "provenance": "https://signals.example/events/1",
    }
    values.update(updates)
    return values


@pytest.mark.parametrize("suffix", [".json", ".csv", ".parquet"])
def test_signal_archive_maps_supported_immutable_formats(
    tmp_path: Path,
    suffix: str,
) -> None:
    path = tmp_path / f"signals{suffix}"
    row = archive_row()
    if suffix == ".json":
        path.write_text(json.dumps([row]))
    elif suffix == ".csv":
        pd.DataFrame([row]).to_csv(path, index=False)
    else:
        pd.DataFrame([row]).to_parquet(path, index=False)

    item = SignalArchiveProvider(path).fetch(query())[0]

    assert item.provider == "signal_archive"
    assert item.source_event_id == "event-1"
    assert item.source_type == "research"
    assert item.direction == Decimal("0.75")
    assert item.confidence == Decimal("0.8")
    assert item.observed_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert item.payload_sha256
    assert "immutable_archive" in item.quality_flags


def test_signal_archive_preserves_late_availability(
    tmp_path: Path,
) -> None:
    path = tmp_path / "signals.json"
    path.write_text(
        json.dumps(
            [
                archive_row(
                    observed_at="2024-01-10T00:00:00Z",
                    expires_at="2024-01-11T00:00:00Z",
                )
            ]
        )
    )

    item = SignalArchiveProvider(path).fetch(query())[0]

    assert item.effective_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert item.observed_at == datetime(2024, 1, 10, tzinfo=UTC)


def test_signal_archive_rejects_mutation_after_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "signals.json"
    path.write_text(json.dumps([archive_row()]))
    provider = SignalArchiveProvider(path)
    path.write_text(json.dumps([archive_row(direction="-0.5")]))

    with pytest.raises(DataValidationError, match="changed"):
        provider.fetch(query())


def test_signal_archive_rejects_missing_columns_and_duplicates(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.json"
    missing = archive_row()
    del missing["observed_at"]
    missing_path.write_text(json.dumps([missing]))
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps([archive_row(), archive_row()]))

    with pytest.raises(DataValidationError, match="observed_at"):
        SignalArchiveProvider(missing_path)
    with pytest.raises(DataValidationError, match="duplicate"):
        SignalArchiveProvider(duplicate_path)


def test_qt_intel_archive_maps_ranked_finding(tmp_path: Path) -> None:
    path = tmp_path / "opportunities.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2024-01-02T00:00:00Z",
                "count": 1,
                "opportunities": [
                    {
                        "ts": "2024-01-01T12:00:00Z",
                        "kind": "funding",
                        "venue": "binance",
                        "symbol": "BTC/USDT",
                        "edge_bps": 12.5,
                        "score": 0.75,
                        "confidence": 0.8,
                        "capacity_usd": 100000,
                        "action": "open basis carry",
                        "why": "positive funding spread",
                        "details": {},
                    }
                ],
            }
        )
    )

    item = QTIntelArchiveProvider(path).fetch(query())[0]

    assert item.provider == "qt_intel"
    assert item.source_type == "funding"
    assert item.symbol == "BTC/USD"
    assert item.direction == Decimal("0.75")
    assert item.confidence == Decimal("0.8")
    assert item.effective_at == datetime(2024, 1, 1, 12, tzinfo=UTC)
    assert item.observed_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert "qt_intel_schema_v1" in item.quality_flags


def test_qt_intel_rejects_count_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "opportunities.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2024-01-02T00:00:00Z",
                "count": 1,
                "opportunities": [],
            }
        )
    )

    with pytest.raises(DataValidationError, match="count"):
        QTIntelArchiveProvider(path)


def test_local_providers_reject_unsupported_query(tmp_path: Path) -> None:
    path = tmp_path / "signals.json"
    path.write_text(json.dumps([archive_row()]))
    provider = SignalArchiveProvider(path)

    with pytest.raises(ProviderError, match="symbol"):
        provider.fetch(query(symbol="ETH/USD"))
    assert provider.fetch(query(source_types=("sentiment",))) == ()
