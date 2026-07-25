from __future__ import annotations

from pathlib import Path

from btc_backtest.signals.store import SignalStore

from .helpers import full_query, observation, utc


def test_store_hides_late_observation_from_earlier_bar(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path)
    late = observation(
        effective_at=utc(),
        observed_at=utc(days=4),
        expires_at=utc(days=6),
    )
    store.append((late,))

    assert store.query(full_query(), available_at=utc(days=3)) == ()
    assert store.query(full_query(), available_at=utc(days=4)) == (late,)


def test_store_uses_observed_at_not_effective_at_for_availability(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path)
    revised = observation(
        id="revision",
        source_event_id="revision",
        effective_at=utc(hours=1),
        observed_at=utc(days=2),
        expires_at=utc(days=5),
        payload_sha256="a" * 64,
        quality_flags=("revised",),
    )
    store.append((revised,))

    before = store.query(full_query(), available_at=utc(days=1))
    after = store.query(full_query(), available_at=utc(days=3))

    assert before == ()
    assert after == (revised,)
