from __future__ import annotations

import pytest
from pydantic import ValidationError

from qt.btc_alerts.models import Bar, Policy, Thresholds
from qt.btc_alerts.signals import evaluate, revalidate, rsi, validate_bars

from .conftest import NOW, packet


def test_closed_breakout_records_values_and_thresholds(policy):
    bars, quote = packet()
    result = evaluate(bars, quote, policy, NOW)
    assert len(result) == 1
    value = result[0]
    assert value.direction == "long"
    assert value.triggers[0].strategy_id == "trend"
    assert value.triggers[0].observations["volume_multiple"] == 1.5
    assert value.triggers[0].thresholds["volume_multiple"] == 1.5
    assert value.expires_at == NOW + 600
    assert value.net_reward_risk > 2
    assert {"card:trend", "market", "validation"} <= set(value.evidence_ids)


def test_threshold_just_below_boundary_does_not_trigger(policy):
    bars, quote = packet(volume=149.999)
    assert evaluate(bars, quote, policy, NOW) == []


def test_short_has_distinct_market_and_funding_cost(policy):
    bars, quote = packet(down=True)
    value = evaluate(bars, quote, policy, NOW)[0]
    assert value.direction == "short"
    assert value.product == "perpetual"
    assert value.cost_bps == policy.round_trip_cost_bps + 10


def test_send_revalidation_reprices_increased_funding_cost(policy):
    bars, quote = packet(down=True)
    candidate = evaluate(bars, quote, policy, NOW)[0]
    assert "net_reward_risk" not in revalidate(candidate, quote, policy, NOW)
    expensive = quote.model_copy(update={"funding_cost_bps": 1000.0})
    assert "net_reward_risk" in revalidate(candidate, expensive, policy, NOW)


def test_downward_spot_signal_is_reduction_not_short(policy):
    bars, quote = packet(down=True)
    quote = quote.model_copy(update={"venue": "binance", "product": "spot", "symbol": "BTCUSDT"})
    assert evaluate(bars, quote, policy, NOW)[0].direction == "reduce_long"


@pytest.mark.parametrize("failure", ["unclosed", "duplicate", "missing", "stale", "unaligned"])
def test_bad_bar_history_fails_closed(failure):
    bars, _ = packet()
    rows = bars["15m"].copy()
    if failure == "unclosed":
        rows[-1] = rows[-1].model_copy(update={"open_time": NOW})
    elif failure == "duplicate":
        rows[-1] = rows[-2]
    elif failure == "missing":
        rows.pop(-20)
    elif failure == "stale":
        rows = [bar.model_copy(update={"open_time": bar.open_time - 1800}) for bar in rows]
    else:
        rows[-1] = rows[-1].model_copy(update={"open_time": rows[-1].open_time + 1})
    with pytest.raises(ValueError):
        validate_bars(rows, "15m", NOW)


@pytest.mark.parametrize("field,value", [("close", float("nan")), ("volume", -1), ("low", 70000)])
def test_nonfinite_or_incoherent_bars_are_rejected(field, value):
    bar = packet()[0]["15m"][-1].model_dump()
    with pytest.raises(ValidationError):
        Bar.model_validate(bar | {field: value})


@pytest.mark.parametrize("change,reason", [
    ({"observed_at": NOW - 5.001}, "quote_stale_or_spread"),
    ({"observed_at": NOW + 0.001}, "quote_stale_or_spread"),
    ({"bid": 61000, "ask": 61001}, "price_moved"),
    ({"venue": "okx"}, "quote_identity_mismatch"),
])
def test_post_review_quote_checks(candidate, policy, change, reason):
    quote = packet()[1].model_copy(update=change)
    assert reason in revalidate(candidate, quote, policy, NOW)


def test_ttl_and_new_strategy_version_expire_old_candidate(candidate, policy):
    assert "candidate_expired_or_future" in revalidate(candidate, packet()[1], policy, NOW + 600)
    changed = policy.model_copy(update={"thresholds": Thresholds(breakout_volume=1.7)})
    assert "strategy_version_changed" in revalidate(candidate, packet()[1], changed, NOW)
    assert policy.model_copy(update={"mode": "notify"}).strategy_version == policy.strategy_version


def test_policy_hash_survives_json_roundtrip_and_mode_changes():
    policy = Policy()
    loaded = Policy.model_validate_json(policy.model_dump_json())
    assert loaded.strategy_version == policy.strategy_version
    assert loaded.model_copy(update={"mode": "notify"}).strategy_version == policy.strategy_version


def test_rsi_flat_series_is_neutral():
    assert set(rsi([100.0] * 50)) == {50}


def test_wick_requires_actual_range_recovery(policy):
    bars, quote = packet(volume=100)
    bars["15m"][-1] = Bar(open_time=NOW - 900, open=59995, high=60010, low=56000, close=59990, volume=100)
    value = evaluate(bars, quote, policy, NOW)[0]
    assert value.triggers[0].strategy_id == "wick"
    assert value.direction == "long"


@pytest.mark.parametrize("value", [0, 1800, 3599, 3601, 7200])
def test_hourly_limit_cannot_be_lowered_or_redefined(value):
    with pytest.raises(ValidationError):
        Policy(cooldown_seconds=value)
