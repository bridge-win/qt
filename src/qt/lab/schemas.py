"""Pydantic API models for the research-lab bounded backend."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ParameterSpec(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    value: float | int | str | bool
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    description: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _range_is_consistent(self) -> ParameterSpec:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter minimum must not exceed maximum")
        if isinstance(self.value, bool) and (self.minimum is not None or self.maximum is not None):
            raise ValueError("boolean parameters cannot have numeric bounds")
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            if self.minimum is not None and self.value < self.minimum:
                raise ValueError("parameter value is below its minimum")
            if self.maximum is not None and self.value > self.maximum:
                raise ValueError("parameter value is above its maximum")
        return self


class IndicatorRef(StrictModel):
    indicator: str = Field(min_length=1, max_length=100)
    timeframe: str = Field(default="current", pattern=r"^(current|[1-9][0-9]*[mhdw])$")
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)


class ComparisonRule(StrictModel):
    kind: Literal["comparison"]
    left: IndicatorRef
    comparator: Literal[">", ">=", "<", "<=", "=="]
    right: float | IndicatorRef | Annotated[str, Field(pattern=r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")]


class CrossRule(StrictModel):
    kind: Literal["cross"]
    left: IndicatorRef
    right: IndicatorRef
    direction: Literal["above", "below"]
    lookback_bars: int = Field(default=1, ge=1, le=5)


class SustainedRule(StrictModel):
    kind: Literal["sustained_for"]
    child: RuleNode
    bars: int = Field(ge=1, le=1000)


class RuleGroup(StrictModel):
    kind: Literal["and", "or", "not"]
    children: list[RuleNode] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _not_has_one_child(self) -> RuleGroup:
        if self.kind == "not" and len(self.children) != 1:
            raise ValueError("NOT requires exactly one child")
        return self


RuleNode = Annotated[
    ComparisonRule | CrossRule | SustainedRule | RuleGroup,
    Field(discriminator="kind"),
]
RuleGroup.model_rebuild()
SustainedRule.model_rebuild()


class EnsembleMember(StrictModel):
    strategy_version_id: str = Field(min_length=1, max_length=64)
    target_weight: float = Field(gt=0, le=1)


class RegimeBranch(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    when: RuleNode
    strategy_version_id: str = Field(min_length=1, max_length=64)


class StrategyContent(StrictModel):
    mode: Literal["builtin", "rules", "ensemble", "regime_switch", "plugin"] = Field(
        validation_alias=AliasChoices("mode", "kind")
    )
    parameters: list[ParameterSpec] = Field(default_factory=list, max_length=80)
    entry_rule: RuleNode | None = None
    exit_rule: RuleNode | None = None
    ensemble: list[EnsembleMember] = Field(default_factory=list)
    regimes: list[RegimeBranch] = Field(default_factory=list)
    source_code: str | None = Field(
        default=None,
        max_length=100_000,
        validation_alias=AliasChoices("source_code", "code"),
    )
    plugin_api_version: str | None = Field(default=None, max_length=32)
    builtin_identity: str | None = Field(default=None, max_length=200)
    builtin_version: str | None = Field(default=None, max_length=100)
    builtin_parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_web_shape(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if normalized.get("kind") == "python":
            normalized["kind"] = "plugin"
        rules = normalized.pop("rules", None)
        if isinstance(rules, dict):
            normalized.setdefault("entry_rule", rules.get("entry"))
            normalized.setdefault("exit_rule", rules.get("exit"))
        raw_parameters = normalized.get("parameters")
        if isinstance(raw_parameters, dict):
            normalized["parameters"] = [
                {"name": name, "description": f"Parameter {name}", **raw}
                if isinstance(raw, dict)
                else {"name": name, "value": raw, "description": f"Parameter {name}"}
                for name, raw in raw_parameters.items()
            ]
        _normalize_rule_kinds(normalized.get("entry_rule"))
        _normalize_rule_kinds(normalized.get("exit_rule"))
        return normalized

    @model_validator(mode="after")
    def _mode_requires_real_content(self) -> StrategyContent:
        if self.mode == "builtin" and not self.builtin_identity:
            raise ValueError("built-in clones require the exact builtin_identity")
        if self.mode == "rules" and (self.entry_rule is None or self.exit_rule is None):
            raise ValueError("rules strategies require entry_rule and exit_rule")
        if self.mode == "ensemble" and not 2 <= len(self.ensemble) <= 3:
            raise ValueError("ensembles require two or three strategy versions")
        if self.mode == "regime_switch" and not 1 <= len(self.regimes) <= 3:
            raise ValueError("regime switching requires one to three branches")
        if self.mode == "plugin" and (not self.source_code or not self.plugin_api_version):
            raise ValueError("plugins require source_code and plugin_api_version")
        if self.mode != "plugin" and self.source_code is not None:
            raise ValueError("source_code is only valid for plugins")
        return self


class CreateStrategyRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    clone_builtin_id: str | None = Field(default=None, max_length=80, alias="base_strategy_id")
    content: StrategyContent | None = None

    @model_validator(mode="before")
    @classmethod
    def _support_web_content_at_top_level(cls, value: object) -> object:
        if not isinstance(value, dict) or "content" in value:
            return value
        content_keys = {
            "kind",
            "mode",
            "rules",
            "parameters",
            "code",
            "source_code",
            "ensemble",
            "regimes",
            "plugin_api_version",
        }
        if content_keys.intersection(value):
            normalized = dict(value)
            normalized["content"] = {
                key: normalized.pop(key) for key in content_keys if key in normalized
            }
            return normalized
        return value


class CreateVersionRequest(StrictModel):
    expected_revision: int = Field(
        ge=0,
        validation_alias=AliasChoices("expected_revision", "expected_version"),
    )
    message: str = Field(default="", max_length=500)
    content: StrategyContent

    @model_validator(mode="before")
    @classmethod
    def _support_web_content_at_top_level(cls, value: object) -> object:
        if not isinstance(value, dict) or "content" in value:
            return value
        content_keys = {
            "kind",
            "mode",
            "rules",
            "parameters",
            "code",
            "source_code",
            "ensemble",
            "regimes",
            "plugin_api_version",
        }
        normalized = dict(value)
        normalized["content"] = {
            key: normalized.pop(key) for key in content_keys if key in normalized
        }
        return normalized


class ValidateStrategyRequest(StrictModel):
    content: StrategyContent
    sample_values: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _support_web_content_at_top_level(cls, value: object) -> object:
        if not isinstance(value, dict) or "content" in value:
            return value
        content_keys = {
            "kind",
            "mode",
            "rules",
            "parameters",
            "code",
            "source_code",
            "ensemble",
            "regimes",
            "plugin_api_version",
        }
        normalized = dict(value)
        normalized["content"] = {
            key: normalized.pop(key) for key in content_keys if key in normalized
        }
        return normalized


class NoteCreateRequest(StrictModel):
    body: str = Field(min_length=1, max_length=20_000)
    entity_type: Literal["strategy_version", "experiment", "result"] = "strategy_version"
    entity_id: str = Field(min_length=1, max_length=100)
    strategy_version_id: str | None = Field(default=None, min_length=1, max_length=64)
    experiment_id: str | None = Field(default=None, max_length=100)
    result_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _bind_entity_to_immutable_version(self) -> NoteCreateRequest:
        if self.entity_type == "strategy_version":
            self.strategy_version_id = self.entity_id
        if not self.strategy_version_id:
            raise ValueError(
                "notes must include the immutable strategy_version_id used by the research entity"
            )
        if self.entity_type == "experiment":
            self.experiment_id = self.entity_id
        if self.entity_type == "result":
            self.result_id = self.entity_id
        return self


class NoteUpdateRequest(StrictModel):
    body: str = Field(min_length=1, max_length=20_000)


class CompareRequest(StrictModel):
    result_ids: list[str] = Field(min_length=2, max_length=20, alias="experiment_ids")
    max_points: int = Field(default=500, ge=10, le=10_000)


class OptimizationRequest(StrictModel):
    strategy_version_id: str = Field(min_length=1, max_length=64)
    experiment: dict[str, object]
    search_space: dict[str, list[float | int | str | bool]] = Field(min_length=1, max_length=30)
    sampler: Literal["grid", "random", "tpe"]
    objective: Literal["sharpe", "calmar", "net_return"]
    budget: int = Field(default=50, ge=1, le=500)
    seed: int = Field(default=7, ge=0)

    @model_validator(mode="after")
    def _search_space_is_bounded(self) -> OptimizationRequest:
        combinations = 1
        for name, values in self.search_space.items():
            if not values:
                raise ValueError(f"search space {name} is empty")
            combinations *= len(values)
            if combinations > 100_000:
                raise ValueError("search space has more than 100000 combinations")
        if self.sampler == "grid" and self.budget > combinations:
            raise ValueError("grid budget cannot exceed parameter combinations")
        return self


class ValidationRequest(StrictModel):
    strategy_version_id: str = Field(min_length=1, max_length=64)
    experiment: dict[str, object]
    profile: Literal["quick", "standard"] = "standard"
    seed: int = Field(default=7, ge=0)
    train_bars: int = Field(default=720, ge=20)
    validation_bars: int = Field(default=240, ge=10)
    test_bars: int = Field(default=240, ge=10)
    folds: int = Field(default=3, ge=1, le=20)
    purge_bars: int = Field(default=1, ge=0, le=10_000)
    embargo_bars: int = Field(default=0, ge=0, le=10_000)
    cscv_diagnostic: bool = False
    candidate_parameters: list[dict[str, float | int | str | bool]] = Field(
        default_factory=lambda: [dict[str, float | int | str | bool]()],
        min_length=1,
        max_length=100,
    )


def _normalize_rule_kinds(value: object) -> None:
    if not isinstance(value, dict):
        return
    kind = value.get("kind")
    if kind == "crosses_above":
        value["kind"] = "cross"
        value["direction"] = "above"
    elif kind == "crosses_below":
        value["kind"] = "cross"
        value["direction"] = "below"
    elif kind == "signal":
        signal = value.pop("signal", None)
        if isinstance(signal, dict):
            value["kind"] = "comparison"
            value["left"] = signal
            value.setdefault("comparator", ">")
            value.setdefault("right", 0)
    for child in value.get("children", []):
        _normalize_rule_kinds(child)
    _normalize_rule_kinds(value.get("child"))
