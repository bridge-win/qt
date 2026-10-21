"""File-owned configuration. Models never receive credentials or set risk policy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROLES = ("research", "analysis", "backtest", "forecast", "risk_review", "log_review")
KINDS = (*ROLES, "pipeline", "discover", "monitor", "execute", "import_context")


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    instruction: str = Field(max_length=8000)


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    kind: str
    interval_seconds: int = Field(ge=15, le=604800)
    payload: dict = Field(default_factory=dict)
    enabled: bool = True


class HubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    tenant_id: str = Field(default="personal", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    mode: Literal["paper", "testnet", "live"] = "paper"
    auto_execute: bool = False
    max_order_quote: float = Field(default=25, gt=0, le=1000)
    max_position_quote: float = Field(default=100, gt=0, le=10000)
    max_daily_quote: float = Field(default=100, gt=0, le=10000)
    minimum_confidence: float = Field(default=0.8, ge=0.5, le=1)
    minimum_prediction_edge: float = Field(default=0.08, ge=0.01, le=0.5)
    stop_loss_fraction: float = Field(default=0.10, gt=0, le=0.5)
    lease_seconds: int = Field(default=240, ge=60, le=1800)
    codex_timeout: int = Field(default=150, ge=10, le=600)
    context_budget_chars: int = Field(default=16000, ge=2000, le=65536)
    roles: dict[str, Role]
    schedules: list[Schedule] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, value: dict[str, Role]) -> dict[str, Role]:
        if set(value) != set(ROLES):
            raise ValueError("configure exactly the six documented agent roles")
        for role in value.values():
            if role.model and not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,100}", role.model):
                raise ValueError("invalid model identifier")
        return value

    @field_validator("schedules")
    @classmethod
    def validate_schedules(cls, value: list[Schedule]) -> list[Schedule]:
        if len({item.name for item in value}) != len(value):
            raise ValueError("schedule names must be unique")
        if any(item.kind not in KINDS or item.kind == "execute" for item in value):
            raise ValueError("invalid or unsafe scheduled job kind")
        return value


def load_config(path: Path | None = None) -> HubConfig:
    source = path or Path(os.environ.get("QT_AGENT_HUB_CONFIG", "config/agent-hub.yaml"))
    return HubConfig.model_validate(yaml.safe_load(source.read_text()))


def state_root() -> Path:
    return Path(os.environ.get("QT_AGENT_HUB_STATE", "data/runtime/agent-hub"))


def files_root() -> Path:
    return Path(os.environ.get("QT_AGENT_HUB_FILES", "data/agent-hub-files"))
