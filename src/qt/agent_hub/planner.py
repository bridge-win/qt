"""Bounded, server-executed research loop. Models request typed tools; they never run them."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLANNER_TOOLS = (
    "search_evidence",
    "read_chunk",
    "market_history",
    "fee_schedule",
    "validate_strategy",
    "forecast_ledger_lookup",
)


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal[
        "search_evidence",
        "read_chunk",
        "market_history",
        "fee_schedule",
        "validate_strategy",
        "forecast_ledger_lookup",
    ]
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def bounded_args(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 16:
            raise ValueError("too_many_tool_args")
        return value


class PlannerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["continue", "propose", "hold", "abstain"] = "hold"
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=4)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    hypotheses: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(default="", max_length=4000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    summary: str = Field(default="", max_length=8000)
    side: Literal["BUY", "SELL", "HOLD"] | None = None
    evidence: list[str] = Field(default_factory=list, max_length=32)


def parse_planner(value: dict[str, Any]) -> PlannerResponse:
    allowed = set(PlannerResponse.model_fields)
    payload = {key: item for key, item in dict(value).items() if key in allowed}
    if "decision" not in payload and (value.get("summary") or value.get("rationale")):
        payload["decision"] = "hold"
    if not payload.get("evidence") and payload.get("evidence_ids"):
        payload["evidence"] = list(payload["evidence_ids"])
    if payload.get("decision") == "propose" and not payload.get("side"):
        payload["side"] = value.get("side") or "HOLD"
    try:
        return PlannerResponse.model_validate(payload)
    except Exception:
        return PlannerResponse(decision="hold", summary=str(value.get("summary") or "")[:8000])


class ResearchLoop:
    """Server-owned planner: validate, execute allowlisted tools, stop on budget."""

    def __init__(self, *, max_rounds: int = 4, max_tool_calls: int = 12) -> None:
        self.max_rounds = max(1, min(8, max_rounds))
        self.max_tool_calls = max(1, min(32, max_tool_calls))

    def run(
        self,
        *,
        invoke,
        execute_tool,
        role: str,
        task: str,
        context: dict[str, Any],
        cancel_requested=None,
    ) -> dict[str, Any]:
        rounds: list[dict[str, Any]] = []
        tools_used = 0
        working = dict(context)
        last = PlannerResponse()
        raw: dict[str, Any] = {}
        for round_index in range(self.max_rounds):
            if cancel_requested is not None and cancel_requested():
                break
            answer = invoke(role, task, working)
            raw = answer if isinstance(answer, dict) else {}
            parsed = parse_planner(raw)
            last = parsed
            record = {
                "round": round_index + 1,
                "decision": parsed.decision,
                "tool_requests": [item.model_dump() for item in parsed.tool_requests],
                "evidence_ids": parsed.evidence_ids,
            }
            if parsed.decision != "continue" or not parsed.tool_requests:
                rounds.append(record)
                break
            observations = []
            for request in parsed.tool_requests:
                if tools_used >= self.max_tool_calls:
                    observations.append({"tool": request.tool, "error": "tool_budget_exhausted"})
                    break
                tools_used += 1
                observations.append(
                    {"tool": request.tool, "result": execute_tool(request.tool, request.args)}
                )
            working = {**working, "tool_observations": observations, "planner_round": round_index + 1}
            record["observations"] = [
                {"tool": item["tool"], "ok": "error" not in item} for item in observations
            ]
            rounds.append(record)
        payload = dict(raw)
        payload.update(last.model_dump())
        payload.update(
            {
                "summary": last.summary or last.rationale or raw.get("summary")
                or f"{role} completed bounded research loop",
                "decision": "propose" if last.decision == "propose" else (raw.get("decision") or "hold"),
                "evidence": last.evidence or last.evidence_ids or raw.get("evidence") or [],
                "planner": {
                    "rounds": rounds,
                    "tool_calls": tools_used,
                    "max_rounds": self.max_rounds,
                    "max_tool_calls": self.max_tool_calls,
                    "stopped": last.decision != "continue" or tools_used >= self.max_tool_calls,
                },
            }
        )
        return payload


def critic_context(proposal: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Clean context for the critic: proposal + citations, no research scratchpad."""
    return {
        "trade_proposal": proposal,
        "evidence": evidence,
        "instruction": "Challenge the proposal. Do not inherit the research chain-of-thought.",
    }
