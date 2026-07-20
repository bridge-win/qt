from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from qt.platform.commands import CommandRepository, StaleCommandClaimError
from qt.platform.config import PlatformSettings
from qt.platform.database import create_platform_engine, create_session_factory
from qt.platform.models import Base
from qt.platform.schemas import CommandStatus, CommandType, CommandView


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = PlatformSettings(
        platform_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'commands.db'}",
        database_echo=False,
        command_lease_seconds=30,
        worker_stale_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )
    database_engine = create_platform_engine(settings)
    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def repository(engine: Engine, clock: MutableClock) -> CommandRepository:
    return CommandRepository(create_session_factory(engine), clock=clock)


def enqueue_noop(
    repository: CommandRepository,
    *,
    idempotency_key: str = "noop-1",
    max_attempts: int = 3,
) -> CommandView:
    return repository.enqueue(
        owner_id="operator",
        command_type=CommandType.NOOP,
        target="worker",
        payload={},
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )


def require_claim(repository: CommandRepository, *, worker_id: str = "worker-a") -> CommandView:
    claimed = repository.claim_next(worker_id=worker_id, lease_seconds=30)
    assert claimed is not None
    assert claimed.claim_token is not None
    return claimed


def test_enqueue_replays_existing_command(repository: CommandRepository) -> None:
    first = repository.enqueue(
        owner_id="operator",
        command_type=CommandType.START,
        target="dca",
        payload={"mode": "paper"},
        idempotency_key="start-dca-1",
    )
    replay = repository.enqueue(
        owner_id="operator",
        command_type=CommandType.STOP,
        target="different-target",
        payload={"mode": "live"},
        idempotency_key="start-dca-1",
    )

    assert replay == first
    assert replay.command_type is CommandType.START
    with pytest.raises(ValidationError):
        replay.status = CommandStatus.CANCELLED


def test_active_claim_cannot_be_claimed_twice(repository: CommandRepository) -> None:
    queued = enqueue_noop(repository)

    first = repository.claim_next(worker_id="worker-a", lease_seconds=30)
    second = repository.claim_next(worker_id="worker-b", lease_seconds=30)

    assert first is not None
    assert first.id == queued.id
    assert first.status is CommandStatus.PROCESSING
    assert first.claim_owner == "worker-a"
    assert first.claim_token is not None
    assert first.attempts == 1
    assert first.claim_expires_at == first.available_at + timedelta(seconds=30)
    assert second is None


def test_expired_claim_is_recovered_with_fresh_fencing_token(
    repository: CommandRepository,
    clock: MutableClock,
) -> None:
    enqueue_noop(repository)
    first = require_claim(repository)
    first_token = first.claim_token
    clock.advance(seconds=31)

    recovered = require_claim(repository, worker_id="worker-b")

    assert recovered.id == first.id
    assert recovered.claim_owner == "worker-b"
    assert recovered.claim_token != first_token
    assert recovered.attempts == 2
    assert recovered.claim_expires_at == clock.current + timedelta(seconds=30)


def test_command_at_attempt_limit_is_never_reclaimed(
    repository: CommandRepository,
    clock: MutableClock,
) -> None:
    enqueue_noop(repository, max_attempts=1)
    require_claim(repository)
    clock.advance(seconds=31)

    assert repository.claim_next(worker_id="worker-b", lease_seconds=30) is None


def test_complete_requires_current_unexpired_claim(
    repository: CommandRepository,
    clock: MutableClock,
) -> None:
    queued = enqueue_noop(repository)
    claimed = require_claim(repository)
    assert claimed.claim_token is not None

    with pytest.raises(StaleCommandClaimError):
        repository.complete(command_id=queued.id, claim_token=uuid4(), result={})

    clock.advance(seconds=31)
    with pytest.raises(StaleCommandClaimError):
        repository.complete(command_id=queued.id, claim_token=claimed.claim_token, result={})


def test_complete_commits_terminal_result_and_clears_claim(
    repository: CommandRepository,
) -> None:
    claimed = require_claim_after_enqueue(repository)
    assert claimed.claim_token is not None

    completed = repository.complete(
        command_id=claimed.id,
        claim_token=claimed.claim_token,
        result={"handled": True},
    )

    assert completed.status is CommandStatus.SUCCEEDED
    assert completed.result == {"handled": True}
    assert completed.error is None
    assert completed.completed_at is not None
    assert completed.claim_owner is None
    assert completed.claim_token is None
    assert completed.claim_expires_at is None
    assert repository.claim_next(worker_id="worker-b", lease_seconds=30) is None


def test_fail_schedules_bounded_retry_and_delays_claim(
    repository: CommandRepository,
    clock: MutableClock,
) -> None:
    claimed = require_claim_after_enqueue(repository)
    assert claimed.claim_token is not None

    failed = repository.fail(
        command_id=claimed.id,
        claim_token=claimed.claim_token,
        error="temporary outage",
        retry_delay_seconds=10,
    )

    assert failed.status is CommandStatus.RETRY_WAIT
    assert failed.error == "temporary outage"
    assert failed.available_at == clock.current + timedelta(seconds=10)
    assert failed.completed_at is None
    assert failed.claim_owner is None
    assert failed.claim_token is None
    assert failed.claim_expires_at is None
    assert repository.claim_next(worker_id="worker-b", lease_seconds=30) is None

    clock.advance(seconds=10)
    retry = require_claim(repository, worker_id="worker-b")
    assert retry.id == failed.id
    assert retry.attempts == 2


@pytest.mark.parametrize("retry_delay_seconds", [None, 10])
def test_fail_becomes_terminal_without_retry_or_when_attempts_are_exhausted(
    repository: CommandRepository,
    retry_delay_seconds: int | None,
) -> None:
    queued = enqueue_noop(
        repository,
        max_attempts=1 if retry_delay_seconds is not None else 3,
    )
    claimed = require_claim(repository)
    assert claimed.id == queued.id
    assert claimed.claim_token is not None

    failed = repository.fail(
        command_id=claimed.id,
        claim_token=claimed.claim_token,
        error="permanent failure",
        retry_delay_seconds=retry_delay_seconds,
    )

    assert failed.status is CommandStatus.FAILED
    assert failed.completed_at is not None
    assert failed.claim_token is None
    assert repository.claim_next(worker_id="worker-b", lease_seconds=30) is None


def test_fail_rejects_stale_claim_token(repository: CommandRepository) -> None:
    claimed = require_claim_after_enqueue(repository)

    with pytest.raises(StaleCommandClaimError):
        repository.fail(
            command_id=claimed.id,
            claim_token=uuid4(),
            error="not owned",
            retry_delay_seconds=None,
        )


def test_get_and_list_recent_return_detached_views(
    repository: CommandRepository,
    clock: MutableClock,
) -> None:
    first = enqueue_noop(repository, idempotency_key="first")
    clock.advance(seconds=1)
    second = enqueue_noop(repository, idempotency_key="second")
    clock.advance(seconds=1)
    other_owner = repository.enqueue(
        owner_id="other",
        command_type=CommandType.NOOP,
        target="worker",
        payload={},
        idempotency_key="third",
    )

    assert repository.get(first.id) == first
    assert repository.get(UUID(int=0)) is None
    assert repository.list_recent(owner_id="operator", limit=1) == [second]
    assert repository.list_recent(limit=2) == [other_owner, second]


def require_claim_after_enqueue(repository: CommandRepository) -> CommandView:
    enqueue_noop(repository)
    return require_claim(repository)
