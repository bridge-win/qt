"""Typed service contracts for durable platform control state."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class CommandStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommandType(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    RECONCILE = "reconcile"
    NOOP = "noop"


class WorkerStatus(str, Enum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    FAILED = "failed"


class CommandView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    owner_id: str
    command_type: CommandType
    target: str
    payload: dict[str, object]
    idempotency_key: str
    status: CommandStatus
    attempts: int
    max_attempts: int
    available_at: AwareDatetime
    claim_owner: str | None
    claim_token: UUID | None
    claim_expires_at: AwareDatetime | None
    result: dict[str, object] | None
    error: str | None
    version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class LeaseGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource_type: str
    resource_id: str
    owner_id: str
    fencing_token: int
    expires_at: AwareDatetime


class WorkerHeartbeatView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    role: str
    instance_id: str
    status: WorkerStatus
    version: str
    details: dict[str, object]
    last_seen_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AuditEventView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    actor_id: str
    action: str
    target_type: str
    target_id: str
    correlation_id: str
    details: dict[str, object]
    created_at: AwareDatetime
