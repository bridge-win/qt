"""Bounded durable research worker."""

from __future__ import annotations

from collections.abc import Callable
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
        if self.repository.is_cancel_requested(job_id):
            self.repository.cancel_running(job_id)
            return True

        def progress(stage: str, value: int) -> None:
            if self.repository.is_cancel_requested(job_id):
                raise ResearchCancelledError
            self.repository.update_progress(job_id, stage, value)

        def cancelled() -> bool:
            return self.repository.is_cancel_requested(job_id)

        try:
            spec = job.get("spec")
            if not isinstance(spec, dict):
                raise ValueError("job spec must be an object")
            result = self.executor(spec, progress, cancelled)
            if cancelled():
                self.repository.cancel_running(job_id)
            else:
                self.repository.complete(job_id, result)
        except ResearchCancelledError:
            self.repository.cancel_running(job_id)
        except Exception as error:
            if self.repository.is_cancel_requested(job_id):
                self.repository.cancel_running(job_id)
            else:
                self.repository.fail(
                    job_id,
                    f"{type(error).__name__}: "
                    f"{str(error) or 'research job failed'}",
                )
        return True


class ResearchCancelledError(RuntimeError):
    pass
