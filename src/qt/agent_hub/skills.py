"""Server-owned, versioned role manifests; these never execute model-supplied code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from qt.agent_hub.codex import RESPONSE_SCHEMA
from qt.agent_hub.config import ROLES

CONTEXT_SCHEMA = {
    "type": "object",
    "required": ["query", "knowledge", "memories", "references"],
    "properties": {
        "query": {"type": "string"},
        "knowledge": {"type": "array"},
        "memories": {"type": "array"},
        "references": {"type": "array"},
    },
    "additionalProperties": True,
}
_COMMON = {"context.search", "context.read_chunk", "market.quote"}
ROLE_TOOLS = {
    "research": _COMMON | {"research.collect"},
    "analysis": _COMMON | {"market.history"},
    "backtest": _COMMON | {"experiment.run", "market.history"},
    "forecast": _COMMON | {"market.history"},
    "risk_review": _COMMON | {"proposal.verify"},
    "log_review": {"context.search", "context.read_chunk", "events.read"},
}


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    role: str
    description: str = Field(min_length=1, max_length=2000)
    allowed_tools: list[str] = Field(max_length=20)
    input_schema: str = "context.v2"
    output_schema: str = "codex.response.v1"
    evaluation_tags: list[str] = Field(min_length=1, max_length=20)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError("unknown skill role")
        return value

    @field_validator("evaluation_tags", "allowed_tools")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        if any(not label or len(label) > 100 for label in value) or len(value) != len(set(value)):
            raise ValueError("invalid or duplicated skill labels")
        return value

    def resolved(self) -> dict:
        if self.input_schema != "context.v2" or self.output_schema != "codex.response.v1":
            raise ValueError("unsupported skill schema version")
        if not set(self.allowed_tools).issubset(ROLE_TOOLS[self.role]):
            raise ValueError("skill requests a tool outside the server role policy")
        record = self.model_dump()
        record["schemas"] = {"input": CONTEXT_SCHEMA, "output": RESPONSE_SCHEMA}
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {**record, "sha256": hashlib.sha256(raw.encode()).hexdigest()}


class SkillRegistry:
    """Load only administrator-selected manifests and fail closed on missing roles.

    ``allowed_tools`` authorize runtime-owned domain operations. Codex still has
    shell, network, plugin and generic filesystem tools disabled. A YAML file
    cannot add a new operation to the allowlist compiled into this module.
    """

    def __init__(self, directory: Path | None = None) -> None:
        config = Path(os.environ.get("QT_AGENT_HUB_CONFIG", "config/agent-hub.yaml"))
        directory = directory or Path(
            os.environ.get("QT_AGENT_HUB_SKILLS_DIR", str(config.parent / "skills"))
        )
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("skill directory must be a regular controlled directory")
        self._skills: dict[str, dict] = {}
        for role in ROLES:
            path = directory / f"{role}.yaml"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise ValueError(f"skill manifest unavailable for {role}")
            manifest = SkillManifest.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            if manifest.role != role:
                raise ValueError("skill filename and role do not match")
            self._skills[role] = manifest.resolved()

    def describe(self, role: str) -> dict:
        if role not in self._skills:
            raise ValueError("unknown skill role")
        # Callers must not be able to mutate the registry's trusted policy.
        return json.loads(json.dumps(self._skills[role], ensure_ascii=False))

    def summary(self, role: str) -> dict:
        manifest = self.describe(role)
        return {key: value for key, value in manifest.items() if key != "schemas"}

    def require_tool(self, role: str, tool: str) -> None:
        if tool not in self.describe(role)["allowed_tools"]:
            raise ValueError("skill_tool_not_allowed")

    def list(self) -> list[dict]:
        return [self.summary(role) for role in ROLES]
