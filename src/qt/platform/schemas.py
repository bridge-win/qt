"""Typed service contracts for durable platform control state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeAlias, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_serializer, field_validator
from typing_extensions import TypeAliasType

if TYPE_CHECKING:
    ImmutableDetailsValue: TypeAlias = (
        str
        | int
        | float
        | bool
        | None
        | Mapping[str, "ImmutableDetailsValue"]
        | tuple["ImmutableDetailsValue", ...]
        | frozenset["ImmutableDetailsValue"]
    )
else:
    ImmutableDetailsValue = TypeAliasType(
        "ImmutableDetailsValue",
        str
        | int
        | float
        | bool
        | None
        | Mapping[str, "ImmutableDetailsValue"]
        | tuple["ImmutableDetailsValue", ...]
        | frozenset["ImmutableDetailsValue"],
    )


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
    STOPPED = "stopped"
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


class _ImmutableDetailsView(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    details: Mapping[str, ImmutableDetailsValue]

    @field_validator("details", mode="before")
    @classmethod
    def freeze_input_details(cls, details: object) -> object:
        if not isinstance(details, Mapping):
            return details
        return _freeze_mapping(details)

    @field_validator("details", mode="after")
    @classmethod
    def freeze_validated_details(
        cls,
        details: Mapping[str, ImmutableDetailsValue],
    ) -> Mapping[str, ImmutableDetailsValue]:
        return _freeze_mapping(cast(Mapping[object, object], details))

    @field_serializer("details")
    def serialize_details(
        self,
        details: Mapping[str, ImmutableDetailsValue],
    ) -> dict[str, object]:
        return {key: _serialize_detail(value) for key, value in details.items()}


class WorkerHeartbeatView(_ImmutableDetailsView):
    id: UUID
    role: str
    instance_id: str
    status: WorkerStatus
    version: str
    last_seen_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AuditEventView(_ImmutableDetailsView):
    id: UUID
    actor_id: str
    action: str
    target_type: str
    target_id: str
    correlation_id: str
    created_at: AwareDatetime


def _freeze_mapping(values: Mapping[object, object]) -> Mapping[str, ImmutableDetailsValue]:
    frozen: dict[str, ImmutableDetailsValue] = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise ValueError("details mappings require string keys")
        frozen[key] = _freeze_detail(value)
    return MappingProxyType(frozen)


def _freeze_detail(value: object) -> ImmutableDetailsValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, Set):
        return frozenset(_freeze_detail(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_detail(item) for item in value)
    raise ValueError(f"unsupported details value: {type(value).__name__}")


def _serialize_detail(value: ImmutableDetailsValue) -> object:
    if isinstance(value, Mapping):
        return {key: _serialize_detail(item) for key, item in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [_serialize_detail(item) for item in value]
    return value
