from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from qt.btc_alerts.models import Policy, Thresholds
from qt.btc_alerts.store import ConflictError, Store

from .conftest import NOW


def test_candidate_dedup_and_review_latch_survive_restart(store, candidate):
    assert store.add_candidate(candidate, NOW)
    assert not store.add_candidate(candidate, NOW)
    assert store.review_budget("bar:1", NOW, {})
    other = Store(store.path.parent)
    assert not other.review_budget("bar:1", NOW + 5, {})


def test_quota_is_atomic_across_concurrent_senders_and_directions(store):
    ids = [store.request_test("different-test-" + str(index), NOW, "fixture") for index in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda event_id: store.claim(event_id, "recipient-btc", NOW), ids))
    assert sum(results) == 1


@pytest.mark.parametrize("status", ["accepted", "unknown"])
def test_success_or_unknown_blocks_rolling_hour_across_restart(store, status):
    first = store.request_test("first-test-request", NOW, "fixture")
    assert store.claim(first, "recipient-btc", NOW)
    store.sent(first, "recipient-btc", NOW + 3, status, message_id=101 if status == "accepted" else None)
    reopened = Store(store.path.parent)
    second = reopened.request_test("second-test-request", NOW + 3599, "fixture")
    assert not reopened.claim(second, "recipient-btc", NOW + 3602.999)
    third = reopened.request_test("third-test-request", NOW + 3603, "fixture")
    assert reopened.claim(third, "recipient-btc", NOW + 3603)


def test_interrupted_send_becomes_unknown_without_retry(store):
    first = store.request_test("first-test-request", NOW, "fixture")
    assert store.claim(first, "recipient-btc", NOW)
    store.recover(NOW + 10)
    assert store.pending(NOW + 10) == []
    second = store.request_test("second-test-request", NOW + 20, "fixture")
    assert not store.claim(second, "recipient-btc", NOW + 20)


def test_explicit_429_releases_quota_but_retry_must_reclaim(store):
    first = store.request_test("first-test-request", NOW, "fixture")
    assert store.claim(first, "recipient-btc", NOW)
    store.sent(first, "recipient-btc", NOW + 1, "pending", retry_at=NOW + 20)
    assert not store.claim(first, "recipient-btc", NOW + 19)
    assert store.claim(first, "recipient-btc", NOW + 20)


def test_clock_rollback_fails_closed(store):
    first = store.request_test("first-test-request", NOW, "fixture")
    assert store.claim(first, "recipient-btc", NOW)
    store.sent(first, "recipient-btc", NOW + 1, "failed")
    second = store.request_test("second-test-request", NOW, "fixture")
    assert not store.claim(second, "recipient-btc", NOW)


def test_old_candidate_and_expired_test_never_claim(store):
    event_id = store.request_test("first-test-request", NOW, "fixture")
    assert not store.claim(event_id, "recipient-btc", NOW + 600)
    store.expire_and_retain(NOW + 600)
    assert store.event(event_id)["status"] == "expired"


def test_hourly_and_daily_model_budgets_are_durable(store):
    for index in range(4):
        assert store.review_budget(str(index), NOW + index, {})
    assert not store.review_budget("fifth", NOW + 60, {})
    assert not Store(store.path.parent).review_budget("rollback", NOW - 3601, {})
    assert store.review_budget("new-hour", NOW + 3605, {})


def test_policy_optimistic_revision_and_restore(store, tmp_path):
    revision, policy = store.policy()
    changed = Policy(thresholds=Thresholds(breakout_volume=1.8))
    assert store.set_policy(revision, changed, NOW) == revision + 1
    with pytest.raises(ConflictError):
        store.set_policy(revision, policy, NOW)
    destination = tmp_path / "restore" / "alerts.sqlite3"
    store.backup(destination)
    restored = Store(destination.parent)
    assert restored.policy() == store.policy()


def test_no_approval_can_substitute_for_research_and_forward_evidence(store, candidate):
    reasons = store.promotion_reasons(candidate, NOW)
    assert "validation_pending:trend:long" in reasons
    assert "forward_shadow_evidence_pending" in reasons
def test_authentication_probe_uses_rolling_hour_across_calendar_boundary(store):
    assert store.review_budget("probe:previous-hour", 3599, {"kind": "authentication_probe"})
    assert not store.review_budget("probe:next-hour", 3600, {"kind": "authentication_probe"})
    assert store.review_budget("probe:later-hour", 7199, {"kind": "authentication_probe"})
