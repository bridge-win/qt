from __future__ import annotations

from pathlib import Path

import pytest
from btc_backtest.errors import DataValidationError
from btc_backtest.signals.store import SignalStore

from .helpers import full_query, observation, utc


def test_store_deduplicates_by_provider_and_source_event(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path)
    first = observation(id="a")
    replay = observation(id="b")

    fingerprint = store.append((first, replay))

    assert len(fingerprint) == 64
    assert store.query(full_query(), available_at=utc(days=1)) == (first,)


def test_store_idempotent_replay_preserves_fingerprint(tmp_path: Path) -> None:
    store = SignalStore(tmp_path)
    item = observation()

    first = store.append((item,))
    second = store.append((item,))

    assert second == first


def test_store_rejects_conflicting_payload_for_same_source_event(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path)
    store.append((observation(),))
    conflict = observation(
        direction=-1,
        payload_sha256="1" * 64,
    )

    with pytest.raises(DataValidationError, match="conflicting"):
        store.append((conflict,))


def test_store_query_filters_symbol_horizon_type_and_expiry(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path)
    accepted = observation(id="accepted")
    other_symbol = observation(
        id="other-symbol",
        source_event_id="other-symbol",
        symbol="ETH/USD",
        payload_sha256="1" * 64,
    )
    other_horizon = observation(
        id="other-horizon",
        source_event_id="other-horizon",
        horizon="1h",
        payload_sha256="2" * 64,
    )
    other_type = observation(
        id="other-type",
        source_event_id="other-type",
        source_type="funding",
        payload_sha256="3" * 64,
    )
    store.append((accepted, other_symbol, other_horizon, other_type))

    filtered = store.query(
        full_query(source_types=("sentiment",)),
        available_at=utc(days=1),
    )
    expired = store.query(
        full_query(),
        available_at=utc(days=2),
    )

    assert filtered == (accepted,)
    assert expired == ()


def test_store_detects_corrupt_published_snapshot(tmp_path: Path) -> None:
    store = SignalStore(tmp_path)
    store.append((observation(),))
    pointer = tmp_path / "current.json"
    pointer.write_text('{"schema_version":"1","fingerprint":"' + "f" * 64 + '"}')

    with pytest.raises(DataValidationError, match="corrupt"):
        store.query(full_query(), available_at=utc(days=1))
