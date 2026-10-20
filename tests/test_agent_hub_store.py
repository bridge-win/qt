"""Real SQLite concurrency, tenant isolation and crash-recovery contracts."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest

from qt.agent_hub.store import HubStore

TENANT = "research-team"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [1_000.0]
    monkeypatch.setattr("qt.agent_hub.store._now", lambda: value[0])
    return value


@pytest.fixture
def store(tmp_path: Path, clock: list[float]) -> HubStore:
    return HubStore(tmp_path / "ledger" / "hub.sqlite3")


def _enqueue(store: HubStore, key: str = "request-1", **kwargs: object) -> dict[str, object]:
    return store.enqueue(
        TENANT,
        "research",
        {"question": "Test hypothesis", "nested": [1, True, None]},
        idempotency_key=key,
        **kwargs,
    )


def _finish(store: HubStore, job: dict[str, object], *, tenant: str = TENANT) -> bool:
    return store.complete(
        tenant,
        str(job["id"]),
        str(job["worker_id"]),
        cast(int, job["lease_token"]),
        {"answer": "verified"},
    )


def test_persists_wal_jobs_results_and_ordered_events(store: HubStore) -> None:
    queued = _enqueue(store)
    claimed = store.claim(TENANT, "worker-1")
    assert claimed is not None
    assert claimed["id"] == queued["id"]
    assert claimed["payload"] == queued["payload"]
    assert claimed["status"] == "running"
    assert claimed["attempts"] == claimed["lease_token"] == 1
    store.event(TENANT, str(queued["id"]), "research.evidence", {"source": "文档", "page": 2})
    assert _finish(store, claimed)
    assert not _finish(store, claimed)

    reopened = HubStore(store.path)
    saved = reopened.get_job(TENANT, str(queued["id"]))
    assert saved is not None
    assert saved["result"] == {"answer": "verified"}
    assert saved["status"] == "succeeded"
    assert saved["lease_until"] is None
    assert saved["error"] is None
    assert "payload_json" not in saved
    events = reopened.events(TENANT, str(queued["id"]))
    assert [event["event_type"] for event in events] == [
        "job.queued",
        "job.claimed",
        "research.evidence",
        "job.succeeded",
    ]
    assert events[2]["payload"] == {"source": "文档", "page": 2}
    assert [event["id"] for event in reopened.events(TENANT, limit=2)] == [
        event["id"] for event in events[-2:]
    ]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_idempotent_enqueue_returns_snapshot_after_time_and_status_changes(
    store: HubStore,
    clock: list[float],
) -> None:
    first = store.enqueue(TENANT, "research", {"b": 2, "a": 1}, idempotency_key="stable")
    claimed = store.claim(TENANT, "worker")
    assert claimed is not None and _finish(store, claimed)
    clock[0] += 1_000
    second = store.enqueue(TENANT, "research", {"a": 1, "b": 2}, idempotency_key="stable")
    assert first["id"] == second["id"]
    assert second["status"] == "succeeded"
    assert len(store.list_jobs(TENANT)) == 1
    with pytest.raises(ValueError, match="different request"):
        store.enqueue(TENANT, "other", {"a": 1, "b": 2}, idempotency_key="stable")
    with pytest.raises(ValueError, match="different request"):
        store.enqueue(TENANT, "research", {"a": 1, "b": 3}, idempotency_key="stable")
    with pytest.raises(ValueError, match="different request"):
        store.enqueue(
            TENANT, "research", {"a": 1, "b": 2}, idempotency_key="stable", max_attempts=4
        )
    with pytest.raises(ValueError, match="different request"):
        store.enqueue(
            TENANT, "research", {"a": 1, "b": 2}, idempotency_key="stable", not_before=1_000
        )


def test_independent_connections_compete_for_one_job(store: HubStore) -> None:
    queued = _enqueue(store)
    other = HubStore(store.path)
    barrier = Barrier(2)

    def claim(pair: tuple[HubStore, str]) -> dict[str, object] | None:
        ledger, name = pair
        barrier.wait()
        return ledger.claim(TENANT, name)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, [(store, "one"), (other, "two")]))
    claimed = [job for job in claims if job is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == queued["id"]
    assert claimed[0]["attempts"] == 1


def test_concurrent_submission_creates_one_job(store: HubStore) -> None:
    other = HubStore(store.path)
    barrier = Barrier(2)

    def submit(ledger: HubStore) -> dict[str, object]:
        barrier.wait()
        return _enqueue(ledger)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(submit, [store, other]))
    assert jobs[0]["id"] == jobs[1]["id"]
    assert len(store.events(TENANT)) == 1


def test_expired_owner_is_fenced_before_and_after_takeover(
    store: HubStore,
    clock: list[float],
) -> None:
    _enqueue(store)
    first = store.claim(TENANT, "same-worker", lease_seconds=10)
    assert first is not None
    job_id, token = str(first["id"]), cast(int, first["lease_token"])
    clock[0] += 10
    assert not _finish(store, first)
    assert not store.heartbeat(TENANT, job_id, "same-worker", token)
    assert not store.fail(TENANT, job_id, "same-worker", token, "late failure")
    second = HubStore(store.path).claim(TENANT, "same-worker", lease_seconds=10)
    assert second is not None
    assert second["id"] == first["id"]
    assert second["lease_token"] == token + 1
    assert second["attempts"] == 2
    assert not _finish(store, first)
    assert not store.heartbeat(TENANT, job_id, "same-worker", token)
    assert _finish(store, second)
    assert "job.lease_expired" in [event["event_type"] for event in store.events(TENANT)]


def test_heartbeat_extends_only_a_live_matching_lease(store: HubStore, clock: list[float]) -> None:
    _enqueue(store)
    first = store.claim(TENANT, "worker", lease_seconds=10)
    assert first is not None
    job_id = str(first["id"])
    token = cast(int, first["lease_token"])
    clock[0] += 8
    assert not store.heartbeat(TENANT, job_id, "intruder", token, lease_seconds=20)
    assert not store.heartbeat(TENANT, job_id, "worker", token + 1, lease_seconds=20)
    assert store.heartbeat(TENANT, job_id, "worker", token, lease_seconds=20)
    # A shorter heartbeat must not shorten the lease already granted.
    assert store.heartbeat(TENANT, job_id, "worker", token, lease_seconds=1)
    clock[0] += 3
    assert store.claim(TENANT, "other") is None
    assert _finish(store, first)


def test_retry_delay_and_max_attempts_deadletter(store: HubStore, clock: list[float]) -> None:
    _enqueue(store, max_attempts=2)
    first = store.claim(TENANT, "one")
    assert first is not None
    assert store.fail(
        TENANT,
        str(first["id"]),
        "one",
        cast(int, first["lease_token"]),
        "provider unavailable",
        retry_delay=5,
    )
    assert store.claim(TENANT, "two") is None
    assert store.get_job(TENANT, str(first["id"]))["status"] == "queued"
    clock[0] += 5
    second = store.claim(TENANT, "two")
    assert second is not None
    assert store.fail(
        TENANT,
        str(second["id"]),
        "two",
        cast(int, second["lease_token"]),
        "still unavailable",
        retry_delay=0,
    )
    assert store.claim(TENANT, "three") is None
    dead = store.get_job(TENANT, str(first["id"]))
    assert dead is not None
    assert dead["status"] == "deadletter"
    assert dead["attempts"] == 2
    assert dead["error"] == "still unavailable"
    assert dead["finished_at"] == clock[0]
    assert store.summary(TENANT)["counts"] == {
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "deadletter": 1,
    }


def test_expired_last_attempt_deadletters_without_an_extra_execution(
    store: HubStore,
    clock: list[float],
) -> None:
    queued = _enqueue(store, max_attempts=1)
    assert store.claim(TENANT, "one", lease_seconds=1) is not None
    clock[0] += 1
    assert store.claim(TENANT, "two") is None
    saved = store.get_job(TENANT, str(queued["id"]))
    assert saved is not None
    assert saved["status"] == "deadletter"
    assert saved["attempts"] == saved["lease_token"] == 1


def test_tenant_scope_applies_to_every_public_read_and_write(store: HubStore) -> None:
    queued = _enqueue(store)
    job_id = str(queued["id"])
    claimed = store.claim(TENANT, "one")
    assert claimed is not None
    token = cast(int, claimed["lease_token"])
    assert store.get_job("other", job_id) is None
    assert store.list_jobs("other") == []
    assert store.events("other", job_id) == []
    assert store.events("other") == []
    assert store.claim("other", "one") is None
    assert not _finish(store, claimed, tenant="other")
    assert not store.fail("other", job_id, "one", token, "forged")
    assert not store.heartbeat("other", job_id, "one", token)
    with pytest.raises(KeyError, match="job not found"):
        store.event("other", job_id, "forged", {})
    assert store.summary("other")["total_jobs"] == 0
    assert store.summary("other")["total_events"] == 0
    second = store.enqueue("other", "different", {}, idempotency_key="request-1")
    assert second["id"] != queued["id"]
    assert _finish(store, claimed)


def test_claim_filters_and_not_before(store: HubStore, clock: list[float]) -> None:
    _enqueue(store, "later", not_before=clock[0] + 10)
    sync = store.enqueue(TENANT, "sync", {}, idempotency_key="sync")
    assert store.claim(TENANT, "one", kinds=["research"]) is None
    assert store.claim(TENANT, "one", kinds=[]) is None
    claimed = store.claim(TENANT, "one", kinds=["sync", "sync"])
    assert claimed is not None and claimed["id"] == sync["id"]
    clock[0] += 10
    research = store.claim(TENANT, "two", kinds=["research"])
    assert research is not None and research["kind"] == "research"


def test_schedule_ticks_are_concurrent_idempotent_and_coalesce_missed_periods(
    store: HubStore,
    clock: list[float],
) -> None:
    schedule = store.put_schedule(TENANT, "daily review", "research", {"version": 1}, 60)
    other = HubStore(store.path)
    barrier = Barrier(2)

    def tick(ledger: HubStore) -> int:
        barrier.wait()
        return ledger.tick_schedules(TENANT, now=clock[0])

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(tick, [store, other])) == [0, 1]
    assert store.tick_schedules(TENANT, now=clock[0] + 1) == 0
    first = store.list_jobs(TENANT)[0]
    assert first["payload"] == {"version": 1}
    assert store.list_schedules(TENANT)[0]["id"] == schedule["id"]
    clock[0] = 1_020
    assert store.tick_schedules(TENANT) == 1
    clock[0] += 600
    assert store.tick_schedules(TENANT) == 1
    assert len(store.list_jobs(TENANT)) == 3
    assert store.summary(TENANT)["enabled_schedules"] == 1
    assert store.summary(TENANT)["total_schedules"] == 1


def test_schedule_edits_disable_and_tenant_isolation(store: HubStore, clock: list[float]) -> None:
    original = store.put_schedule(TENANT, "review", "research", {"version": 1}, 60)
    store.put_schedule("other", "review", "sync", {}, 60)
    assert store.tick_schedules(TENANT) == 1
    assert store.list_jobs("other") == []
    changed = store.put_schedule(TENANT, "review", "research", {"version": 2}, 60, enabled=False)
    assert changed["id"] == original["id"]
    assert changed["enabled"] is False
    clock[0] += 120
    assert store.tick_schedules(TENANT) == 0
    store.put_schedule(TENANT, "review", "research", {"version": 2}, 60)
    assert store.tick_schedules(TENANT) == 1
    # Re-enabling during the same bucket cannot generate a second task.
    store.put_schedule(TENANT, "review", "research", {"version": 3}, 60, enabled=False)
    store.put_schedule(TENANT, "review", "research", {"version": 3}, 60)
    assert store.tick_schedules(TENANT) == 0
    assert [job["payload"] for job in store.list_jobs(TENANT)] == [{"version": 2}, {"version": 1}]
    assert store.tick_schedules("other") == 1
    assert store.list_schedules("other")[0]["kind"] == "sync"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"max_attempts": True},
        {"max_attempts": 1.5},
        {"not_before": float("nan")},
        {"not_before": float("inf")},
        {"not_before": -1},
        {"not_before": True},
        {"not_before": 10**1000},
    ],
)
def test_rejects_invalid_enqueue_numbers(store: HubStore, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _enqueue(store, **kwargs)
    assert store.list_jobs(TENANT) == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {1: "not a string key"},
        {"nested": {2: "invalid"}},
        {"number": float("nan")},
        {"number": float("inf")},
        {"bytes": b"no"},
        {"tuple": (1, 2)},
        {"large": "x" * 1_048_576},
        {"bad_unicode": "\ud800"},
    ],
)
def test_rejects_invalid_json_objects(store: HubStore, payload: object) -> None:
    with pytest.raises(ValueError):
        store.enqueue(TENANT, "research", cast(dict[str, object], payload), idempotency_key="bad")
    assert store.events(TENANT) == []


def test_rejects_circular_or_deep_json_without_writing(store: HubStore) -> None:
    circular: dict[str, object] = {}
    circular["self"] = circular
    with pytest.raises(ValueError, match="nesting"):
        store.enqueue(TENANT, "research", circular, idempotency_key="circular")
    assert store.list_jobs(TENANT) == []


@pytest.mark.parametrize("bad", ["", "  ", "x" * 129, "bad\x00value"])
def test_rejects_invalid_tenant_ids(store: HubStore, bad: str) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        store.enqueue(bad, "research", {}, idempotency_key="bad")
    with pytest.raises(ValueError, match="tenant_id"):
        store.get_job(bad, "job")


def test_validates_remaining_public_parameter_bounds(store: HubStore) -> None:
    with pytest.raises(ValueError):
        store.enqueue(TENANT, "research", {}, idempotency_key="schedule:reserved")
    with pytest.raises(ValueError):
        store.enqueue(TENANT, "research", {}, idempotency_key="x" * 257)
    for lease in (0, float("inf"), True, 86_401):
        with pytest.raises(ValueError):
            store.claim(TENANT, "worker", lease_seconds=lease)
    for limit in (0, -1, 1001, True):
        with pytest.raises(ValueError):
            store.list_jobs(TENANT, limit=limit)
        with pytest.raises(ValueError):
            store.events(TENANT, limit=limit)
    for interval in (0, float("nan"), float("inf"), True, 31_536_001):
        with pytest.raises(ValueError):
            store.put_schedule(TENANT, "name", "research", {}, interval)
    with pytest.raises(ValueError):
        store.put_schedule(TENANT, "name", "research", {}, 60, enabled=cast(bool, 1))
    with pytest.raises(ValueError):
        store.tick_schedules(TENANT, now=float("nan"))
    with pytest.raises(ValueError):
        store.claim(TENANT, "worker", kinds=cast(list[str], "research"))
    with pytest.raises(ValueError):
        store.claim(TENANT, "worker", kinds=["x"] * 101)


def test_invalid_completion_and_failure_do_not_mutate_live_job(store: HubStore) -> None:
    _enqueue(store)
    claimed = store.claim(TENANT, "worker")
    assert claimed is not None
    job_id, token = str(claimed["id"]), cast(int, claimed["lease_token"])
    with pytest.raises(ValueError):
        store.complete(TENANT, job_id, "worker", token, {"bad": float("nan")})
    with pytest.raises(ValueError):
        store.fail(TENANT, job_id, "worker", token, "error", retry_delay=-1)
    with pytest.raises(ValueError):
        store.fail(TENANT, job_id, "worker", token, "x" * 8193)
    with pytest.raises(ValueError):
        store.heartbeat(TENANT, job_id, "worker", token, lease_seconds=0)
    with pytest.raises(ValueError):
        store.complete(TENANT, job_id, "worker", True, {})
    assert store.get_job(TENANT, job_id) == claimed
    assert _finish(store, claimed)


def test_chained_completion_survives_reopen_and_lost_response(store: HubStore) -> None:
    _enqueue(store)
    claimed = store.claim(TENANT, "worker")
    assert claimed is not None
    args = (
        TENANT,
        str(claimed["id"]),
        "worker",
        cast(int, claimed["lease_token"]),
        {"plan": "ready"},
    )
    assert store.complete_and_enqueue(
        *args, next_kind="backtest", next_payload={"plan": 1}, idempotency_key="chain:backtest"
    )
    recovered = HubStore(store.path)
    assert not recovered.complete_and_enqueue(
        *args, next_kind="backtest", next_payload={"plan": 1}, idempotency_key="chain:backtest"
    )
    jobs = recovered.list_jobs(TENANT)
    assert len(jobs) == 2
    assert [(job["kind"], job["status"]) for job in jobs] == [
        ("backtest", "queued"),
        ("research", "succeeded"),
    ]
    followup = recovered.claim(TENANT, "next-worker")
    assert followup is not None and followup["kind"] == "backtest"
    links = [
        event
        for event in recovered.events(TENANT)
        if event["event_type"] == "job.followup_enqueued"
    ]
    assert len(links) == 1
    assert links[0]["payload"] == {"next_job_id": followup["id"], "next_kind": "backtest"}


def test_chained_completion_conflict_rolls_back_result_and_events(store: HubStore) -> None:
    original = _enqueue(store)
    store.enqueue(TENANT, "backtest", {"old": True}, idempotency_key="next")
    claimed = store.claim(TENANT, "worker", kinds=["research"])
    assert claimed is not None
    before = store.events(TENANT)
    with pytest.raises(ValueError, match="different request"):
        store.complete_and_enqueue(
            TENANT,
            str(original["id"]),
            "worker",
            cast(int, claimed["lease_token"]),
            {"plan": "ready"},
            next_kind="backtest",
            next_payload={"new": True},
            idempotency_key="next",
        )
    assert store.get_job(TENANT, str(original["id"])) == claimed
    assert store.events(TENANT) == before


def test_expired_chained_completion_cannot_enqueue(store: HubStore, clock: list[float]) -> None:
    _enqueue(store)
    claimed = store.claim(TENANT, "worker", lease_seconds=1)
    assert claimed is not None
    clock[0] += 1
    assert not store.complete_and_enqueue(
        TENANT,
        str(claimed["id"]),
        "worker",
        cast(int, claimed["lease_token"]),
        {},
        next_kind="backtest",
        next_payload={},
        idempotency_key="stale-followup",
    )
    assert len(store.list_jobs(TENANT)) == 1


def test_manual_deadletter_requeue_preserves_fencing_and_tenant_scope(store: HubStore) -> None:
    _enqueue(store, max_attempts=1)
    claimed = store.claim(TENANT, "worker")
    assert claimed is not None
    job_id, token = str(claimed["id"]), cast(int, claimed["lease_token"])
    assert store.fail(TENANT, job_id, "worker", token, "fix configuration first")
    with pytest.raises(KeyError, match="job not found"):
        store.requeue_deadletter("other", job_id)
    requeued = store.requeue_deadletter(TENANT, job_id)
    assert requeued["attempts"] == 0
    assert requeued["lease_token"] == token
    assert requeued["status"] == "queued"
    assert requeued["error"] is None
    with pytest.raises(ValueError, match="only deadletter"):
        store.requeue_deadletter(TENANT, job_id)
    next_attempt = store.claim(TENANT, "worker")
    assert next_attempt is not None
    assert next_attempt["lease_token"] == token + 1
    assert not _finish(store, claimed)
    assert _finish(store, next_attempt)


def test_schedule_interval_identity_preserves_float_precision(store: HubStore) -> None:
    store.put_schedule(TENANT, "review", "research", {"version": 1}, 60.00001)
    assert store.tick_schedules(TENANT) == 1
    store.put_schedule(TENANT, "review", "research", {"version": 2}, 60.00002)
    assert store.tick_schedules(TENANT) == 1
    assert store.tick_schedules(TENANT) == 0
    assert len(store.list_jobs(TENANT)) == 2


def test_chained_completion_requires_full_followup_contract_and_distinct_job(
    store: HubStore,
) -> None:
    queued = store.enqueue(TENANT, "research", {}, idempotency_key="original")
    claimed = store.claim(TENANT, "worker")
    assert claimed is not None
    job_id, token = str(queued["id"]), cast(int, claimed["lease_token"])
    with pytest.raises(ValueError, match="require next_kind"):
        store.complete_and_enqueue(TENANT, job_id, "worker", token, {}, next_payload={})
    with pytest.raises(ValueError, match="requires next_payload"):
        store.complete_and_enqueue(TENANT, job_id, "worker", token, {}, next_kind="research")
    with pytest.raises(ValueError, match="must not reference"):
        store.complete_and_enqueue(
            TENANT,
            job_id,
            "worker",
            token,
            {},
            next_kind="research",
            next_payload={},
            idempotency_key="original",
        )
    assert not store.complete_and_enqueue(
        "other",
        job_id,
        "worker",
        token,
        {},
        next_kind="research",
        next_payload={},
        idempotency_key="cross-tenant-next",
    )
    assert store.get_job(TENANT, job_id) == claimed
    assert store.list_jobs("other") == []


def test_busy_deferral_preserves_failure_budget_but_never_rewinds_fencing(
    store: HubStore,
    clock: list[float],
) -> None:
    _enqueue(store, max_attempts=1)
    first = store.claim(TENANT, "one")
    assert first is not None
    job_id, token = str(first["id"]), cast(int, first["lease_token"])
    assert store.defer(TENANT, job_id, "one", token, retry_delay=5)
    deferred = store.get_job(TENANT, job_id)
    assert deferred is not None
    assert deferred["status"] == "queued"
    assert deferred["attempts"] == 0
    assert deferred["lease_token"] == token
    assert clock[0] + 5 <= deferred["not_before"] <= clock[0] + 6
    assert deferred["worker_id"] is None and deferred["lease_until"] is None
    assert not store.defer(TENANT, job_id, "one", token)
    assert store.claim(TENANT, "two") is None
    clock[0] += 7
    second = store.claim(TENANT, "two")
    assert second is not None
    assert second["attempts"] == 1 and second["lease_token"] == token + 1
    assert not store.defer(TENANT, job_id, "one", token)
    assert not store.defer("other", job_id, "two", cast(int, second["lease_token"]))
    assert store.fail(TENANT, job_id, "two", cast(int, second["lease_token"]), "real failure")
    assert store.get_job(TENANT, job_id)["status"] == "deadletter"
    assert [event["event_type"] for event in store.events(TENANT)].count("job.deferred") == 1


def test_expired_owner_cannot_refund_an_attempt_using_defer(
    store: HubStore,
    clock: list[float],
) -> None:
    _enqueue(store)
    claimed = store.claim(TENANT, "one", lease_seconds=1)
    assert claimed is not None
    clock[0] += 1
    assert not store.defer(TENANT, str(claimed["id"]), "one", cast(int, claimed["lease_token"]))
    saved = store.get_job(TENANT, str(claimed["id"]))
    assert saved is not None and saved["attempts"] == 1
