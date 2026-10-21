"""Contract tampering, probability scoring and the real Lab/native bridge."""

from __future__ import annotations

import math
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qt.agent_hub.research import (
    Bar,
    Costs,
    RemoteResearch,
    ResolvedForecast,
    StrategyVersion,
    ValidationReport,
    ValidationRequest,
    WalkForward,
    build_default_spec,
    digest,
    evaluate_forecasts,
)
from qt.agent_hub.research_service import NativeResearch, _AttestedExecutor, create_research_app


def history(count: int = 180) -> list[dict]:
    rows = []
    for number in range(count):
        price = 40000 * (1 + number * 0.001 + 0.08 * math.sin(number / 2))
        rows.append(
            {
                "timestamp": 1704067200000 + number * 86400000,
                "open": price * 0.999,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100.0,
            }
        )
    return rows


def request() -> ValidationRequest:
    rows = history()
    spec = build_default_spec(
        rows,
        strategy_content={
            "mode": "builtin",
            "builtin_identity": "qt:sma_crossover",
            "builtin_version": "1.0.0",
            "builtin_parameters": {"fast_window": 3, "slow_window": 8},
        },
        walk_forward=WalkForward(train_bars=60, validation_bars=30, test_bars=30, folds=2),
    )
    return ValidationRequest(tenant_id="personal", job_id="native-test", spec=spec, bars=rows)


def test_contract_rejects_tampered_data_and_strategy() -> None:
    body = request().model_dump()
    body["bars"][0]["close"] *= 1.001
    with pytest.raises(ValidationError, match="hash mismatch"):
        ValidationRequest.model_validate(body)
    version = request().spec.strategy_version.model_dump()
    version["content"]["builtin_parameters"]["fast_window"] = 2
    with pytest.raises(ValidationError, match="hash mismatch"):
        StrategyVersion.model_validate(version)


@pytest.mark.parametrize("change", ["gap", "future", "duplicate", "ohlc"])
def test_contract_rejects_invalid_temporal_and_price_data(change: str) -> None:
    body = request().model_dump()
    if change == "gap":
        body["bars"][2]["timestamp"] += 86400000
    elif change == "duplicate":
        body["bars"][2]["timestamp"] = body["bars"][1]["timestamp"]
    elif change == "future":
        body["spec"]["data"]["observed_at"] = body["bars"][-1]["timestamp"] / 1000
    else:
        body["bars"][0]["high"] = 1
    body["spec"]["data"]["bars_sha256"] = digest(body["bars"])
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(body)


def test_contract_rejects_arbitrary_code_nan_and_unrepresentable_slippage() -> None:
    with pytest.raises(ValidationError, match="bounded builtin"):
        StrategyVersion(
            version_id="p", content={"mode": "plugin"}, content_sha256=digest({"mode": "plugin"})
        )
    with pytest.raises(ValidationError):
        Bar.model_validate({**history()[0], "close": float("nan")})
    with pytest.raises(ValidationError):
        Costs(slippage_bps=5)
    with pytest.raises(ValidationError):
        build_default_spec(history(180))


def test_default_is_an_explicit_exact_strategy_contract() -> None:
    spec = build_default_spec(history(900))
    assert spec.strategy_version.content["builtin_identity"] == "qt:sma_crossover"
    assert spec.strategy_version.content["builtin_parameters"] == {
        "fast_window": 20,
        "slow_window": 60,
    }
    assert spec.walk_forward.required_bars() == 842
    assert spec.strategy_version.content_sha256 == digest(spec.strategy_version.content)


def test_resolved_probability_evaluation_is_separate_and_properly_scored() -> None:
    now = time.time()
    rows = [
        ResolvedForecast(
            market_id=str(index),
            prediction=prediction,
            market_probability=0.5,
            outcome=outcome,
            predicted_at=now - 200,
            resolved_at=now - 100,
        )
        for index, (prediction, outcome) in enumerate(((0.8, 1), (0.2, 0)))
    ]
    result = evaluate_forecasts(rows)
    assert result["brier_score"] == pytest.approx(0.04)
    assert result["market_baseline_brier"] == 0.25
    assert result["brier_skill"] == pytest.approx(0.84)
    assert result["live_ready"] is False
    assert sum(row["count"] for row in result["calibration_bins"]) == 2
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_forecasts([rows[0], rows[0]])
    with pytest.raises(ValidationError):
        ResolvedForecast(
            market_id="x",
            prediction=0.8,
            market_probability=0.5,
            outcome=1,
            predicted_at=now,
            resolved_at=now - 100,
        )


def test_service_auth_and_bounded_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QT_AGENT_HUB_EXECUTOR_TOKEN", "test-token")
    client = TestClient(create_research_app(tmp_path))
    assert client.post("/internal/validate", json=request().model_dump()).status_code == 401
    assert client.post("/internal/validate", content=b"x" * 1_000_001).status_code == 413


def test_attestation_rejects_fake_engine_and_accounting() -> None:
    class Fake:
        def execute(self, *_: object) -> dict:
            return {"engine": "fixed-function"}

    with pytest.raises(ValueError, match="unexpected research engine"):
        _AttestedExecutor(Fake()).execute({}, lambda *_: None, lambda: False)


def test_real_lab_nautilus_walk_forward_baseline_costs_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("nautilus_trader")
    native = NativeResearch(tmp_path)
    req = request()
    report = native.run(req)
    parsed = ValidationReport.model_validate(report)
    assert parsed.engine["name"] == "nautilus_trader"
    assert parsed.validation_status == "strict_walk_forward"
    assert parsed.integrity["native_attempts"] == 13  # 2 * 6 protocol runs + prefix audit
    assert 11 <= parsed.integrity["native_runs"] <= 13
    assert len(parsed.folds) == 2
    assert parsed.data_sha256 == req.spec.data.bars_sha256
    assert parsed.input_hash == req.input_hash()
    assert parsed.costs["2x_fees_oos"]["folds"] == 2
    delayed = parsed.costs["execution_stress_oos"]
    if delayed["status"] == "verified":
        assert delayed["metrics"]["folds"] == 2
    else:
        assert delayed["status"] == "failed_native_account_invariants"
        assert delayed["metrics"] is None
        assert "delayed_execution_accounting_unverified" in parsed.failed_gates
        assert not parsed.eligible_for_paper
    assert parsed.costs["execution_stress_oos"]["assumptions"]["latency_nanos"] == 86400000000000
    assert parsed.benchmarks["buy_and_hold_same_oos"]["folds"] == 2
    assert parsed.statistics["deflated_sharpe"]["status"] == "computed"
    assert parsed.statistics["block_bootstrap"]["simulations"] == 200
    assert parsed.statistics["fold_bootstrap"]["simulations"] == 1000
    assert parsed.statistics["fold_bootstrap"]["cross_fold_blocks"] is False
    assert parsed.statistics["search_accounting"]["global_selection_bias_corrected"] is False
    assert parsed.integrity["prefix_invariance"]["status"] == "verified"
    assert parsed.integrity["prefix_invariance"]["removed_future_bars"] == 30
    assert parsed.execution_signal["initialization_scope"] == "final_oos_fold_cash_reset"
    assert parsed.live_ready is False
    for fold in parsed.folds:
        window = fold["fold"]
        assert window["train_end"] < window["validation_start"] < window["test_start"]
        assert window["purge_bars"] == window["embargo_bars"] == 1
        assert fold["temporal_audit"]["test"]["bars"] == 30
    assert native.run(req) == report
    assert len(list(tmp_path.rglob("report.json"))) == 1
    monkeypatch.setenv("QT_AGENT_HUB_EXECUTOR_TOKEN", "test-token")
    client = TestClient(create_research_app(tmp_path))
    response = client.post(
        "/internal/validate", json=req.model_dump(), headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200
    assert response.json() == report


def test_remote_rejects_bad_configuration() -> None:
    with pytest.raises(RuntimeError, match="not_configured"):
        RemoteResearch("http://user:pass@research", "token").run("personal", "j", history(900))
    with pytest.raises(ValueError, match="unsupported strategy"):
        RemoteResearch("http://research", "token").run(
            "personal", "j", history(900), strategy="hallucinated_strategy"
        )


def test_native_prefix_replay_rejects_future_sensitive_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("nautilus_trader")
    from qt.nautilus.executor import NautilusResearchExecutor

    original = NautilusResearchExecutor.execute

    def future_sensitive(self: NautilusResearchExecutor, *args: object, **kwargs: object) -> dict:
        result = original(self, *args, **kwargs)
        if "prefix_audit" in self.parquet_root.parts:
            # A decision changes only when invisible future bars disappear.
            # Metadata and causal row counts alone cannot detect this dependency.
            result["decision_traces"][0]["no_trade_cause"] = "future_sensitive_decision"
        return result

    monkeypatch.setattr(NautilusResearchExecutor, "execute", future_sensitive)
    with pytest.raises(ValueError, match="prefix invariance failed"):
        NativeResearch(tmp_path).run(request())
    assert not list(tmp_path.rglob("report.json"))


def test_remote_rejects_foreign_report_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Malformed reports fail before identity can become evidence.
    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda *args, **kwargs: httpx.Response(
            200,
            content=b'{"status":"completed"}',
            request=httpx.Request("POST", "http://research/internal/validate"),
        ),
    )
    with pytest.raises(ValidationError):
        RemoteResearch("http://research", "token").run("personal", "j", history(900))


def test_exact_crossover_signal_does_not_invent_an_initial_cross() -> None:
    pytest.importorskip("nautilus_trader")
    from qt.agent_hub.research_service import execution_signal

    rows = history(900)
    for index, row in enumerate(rows):
        price = 40000 + index * 50
        row.update(open=price, high=price + 2, low=price - 2, close=price)
    req = ValidationRequest(
        tenant_id="personal",
        job_id="signal",
        spec=build_default_spec(rows),
        bars=rows,
    )
    signal = execution_signal(req)
    # Fast > slow throughout is not a confirmed crossing from <= to >.
    assert signal["side"] == "SELL"
    assert signal["target_weight"] == "0"
    assert signal["method"] == "exact_builtin_target_weight_replay"
    assert signal["data_sha256"] == req.spec.data.bars_sha256
    assert signal["as_of_bar"] == rows[-1]["timestamp"]


def test_rules_are_research_only_without_an_execution_signal_adapter() -> None:
    from qt.agent_hub.research_service import execution_signal

    body = request().model_dump()
    content = {"mode": "rules"}
    body["spec"]["strategy_version"].update(content=content, content_sha256=digest(content))
    signal = execution_signal(ValidationRequest.model_validate(body))
    assert signal["side"] == "HOLD"
    assert signal["method"] == "unsupported_strategy_signal_adapter"


def test_signal_uses_same_reset_state_as_final_oos_fold() -> None:
    pytest.importorskip("nautilus_trader")
    from qt.agent_hub.research_service import execution_signal

    rows = history(180)
    # A crossover before the last 30-bar OOS window must not persist into its
    # reset strategy state. The old all-history signal incorrectly retained BUY.
    for index, row in enumerate(rows):
        price = float(50000 - index * 50 if index < 100 else 45000 + (index - 100) * 50)
        row.update(open=price, high=price + 2, low=price - 2, close=price)
    body = request().model_dump()
    body["bars"] = rows
    body["spec"]["data"]["bars_sha256"] = digest(rows)
    signal = execution_signal(ValidationRequest.model_validate(body))
    assert signal["side"] == "SELL"
    assert signal["decision_start_bar"] == rows[-30]["timestamp"]


def test_service_rejects_path_escape_identifiers() -> None:
    body = request().model_dump()
    body["tenant_id"] = ".."
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(body)


def test_remote_health_is_bounded_and_binds_the_native_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qt.agent_hub.research import native_bridge_fingerprint

    assert len(native_bridge_fingerprint()) == 64
    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"ok": True, "engine": {"native_bridge_sha256": native_bridge_fingerprint()}},
            request=httpx.Request("GET", "http://research/healthz"),
        ),
    )
    result = RemoteResearch("http://research", "token").health()
    assert result["engine"]["native_bridge_sha256"] == native_bridge_fingerprint()


def test_research_rpc_propagates_traceparent_without_baggage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    monkeypatch.setattr("qt.agent_hub.research.traceparent", lambda: trace)
    requests = []

    def send(_client: object, request: httpx.Request, **_kwargs: object) -> httpx.Response:
        requests.append(request)
        body = {"ok": True, "engine": {}} if request.method == "GET" else {"status": "completed"}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    client = RemoteResearch("http://research", "token")
    client.health()
    with pytest.raises(ValidationError):
        client.run("personal", "j", history(900))
    assert len(requests) == 2
    for req in requests:
        assert req.headers["traceparent"] == trace
        assert "baggage" not in req.headers
