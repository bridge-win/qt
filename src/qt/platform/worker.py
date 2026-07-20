"""Bounded durable-command processing for the trading worker process."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from threading import Event
from typing import Protocol, cast
from uuid import UUID

from qt.platform.commands import StaleCommandClaimError
from qt.platform.schemas import (
    CommandType,
    CommandView,
    WorkerHeartbeatView,
    WorkerStatus,
)

Handler = Callable[[CommandView], Mapping[str, object]]

_MAX_COMMAND_LEASE_SECONDS = 3600
_MAX_POLL_SECONDS = 300.0
_MAX_RETRY_SECONDS = 3600
_PHASE_TWO_ERROR = "Phase 2 lifecycle command '{command_type}' is not enabled"


class CommandStore(Protocol):
    def claim_next(self, *, worker_id: str, lease_seconds: int) -> CommandView | None: ...

    def complete(
        self,
        *,
        command_id: UUID,
        claim_token: UUID,
        result: Mapping[str, object],
    ) -> CommandView: ...

    def fail(
        self,
        *,
        command_id: UUID,
        claim_token: UUID,
        error: str,
        retry_delay_seconds: int | None,
    ) -> CommandView: ...


class OperationsStore(Protocol):
    def record_heartbeat(
        self,
        *,
        role: str,
        instance_id: str,
        status: WorkerStatus,
        version: str,
        details: Mapping[str, object],
    ) -> WorkerHeartbeatView: ...


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float) -> bool: ...


class TradingWorker:
    """Claim and transition at most one durable command per processing cycle."""

    def __init__(
        self,
        commands: CommandStore,
        operations: OperationsStore,
        *,
        worker_id: str,
        version: str,
        command_lease_seconds: int,
        poll_seconds: float,
        retry_base_seconds: int = 1,
        retry_max_seconds: int = 60,
        handlers: Mapping[CommandType, Handler] | None = None,
        stop_event: StopEvent | None = None,
    ) -> None:
        self._worker_id = _required_text(worker_id, "worker_id", max_length=128)
        self._version = _required_text(version, "version", max_length=64)
        if not 5 <= command_lease_seconds <= _MAX_COMMAND_LEASE_SECONDS:
            raise ValueError(
                f"command_lease_seconds must be between 5 and {_MAX_COMMAND_LEASE_SECONDS}"
            )
        if not math.isfinite(poll_seconds) or not 0 < poll_seconds <= _MAX_POLL_SECONDS:
            raise ValueError(f"poll_seconds must be greater than 0 and at most {_MAX_POLL_SECONDS:g}")
        if not 1 <= retry_base_seconds <= _MAX_RETRY_SECONDS:
            raise ValueError(
                f"retry_base_seconds must be between 1 and {_MAX_RETRY_SECONDS}"
            )
        if not retry_base_seconds <= retry_max_seconds <= _MAX_RETRY_SECONDS:
            raise ValueError(
                "retry_max_seconds must be at least retry_base_seconds "
                f"and at most {_MAX_RETRY_SECONDS}"
            )

        configured_handlers: dict[CommandType, Handler] = {CommandType.NOOP: _handle_noop}
        if handlers is not None:
            configured_handlers.update(handlers)

        self._commands = commands
        self._operations = operations
        self._command_lease_seconds = command_lease_seconds
        self._poll_seconds = poll_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._handlers = configured_handlers
        self._stop_event: StopEvent = stop_event if stop_event is not None else Event()

    def run_once(self) -> bool:
        """Process at most one command and report whether one was claimed."""

        self._record_heartbeat(WorkerStatus.HEALTHY, {"state": "idle"})
        command = self._commands.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._command_lease_seconds,
        )
        if command is None:
            return False

        self._execute(command)
        return True

    def run_forever(self) -> None:
        """Poll at a bounded cadence until a stop is requested."""

        self._record_heartbeat_best_effort(WorkerStatus.STARTING, {"state": "starting"})
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception as error:
                    self._record_heartbeat_best_effort(
                        WorkerStatus.DEGRADED,
                        {"state": "loop_error", "error": _format_error(error)},
                    )
                self._stop_event.wait(self._poll_seconds)
        finally:
            self._record_heartbeat_best_effort(WorkerStatus.STOPPING, {"state": "stopping"})
            self._record_heartbeat_best_effort(WorkerStatus.STOPPED, {"state": "stopped"})

    def stop(self) -> None:
        """Request shutdown; repeated requests have no additional effect."""

        if not self._stop_event.is_set():
            self._stop_event.set()

    def _execute(self, command: CommandView) -> None:
        claim_token = command.claim_token
        if claim_token is None:
            self._record_claim_lost(command.id)
            return

        handler = self._handlers.get(command.command_type)
        if handler is None:
            self._transition_failure(
                command,
                claim_token,
                error=_PHASE_TWO_ERROR.format(command_type=command.command_type.value),
                retry_delay_seconds=None,
            )
            return

        try:
            result = _json_compatible_result(handler(command))
        except Exception as error:
            self._transition_failure(
                command,
                claim_token,
                error=_format_error(error),
                retry_delay_seconds=self._retry_delay(command.attempts),
            )
            return

        try:
            self._commands.complete(
                command_id=command.id,
                claim_token=claim_token,
                result=result,
            )
        except StaleCommandClaimError:
            self._record_claim_lost(command.id)

    def _transition_failure(
        self,
        command: CommandView,
        claim_token: UUID,
        *,
        error: str,
        retry_delay_seconds: int | None,
    ) -> None:
        try:
            self._commands.fail(
                command_id=command.id,
                claim_token=claim_token,
                error=error,
                retry_delay_seconds=retry_delay_seconds,
            )
        except StaleCommandClaimError:
            self._record_claim_lost(command.id)

    def _retry_delay(self, attempt: int) -> int:
        delay = self._retry_base_seconds
        for _ in range(max(0, attempt - 1)):
            if delay >= self._retry_max_seconds:
                return self._retry_max_seconds
            delay = min(delay * 2, self._retry_max_seconds)
        return delay

    def _record_claim_lost(self, command_id: UUID) -> None:
        self._record_heartbeat(
            WorkerStatus.DEGRADED,
            {"state": "claim_lost", "command_id": str(command_id)},
        )

    def _record_heartbeat(
        self,
        status: WorkerStatus,
        details: Mapping[str, object],
    ) -> None:
        self._operations.record_heartbeat(
            role="trading",
            instance_id=self._worker_id,
            status=status,
            version=self._version,
            details=details,
        )

    def _record_heartbeat_best_effort(
        self,
        status: WorkerStatus,
        details: Mapping[str, object],
    ) -> None:
        try:
            self._record_heartbeat(status, details)
        except Exception:
            return


def _handle_noop(_command: CommandView) -> Mapping[str, object]:
    return {"handled": True}


def _json_compatible_result(result: Mapping[str, object]) -> dict[str, object]:
    encoded = json.dumps(dict(result), allow_nan=False, sort_keys=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("handler result must be a JSON object")
    return cast(dict[str, object], decoded)


def _required_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return normalized


def _format_error(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        return type(error).__name__
    return f"{type(error).__name__}: {message}"
