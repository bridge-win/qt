"""Bounded durable research worker."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread
from typing import TypeAlias

from qt.research.repository import JsonDict, ResearchRepository

Progress: TypeAlias = Callable[[str, int], None]
Cancelled: TypeAlias = Callable[[], bool]
Executor: TypeAlias = Callable[[JsonDict, Progress, Cancelled], JsonDict]


class ResearchWorker:
    def __init__(
        self,
        repository: ResearchRepository,
        *,
        worker_id: str,
        executor: Executor,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        self.repository = repository
        self.worker_id = worker_id.strip()
        self.executor = executor

    def run_once(self) -> bool:
        job = self.repository.claim_next(self.worker_id)
        if job is None:
            return False
        job_id = str(job["job_id"])
        attempt_id = str(job["attempt_id"])
        self.repository.heartbeat(self.worker_id)
        if self.repository.is_cancel_requested(job_id, attempt_id=attempt_id):
            self.repository.cancel_running(job_id, attempt_id=attempt_id)
            return True

        def progress(stage: str, value: int) -> None:
            if self.repository.is_cancel_requested(job_id, attempt_id=attempt_id):
                raise ResearchCancelledError
            self.repository.heartbeat(self.worker_id)
            self.repository.update_progress(job_id, stage, value, attempt_id=attempt_id)

        def cancelled() -> bool:
            return self.repository.is_cancel_requested(job_id, attempt_id=attempt_id)

        stop_lease = Event()

        def renew_lease() -> None:
            while not stop_lease.wait(10):
                if not self.repository.heartbeat_attempt(job_id, attempt_id=attempt_id):
                    return
                self.repository.heartbeat(self.worker_id)

        lease_thread = Thread(target=renew_lease, name=f"research-lease-{job_id}", daemon=True)
        lease_thread.start()

        try:
            spec = job.get("spec")
            if not isinstance(spec, dict):
                raise ValueError("job spec must be an object")
            # The durable spec remains immutable.  Dispatchers may use this
            # transient identifier to associate child records (for example,
            # Optuna trials) with the common queue job.
            execution_spec = dict(spec)
            execution_spec["_job_id"] = job_id
            result = self.executor(execution_spec, progress, cancelled)
            if cancelled():
                self.repository.cancel_running(job_id, attempt_id=attempt_id)
            else:
                self.repository.complete(job_id, result, attempt_id=attempt_id)
        except ResearchCancelledError:
            self.repository.cancel_running(job_id, attempt_id=attempt_id)
        except Exception as error:
            if self.repository.is_cancel_requested(job_id, attempt_id=attempt_id):
                self.repository.cancel_running(job_id, attempt_id=attempt_id)
            else:
                self.repository.fail(
                    job_id,
                    f"{type(error).__name__}: {str(error) or 'research job failed'}",
                    attempt_id=attempt_id,
                )
        finally:
            stop_lease.set()
            lease_thread.join(timeout=1)
        return True


class ResearchCancelledError(RuntimeError):
    pass
