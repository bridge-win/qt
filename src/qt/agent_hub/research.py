"""Typed, content-addressed bridge from agents to the native research worker.

Only the HTTP service imports the scientific/native stack. A completed report is
research evidence for its exact immutable strategy and dataset, never approval
for an unrelated model-generated trade or a prediction-market forecast.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qt.agent_hub.context import identifier
from qt.agent_hub.tracing import traceparent

_DAY_MS = 86_400_000


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def native_bridge_fingerprint() -> str:
    """Same lightweight source identity in the Hub and native worker images."""
    checksum = hashlib.sha256()
    for name in ("research.py", "research_service.py", "quant_validation.py"):
        checksum.update(Path(__file__).with_name(name).read_bytes())
    return checksum.hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, frozen=True, validate_default=True
    )


class Bar(Contract):
    timestamp: int = Field(gt=0, strict=True)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_ohlc(self) -> Bar:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC bounds are inconsistent")
        if self.timestamp % _DAY_MS:
            raise ValueError("daily bar timestamp must be a UTC day boundary")
        return self


class StrategyVersion(Contract):
    version_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,64}$")
    content: dict[str, Any]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verified_content(self) -> StrategyVersion:
        if digest(self.content) != self.content_sha256:
            raise ValueError("strategy content hash mismatch")
        if self.content.get("mode") not in {"builtin", "rules"}:
            raise ValueError("research bridge supports bounded builtin and rules strategies")
        if len(json.dumps(self.content)) > 32000:
            raise ValueError("strategy content exceeds bound")
        return self


class DataSnapshot(Contract):
    source: str = Field(min_length=1, max_length=300)
    instrument: Literal["BTCUSDT"] = "BTCUSDT"
    timeframe: Literal["1d"] = "1d"
    bars_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: float = Field(gt=0)
    rows: int = Field(ge=90, le=2000)


class Costs(Contract):
    initial_cash: float = Field(default=1000, gt=0, le=1000000)
    fee_bps: float = Field(default=10, gt=0, le=1000)
    slippage_bps: Literal[0] = 0


class WalkForward(Contract):
    train_bars: int = Field(default=360, ge=20, le=1000)
    validation_bars: int = Field(default=120, ge=10, le=500)
    test_bars: int = Field(default=120, ge=10, le=500)
    folds: int = Field(default=3, ge=2, le=5)
    purge_bars: int = Field(default=1, ge=1, le=100)
    embargo_bars: int = Field(default=1, ge=1, le=100)

    def required_bars(self) -> int:
        return (
            self.train_bars
            + self.validation_bars
            + self.test_bars * self.folds
            + self.purge_bars
            + self.embargo_bars
        )


class ExperimentSpec(Contract):
    schema_version: Literal["1"] = "1"
    strategy_version: StrategyVersion
    data: DataSnapshot
    costs: Costs = Field(default_factory=Costs)
    walk_forward: WalkForward = Field(default_factory=WalkForward)
    seed: int = Field(default=7, ge=0, le=2147483647)

    @model_validator(mode="after")
    def sufficient_data(self) -> ExperimentSpec:
        if self.data.rows < self.walk_forward.required_bars():
            raise ValueError("insufficient bars for requested chronological folds")
        return self


class ValidationRequest(Contract):
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    job_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    spec: ExperimentSpec
    bars: list[Bar] = Field(min_length=90, max_length=2000)

    @model_validator(mode="after")
    def verified_snapshot(self) -> ValidationRequest:
        if len(self.bars) != self.spec.data.rows:
            raise ValueError("snapshot row count mismatch")
        if digest([bar.model_dump() for bar in self.bars]) != self.spec.data.bars_sha256:
            raise ValueError("snapshot bars hash mismatch")
        if any(b.timestamp - a.timestamp != _DAY_MS for a, b in pairwise(self.bars)):
            raise ValueError("snapshot contains gaps, duplicate or out-of-order bars")
        if self.bars[-1].timestamp + _DAY_MS > self.spec.data.observed_at * 1000:
            raise ValueError("snapshot contains an unfinished or future bar")
        if self.spec.data.observed_at > time.time() + 30:
            raise ValueError("snapshot observation time is in the future")
        return self

    def input_hash(self) -> str:
        return digest(self.spec.model_dump())


class ValidationReport(Contract):
    schema_version: Literal["1"] = "1"
    status: Literal["completed"] = "completed"
    eligible_for_paper: bool
    spec: ExperimentSpec
    execution_signal: dict[str, Any]
    input_hash: str
    strategy_version_id: str
    strategy_sha256: str
    data_sha256: str
    engine: dict[str, Any]
    validation_status: Literal["strict_walk_forward"]
    integrity: dict[str, Any]
    metrics: dict[str, Any]
    folds: list[dict[str, Any]]
    statistics: dict[str, Any]
    costs: dict[str, Any]
    benchmarks: dict[str, Any]
    verdict: Literal["paper_research_candidate", "fragile"]
    failed_gates: list[str]
    limitations: list[str]
    live_ready: Literal[False] = False

    @model_validator(mode="after")
    def coherent_report(self) -> ValidationReport:
        spec = self.spec
        if (
            self.input_hash != digest(spec.model_dump())
            or self.strategy_version_id != spec.strategy_version.version_id
            or self.strategy_sha256 != spec.strategy_version.content_sha256
            or self.data_sha256 != spec.data.bars_sha256
        ):
            raise ValueError("validation report identity mismatch")
        if self.eligible_for_paper != (
            self.verdict == "paper_research_candidate" and not self.failed_gates
        ):
            raise ValueError("validation report eligibility is inconsistent")
        delayed = self.costs.get("execution_stress_oos")
        if (
            self.eligible_for_paper and delayed is not None
            and (
                not isinstance(delayed, dict)
                or delayed.get("status") != "verified"
                or not isinstance(delayed.get("metrics"), dict)
                or delayed["metrics"].get("total_return", 0) <= 0
            )
        ):
            raise ValueError("eligible report does not pass delayed execution stress")
        bootstrap = self.statistics.get("fold_bootstrap")
        if (
            self.eligible_for_paper
            and bootstrap is not None
            and (not isinstance(bootstrap, dict) or bootstrap.get("worst_return_p05", 0) <= 0)
        ):
            raise ValueError("eligible report does not pass OOS bootstrap lower bound")
        if (
            self.engine.get("name") != "nautilus_trader"
            or self.integrity.get("account_invariants") != "verified"
        ):
            raise ValueError("validation report lacks native integrity attestation")
        signal = self.execution_signal
        if (
            signal.get("strategy_version_id") != self.strategy_version_id
            or signal.get("data_sha256") != self.data_sha256
            or signal.get("strategy_sha256") != self.strategy_sha256
            or signal.get("side") not in {"BUY", "SELL", "HOLD"}
        ):
            raise ValueError("execution signal identity mismatch")
        json.dumps(self.model_dump(), allow_nan=False)
        return self


def build_default_spec(
    history: list[dict[str, Any]],
    *,
    strategy_content: dict[str, Any] | None = None,
    observed_at: float | None = None,
    walk_forward: WalkForward | None = None,
) -> ExperimentSpec:
    """Build an explicit study, not an implicit validation of an LLM proposal."""
    content = strategy_content or {
        "mode": "builtin",
        "builtin_identity": "qt:sma_crossover",
        "builtin_version": "1.0.0",
        "builtin_parameters": {"fast_window": 20, "slow_window": 60},
    }
    content_hash = digest(content)
    bars = [Bar.model_validate(row).model_dump() for row in history]
    return ExperimentSpec(
        strategy_version=StrategyVersion(
            version_id="hub-" + content_hash[:40],
            content=content,
            content_sha256=content_hash,
        ),
        data=DataSnapshot(
            source="https://data-api.binance.vision/api/v3/klines",
            bars_sha256=digest(bars),
            observed_at=observed_at or time.time(),
            rows=len(bars),
        ),
        walk_forward=walk_forward or WalkForward(),
    )


class RemoteResearch:
    """Authenticated, bounded internal HTTP client; no credential or code upload."""

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = (url or os.environ.get("QT_AGENT_HUB_RESEARCH_URL", "")).rstrip("/")
        self.token = token or os.environ.get("QT_AGENT_HUB_EXECUTOR_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": "Bearer " + self.token}
        current = traceparent()
        if current is not None:
            headers["traceparent"] = current
        return headers

    def health(self) -> dict[str, Any]:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not self.token
        ):
            raise RuntimeError("native_research_service_not_configured")
        with (
            httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client,
            client.stream(
                "GET", self.url + "/healthz", headers=self._headers()
            ) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 16384:
                    raise ValueError("native research health response exceeds bound")
        health = json.loads(data)
        if (
            not isinstance(health, dict)
            or health.get("ok") is not True
            or not isinstance(health.get("engine"), dict)
        ):
            raise ValueError("native research health is invalid")
        return health

    def run(
        self,
        tenant_id: str,
        job_id: str,
        history: list[dict[str, Any]],
        *,
        strategy: str | dict[str, Any] = "ma_trend",
    ) -> dict[str, Any]:
        if isinstance(strategy, str):
            if strategy not in {"ma_trend", "sma_crossover"}:
                raise ValueError("unsupported strategy; supply an exact builtin or rules content")
            content = None
        else:
            content = strategy
        spec = build_default_spec(history, strategy_content=content)
        request = ValidationRequest(
            tenant_id=identifier(tenant_id),
            job_id=identifier(job_id),
            spec=spec,
            bars=[Bar.model_validate(row) for row in history],
        )
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not self.token
        ):
            raise RuntimeError("native_research_service_not_configured")
        with (
            httpx.Client(timeout=180, trust_env=False, follow_redirects=False) as client,
            client.stream(
                "POST",
                self.url + "/internal/validate",
                json=request.model_dump(),
                headers=self._headers(),
            ) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ValueError("native research response exceeds bound")
        report = ValidationReport.model_validate_json(bytes(data))
        if (
            report.input_hash != request.input_hash()
            or report.strategy_sha256 != spec.strategy_version.content_sha256
            or report.strategy_version_id != spec.strategy_version.version_id
            or report.data_sha256 != spec.data.bars_sha256
        ):
            raise ValueError("native research result identity mismatch")
        return report.model_dump()


class ResolvedForecast(Contract):
    """One outcome evaluated after the decision, with immutable market identity."""

    market_id: str = Field(min_length=1, max_length=160)
    prediction: float = Field(ge=0, le=1)
    market_probability: float | None = Field(default=None, ge=0, le=1)
    outcome: Literal[0, 1]
    predicted_at: float = Field(gt=0)
    resolved_at: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> ResolvedForecast:
        if self.resolved_at <= self.predicted_at or self.resolved_at > time.time():
            raise ValueError("forecast must predate an observed resolved outcome")
        return self


def evaluate_forecasts(rows: list[ResolvedForecast]) -> dict[str, Any]:
    """Proper scoring and reliability bins; never substitute BTC strategy returns."""
    if not rows or len(rows) > 10000:
        raise ValueError("forecast evaluation requires 1..10000 resolved observations")
    if len({row.market_id for row in rows}) != len(rows):
        raise ValueError("duplicate market outcomes would inflate the evaluation sample")
    brier = sum((row.prediction - row.outcome) ** 2 for row in rows) / len(rows)
    paired = [row for row in rows if row.market_probability is not None]
    baseline = (
        sum((row.market_probability - row.outcome) ** 2 for row in paired) / len(paired)
        if paired else None
    )
    paired_brier = (
        sum((row.prediction - row.outcome) ** 2 for row in paired) / len(paired)
        if paired else None
    )
    log_loss = -sum(
        row.outcome * math.log(max(1e-12, min(1 - 1e-12, row.prediction)))
        + (1 - row.outcome) * math.log(max(1e-12, min(1 - 1e-12, 1 - row.prediction)))
        for row in rows
    ) / len(rows)
    bins = []
    for index in range(10):
        selected = [row for row in rows if min(9, int(row.prediction * 10)) == index]
        if selected:
            bins.append(
                {
                    "lower": index / 10,
                    "upper": (index + 1) / 10,
                    "count": len(selected),
                    "mean_prediction": sum(row.prediction for row in selected) / len(selected),
                    "outcome_rate": sum(row.outcome for row in selected) / len(selected),
                }
            )
    return {
        "status": "evaluated",
        "samples": len(rows),
        "brier_score": brier,
        "market_baseline_brier": baseline,
        "paired_model_brier": paired_brier,
        "paired_comparison_samples": len(paired),
        "market_baseline_coverage": len(paired) / len(rows),
        "brier_skill": 1 - paired_brier / baseline if baseline else None,
        "comparison_status": "comparable" if paired else "missing_market_baseline",
        "log_loss": log_loss,
        "calibration_bins": bins,
        "live_ready": False,
        "data_sha256": digest([row.model_dump() for row in rows]),
        "limitations": [
            "Caller-supplied resolution evidence requires source verification",
            "No causal profit or execution-quality claim",
        ],
    }
