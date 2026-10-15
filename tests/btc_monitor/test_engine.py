from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from qt.btc_monitor.config import MonitorConfig
from qt.btc_monitor.engine import MonitorEngine
from qt.btc_monitor.models import MarketEvent

START = 1_800_000_000.0


def candle(engine, now, mono, opened):
    return engine.ingest(
        MarketEvent(
            channel="candle",
            event_time=opened + 59.999,
            received_at=now,
            received_mono=mono,
            sequence=int(opened * 1000),
            price=100000,
            low=98000,
            high=102000,
            open_time=opened,
            close_time=opened + 59.999,
            historical=True,
        ),
        now,
        mono,
    )


def prepared(config=None):
    engine = MonitorEngine(config or MonitorConfig(), boot_id="test")
    for opened in (START - 180, START - 120, START - 60):
        candle(engine, START, 1000, opened)
    return engine


def tick(engine, index, price=100000, *, seq=None, fraction=0):
    now, mono = START + index + fraction, 1000 + index + fraction
    sequence = seq if seq is not None else index + 1
    engine.ingest(
        MarketEvent(
            channel="quote",
            received_at=now,
            received_mono=mono,
            sequence=sequence,
            price=price,
            bid=price - 0.5,
            ask=price + 0.5,
        ),
        now,
        mono,
    )
    result = engine.ingest(
        MarketEvent(
            channel="trade",
            event_time=now,
            received_at=now,
            received_mono=mono,
            sequence=sequence,
            price=price,
            quantity=1,
        ),
        now,
        mono,
    )
    if index and index % 60 == 0 and fraction == 0:
        candle(engine, now, mono, now - 60)
    return result


def oscillation(engine, seconds=720, amplitude=1100):
    for i in range(seconds + 1):
        tick(engine, i, 100000 + amplitude * math.sin(i * math.pi / 60))
    return engine.snapshot(START + seconds, 1000 + seconds)


def test_same_minute_subsecond_wick_preserves_path_and_identity():
    engine = prepared(MonitorConfig(confirmation_seconds=0))
    tick(engine, 0, 100000, seq=1)
    tick(engine, 0, 99000, seq=2, fraction=0.1)
    watch = engine.active
    assert watch.state == "WATCH"
    tick(engine, 0, 100000, seq=3, fraction=0.2)
    confirmed = engine.active
    assert confirmed.event_id == watch.event_id
    assert confirmed.revision > watch.revision
    assert confirmed.state == "CONFIRMED"
    window = engine.micro.window(5, START + 0.2)
    assert window["low"] == 99000
    assert window["high"] == 100000
    assert window["path"] == 2000
    assert window["reversals"] == 1
    assert window["buckets"] == 1
    tick(engine, 0, 98500, seq=4, fraction=0.3)
    assert engine.active.event_id == confirmed.event_id
    assert engine.active.state == "INVALIDATED"
    assert engine.active.reason == "renewed_extreme"
    tick(engine, 0, 100000, seq=5, fraction=0.4)
    assert engine.active.state == "INVALIDATED"  # no rediscovery flood
    notifications = engine.drain_notifications()
    assert sum(item["positive"] for item in notifications) == 1
    confirmed_notice = next(item for item in notifications if item["positive"])
    assert (
        f"expires {datetime.fromtimestamp(confirmed.expires_at, timezone.utc).isoformat()}"
        in confirmed_notice["text"]
    )


def test_confirmation_requires_sustained_recovery():
    engine = prepared(MonitorConfig(confirmation_seconds=3))
    tick(engine, 0, 100000)
    tick(engine, 1, 99000)
    tick(engine, 2, 100000)
    tick(engine, 3, 99100)
    tick(engine, 4, 100000)
    tick(engine, 5, 100000)
    assert engine.active.state == "WATCH"
    tick(engine, 7, 100000, seq=7)
    assert engine.active.state == "CONFIRMED"


def test_default_realistic_cost_oscillation_candidate_and_high_fee_rejection():
    snapshot = oscillation(prepared())
    assert snapshot.health.data_ready and snapshot.health.assessment_ready
    assert snapshot.assessment.state == "CANDIDATE"
    assert all(gate.passed for gate in snapshot.assessment.gates)
    assert snapshot.assessment.evidence["fee_per_side_bps"] == 10
    assert snapshot.assessment.net_spacing_bps > 0
    expensive = oscillation(prepared(MonitorConfig(fee_per_side_bps=30)))
    assert expensive.assessment.state == "UNSUITABLE"
    assert "net_cost_edge" in expensive.assessment.reasons
    assert "saleable_btc_inventory" in snapshot.assessment.missing_inputs
    assert snapshot.assessment.allowed_actions == ["monitor", "research"]
    assert snapshot.live_execution_enabled is False


def test_monotone_trend_rejected_even_with_big_range():
    engine = prepared()
    for index in range(721):
        tick(engine, index, 100000 + index * 10)
    snapshot = engine.snapshot(START + 720, 1720)
    assert snapshot.assessment.state == "UNSUITABLE"
    assert "two_way_motion" in snapshot.assessment.reasons
    assert snapshot.assessment.evidence["directional_efficiency"] == pytest.approx(1)


def test_live_quotes_cannot_mask_stale_trade():
    engine = prepared()
    snapshot = oscillation(engine)
    assert snapshot.assessment.state == "CANDIDATE"
    now = START + 730
    engine.ingest(
        MarketEvent(
            channel="quote",
            received_at=now,
            received_mono=1730,
            sequence=9999,
            price=100000,
            bid=99999,
            ask=100001,
        ),
        now,
        1730,
    )
    snapshot = engine.snapshot(now, 1730)
    assert snapshot.health.channels["quote"].ready
    assert not snapshot.health.channels["trade"].ready
    assert snapshot.assessment.state == "DATA_UNHEALTHY"
    assert any(item["notification_class"] == "withdrawn" for item in engine.drain_notifications())


def test_delayed_history_never_refreshes_current_feed():
    engine = prepared()
    event = MarketEvent(
        channel="trade",
        event_time=START - 100,
        received_at=START,
        received_mono=1000,
        sequence=1,
        price=100000,
    )
    assert not engine.ingest(event, START, 1000)
    snapshot = engine.snapshot(START, 1000)
    assert not snapshot.health.data_ready
    assert snapshot.health.coverage_seconds == 0
    assert snapshot.micro["price"] is None
    assert not engine.ingest(event.model_copy(update={"historical": True}), START, 1000)


@pytest.mark.parametrize("case", ["future", "disorder", "gap", "backlog", "receive_future"])
def test_bad_paths_invalidate_and_force_warmup(case):
    engine = prepared()
    tick(engine, 0)
    now, mono = START + 1, 1001
    values = dict(
        channel="trade",
        event_time=now,
        received_at=now,
        received_mono=mono,
        sequence=2,
        price=100000,
    )
    if case == "future":
        values["event_time"] = now + 10
    elif case == "disorder":
        values["event_time"] = START - 1
    elif case == "gap":
        values["sequence"] = 5
    elif case == "backlog":
        values["received_mono"] = 998
    else:
        values["received_mono"] = mono + 10
    engine.ingest(MarketEvent(**values), now, mono)
    snapshot = engine.snapshot(now, mono)
    assert not snapshot.health.data_ready
    assert snapshot.health.coverage_seconds == 0
    assert engine.errors


def test_duplicate_does_not_refresh_and_quote_id_skip_is_valid():
    engine = prepared()
    tick(engine, 0)
    duplicate = engine.channels["trade"].model_copy(
        update={"received_at": START + 20, "received_mono": 1020}
    )
    assert not engine.ingest(duplicate, START + 20, 1020)
    assert engine.channels["trade"].received_at == START
    quote = engine.channels["quote"].model_copy(
        update={"sequence": 999, "received_at": START + 1, "received_mono": 1001}
    )
    assert engine.ingest(quote, START + 1, 1001)
    assert "trade_sequence_gap" not in engine.errors


def test_reconnect_and_restart_identity_fail_closed():
    engine = prepared(MonitorConfig(confirmation_seconds=0))
    tick(engine, 0)
    tick(engine, 1, 99000)
    tick(engine, 2)
    before = engine.snapshot(START + 2, 1002)
    event_id = engine.active.event_id
    engine.reset("reconnect", START + 3)
    after = engine.snapshot(START + 3, 1003)
    assert not after.health.data_ready
    assert engine.active.event_id == event_id
    assert engine.active.state == "INVALIDATED"
    restart = prepared()
    restart.restore(before, START + 4, 1004)
    assert restart.active.event_id == event_id
    assert restart.active.state == "INVALIDATED"
    assert not restart.micro.buckets
    tick(restart, 5)
    tick(restart, 6, 99000)
    assert restart.active.event_id == event_id


def test_event_expiry_once_and_bounded_high_rate_bucket():
    engine = prepared(MonitorConfig(confirmation_seconds=0, event_ttl_seconds=5))
    tick(engine, 0)
    tick(engine, 0, 99000, seq=2, fraction=0.1)
    for seq in range(3, 5003):
        tick(engine, 0, 100000, seq=seq, fraction=0.2)
    assert len(engine.micro.buckets) == 1
    assert engine.micro.buckets[0].count == 5002
    engine.snapshot(START + 6, 1006)
    count = len(engine.events)
    assert engine.active.state == "EXPIRED"
    engine.snapshot(START + 7, 1007)
    assert len(engine.events) == count


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0, -1])
def test_nonfinite_or_invalid_prices_rejected_at_boundary(price):
    with pytest.raises(ValidationError):
        MarketEvent(
            channel="trade",
            event_time=START,
            received_at=START,
            received_mono=1000,
            sequence=1,
            price=price,
        )


def test_secret_endpoint_configuration_rejected():
    with pytest.raises(ValidationError):
        MonitorConfig(rest_url="https://user:secret@example.com")


def test_fresh_shock_updates_cooldown_even_inside_withdrawn_event_dedup():
    engine = prepared(MonitorConfig(confirmation_seconds=0))
    tick(engine, 0)
    tick(engine, 0, 99000, seq=2, fraction=0.1)
    tick(engine, 0, 100000, seq=3, fraction=0.2)
    tick(engine, 0, 98500, seq=4, fraction=0.3)
    identity = engine.active.event_id
    for index in range(1, 40):
        tick(engine, index, 100000, seq=index + 4)
    tick(engine, 39, 98000, seq=44, fraction=0.1)
    assert engine.active.event_id == identity
    assert engine.active.state == "INVALIDATED"
    assert engine.last_shock_mono == pytest.approx(1039.1)


def test_early_confirmation_record_cannot_generate_positive_notification():
    engine = prepared(MonitorConfig(confirmation_seconds=0))
    tick(engine, 0)
    tick(engine, 0, 99000, seq=2, fraction=0.1)
    tick(engine, 0, 100000, seq=3, fraction=0.2)
    snapshot = engine.snapshot(START + 0.2, 1000.2)
    assert engine.active.state == "CONFIRMED"
    assert not snapshot.health.data_ready
    assert not any(item["positive"] for item in engine.drain_notifications())


def test_live_closed_candle_consumer_backlog_is_not_healthy_context():
    engine = prepared()
    tick(engine, 60)
    now, mono = START + 120, 1120
    item = MarketEvent(
        channel="candle",
        event_time=now - 0.001,
        received_at=now,
        received_mono=mono - 5,
        sequence=int((now - 60) * 1000),
        price=100000,
        low=99000,
        high=101000,
        open_time=now - 60,
        close_time=now - 0.001,
    )
    engine.ingest(item, now, mono)
    snapshot = engine.snapshot(now, mono)
    assert not snapshot.health.channels["candle"].ready
    assert not snapshot.health.data_ready


def test_identical_replay_has_identical_decisions():
    left, right = prepared(), prepared()
    left_snapshot, right_snapshot = oscillation(left), oscillation(right)
    assert left_snapshot.model_dump() == right_snapshot.model_dump()


def test_opposite_impulse_updates_cooldown_during_confirmed_identity():
    engine = prepared()
    for index in range(80):
        tick(engine, index, 100000)
    tick(engine, 80, 99000)
    for index in range(81, 121):
        tick(engine, index, 99700)
    assert engine.active.state == "CONFIRMED"
    identity = engine.active.event_id
    old_shock = engine.last_shock_mono
    tick(engine, 120, 102000, seq=122, fraction=0.1)
    tick(engine, 120, 99700, seq=123, fraction=0.2)
    assert engine.active.event_id == identity
    assert engine.last_shock_mono > old_shock
    assert engine.last_shock_mono >= 1120.1


def test_notification_carries_the_exact_projected_decision_and_as_of():
    engine = prepared()
    snapshot = oscillation(engine)
    notifications = engine.drain_notifications()
    candidate = next(item for item in notifications if item["notification_class"] == "candidate")
    assert candidate["as_of"] == snapshot.assessment.as_of
    assert candidate["decision"] == snapshot.assessment.model_dump(mode="json")
    assert snapshot.assessment.assessment_id in candidate["text"]
    assert (
        f"expires {datetime.fromtimestamp(snapshot.assessment.expires_at, timezone.utc).isoformat()}"
        in candidate["text"]
    )
