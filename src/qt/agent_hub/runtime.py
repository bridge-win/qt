"""Durable multi-agent workflow. Domain tools, not model prose, own side effects."""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from qt.agent_hub.approvals import ApprovalStore
from qt.agent_hub.codex import CodexBusyError
from qt.agent_hub.config import ROLES, HubConfig
from qt.agent_hub.context import ContextStore, _safe_path
from qt.agent_hub.forecast_ledger import ForecastLedger
from qt.agent_hub.governance import Governance, attach_tool_evidence, proposal
from qt.agent_hub.opportunities import OpportunityScanner, OpportunityStore
from qt.agent_hub.planner import ResearchLoop, critic_context
from qt.agent_hub.sandbox import SandboxRunner
from qt.agent_hub.skills import SkillRegistry
from qt.agent_hub.store import HubStore
from qt.agent_hub.tracing import traceparent
from qt.agent_hub.trading import TradingService, trading_from_env
from qt.agent_hub.trial_ledger import TrialLedger

LOG = logging.getLogger(__name__)
FLOW = (
    "discover",
    "research",
    "analysis",
    "backtest",
    "forecast",
    "risk_review",
    "execute",
    "log_review",
)


class ModelProvider(Protocol):
    def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]: ...


class HistoryProvider(Protocol):
    def history(self) -> list[dict[str, Any]]: ...


class ExperimentRunner(Protocol):
    def run(self, tenant_id: str, job_id: str, request: dict[str, Any]) -> dict[str, Any]: ...


class RemoteCodex:
    def __init__(self, config: HubConfig) -> None:
        self.config = config
        self.url = os.environ.get("QT_AGENT_HUB_CODEX_URL", "http://codex:8091").rstrip("/")

    def invoke(self, role: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
        propagation = traceparent()
        with httpx.Client(timeout=self.config.codex_timeout + 10, trust_env=False) as client:
            response = client.post(
                f"{self.url}/invoke",
                json={
                    "role": role,
                    "task": task,
                    "context": context,
                    "model": self.config.roles[role].model,
                },
                headers={
                    "Authorization": f"Bearer {os.environ.get('QT_AGENT_HUB_EXECUTOR_TOKEN', '')}",
                    **({"traceparent": propagation} if propagation else {}),
                },
            )
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5.0
                raise CodexBusyError(retry_after=retry_after)
            if response.status_code != 200:
                raise RuntimeError(f"codex_executor_http_{response.status_code}")
            value = response.json()
            if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
                raise RuntimeError("invalid_codex_executor_result")
            return value


class MarketData:
    def history(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1d",
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("invalid_market_history")
        now_ms = time.time() * 1000
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                raise ValueError("invalid_market_candle")
            values = [float(item) for item in row[:6]]
            if not all(math.isfinite(item) for item in values) or min(values[1:5]) <= 0:
                raise ValueError("invalid_market_number")
            if float(row[6]) < now_ms:
                result.append(
                    {
                        "timestamp": int(values[0]),
                        "open": values[1],
                        "high": values[2],
                        "low": values[3],
                        "close": values[4],
                        "volume": values[5],
                    }
                )
        if len(result) < 30 or any(
            a["timestamp"] >= b["timestamp"] for a, b in itertools.pairwise(result)
        ):
            raise ValueError("insufficient_or_unordered_history")
        return result


class HubRuntime:
    def __init__(
        self,
        config: HubConfig,
        root: Path,
        files: Path,
        *,
        provider: ModelProvider | None = None,
        market: HistoryProvider | None = None,
        trading: TradingService | None = None,
        sandbox: ExperimentRunner | None = None,
        research: Any = None,
        opportunity_scanner: Any = None,
    ) -> None:
        self.config, self.root, self.files = config, root, files
        root.mkdir(parents=True, exist_ok=True)
        self.store = HubStore(root / "hub.sqlite3")
        self.context = ContextStore(root / "context.sqlite3", files)
        self.governance = Governance(root)
        self.opportunities = OpportunityStore(root / "opportunities.sqlite3")
        self.opportunity_scanner = opportunity_scanner or OpportunityScanner()
        self.skills = SkillRegistry()
        if trading is not None:
            self.trading = trading
        elif config.mode == "paper":
            self.trading = TradingService(
                root / "trading.sqlite3",
                mode="paper",
                max_order_quote=config.max_order_quote,
                max_position_quote=config.max_position_quote,
                max_daily_quote=config.max_daily_quote,
                frozen_allows_protective_exit=config.frozen_allows_protective_exit,
            )
        else:
            self.trading = trading_from_env(
                root / "trading.sqlite3",
                environ={
                    **os.environ,
                    "QT_AGENT_HUB_TRADING_MODE": config.mode,
                    "QT_AGENT_HUB_EXECUTION_TENANT": config.tenant_id,
                },
            )
            from decimal import Decimal

            self.trading.max_order_quote = Decimal(str(config.max_order_quote))
            self.trading.max_position_quote = Decimal(str(config.max_position_quote))
            self.trading.max_daily_quote = Decimal(str(config.max_daily_quote))
        self.provider: ModelProvider = provider or RemoteCodex(config)
        self.market: HistoryProvider = market or MarketData()
        self.sandbox: ExperimentRunner = sandbox or SandboxRunner(root / "experiments")
        self.research = research
        if self.research is None and os.environ.get("QT_AGENT_HUB_RESEARCH_URL"):
            from qt.agent_hub.research import RemoteResearch

            self.research = RemoteResearch()
        self.trials = TrialLedger(root / "trials.sqlite3")
        self.forecasts = ForecastLedger(root / "forecasts.sqlite3")
        self.approvals = ApprovalStore(root / "approvals.sqlite3")
        self.planner = ResearchLoop(
            max_rounds=config.planner_max_rounds,
            max_tool_calls=config.planner_max_tool_calls,
        )
        self.crash_after: str | None = None

    def install_schedules(self) -> None:
        for item in self.config.schedules:
            self.store.put_schedule(
                self.config.tenant_id,
                item.name,
                item.kind,
                item.payload,
                item.interval_seconds,
                enabled=item.enabled,
                overlap_policy=item.overlap_policy,
                catchup_window_s=item.catchup_window_s,
                debounce_key=item.debounce_key,
                debounce_period_s=item.debounce_period_s,
                priority=item.priority,
                pause_on_failure=item.pause_on_failure,
                notify_only_on_change=item.notify_only_on_change,
            )

    def _ancestors(self, tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        parent = payload.get("parent_id")
        for _ in range(12):
            if not parent:
                break
            job = cast(dict[str, Any] | None, self.store.get_job(tenant, parent))
            if not job or job["status"] != "succeeded":
                raise ValueError("workflow_parent_unavailable")
            result.setdefault(job["kind"], job.get("result") or {})
            parent = job["payload"].get("parent_id")
        return result

    def _quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        instrument = payload.get("instrument", "BTCUSDT")
        if payload.get("venue") == "polymarket" and payload.get("outcome_token"):
            instrument = payload["outcome_token"]
        return self.governance.call(
            "tool",
            "market.quote",
            self.trading.quote,
            payload.get("venue", "binance"),
            instrument,
        )

    def _artifact(self, tenant: str, job_id: str, value: dict[str, Any]) -> dict[str, Any]:
        destination = _safe_path(self.files.resolve(), tenant, "artifacts", f"{job_id}.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
        temp = destination.with_suffix(".tmp")
        temp.write_bytes(raw)
        temp.replace(destination)
        return {
            "path": f"artifacts/{destination.name}",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }

    def _import_context(self, tenant: str) -> dict[str, Any]:
        folder = _safe_path(self.files.resolve(), tenant, "imports")
        imported, skipped = [], 0
        if not folder.exists():
            return {"imported": [], "status": "no_mounted_files"}
        for source in sorted(folder.glob("*"))[:100]:
            if (
                source.is_symlink()
                or not source.is_file()
                or source.suffix.lower() not in {".md", ".txt", ".csv"}
            ):
                skipped += 1
                continue
            if source.stat().st_size > 200000:
                skipped += 1
                continue
            content = source.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode()).hexdigest()
            marker = (
                self.root
                / f"import-{tenant}-{hashlib.sha256(source.name.encode()).hexdigest()}.sha256"
            )
            if marker.exists() and marker.read_text() == digest:
                continue
            imported.append(self.context.ingest(tenant, f"mount:{source.name}", content))
            marker.write_text(digest)
        return {"imported": imported, "skipped": skipped}

    def _fit_context(
        self, job: dict[str, Any], task: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Bound the complete model input, after adding tools and prior steps."""
        envelope = json.dumps({"role": job["kind"], "task": task}, ensure_ascii=False).encode()
        budget = max(1000, min(48000, 58000 - len(envelope)))
        if len(json.dumps(context, ensure_ascii=False).encode()) <= budget:
            return context
        reference = self._artifact(
            job["tenant_id"],
            hashlib.sha256(f"context:{job['id']}:{job['lease_token']}".encode()).hexdigest(),
            context,
        )

        def compact(value: Any, text_limit: int = 1200, list_limit: int = 5) -> Any:
            if isinstance(value, str):
                return value[:text_limit]
            if isinstance(value, list):
                return [compact(item, text_limit, list_limit) for item in value[:list_limit]]
            if isinstance(value, dict):
                return {
                    key: compact(item, text_limit, list_limit)
                    for key, item in value.items()
                    if key not in {"actions", "equity_curve", "fills", "execution_events"}
                }
            return value

        result = cast(dict[str, Any], compact(context))
        if "trade_proposal" in context:
            result["trade_proposal"] = context["trade_proposal"]
        result["offloaded_context"] = reference
        result["context_compacted"] = True
        if len(json.dumps(result, ensure_ascii=False).encode()) > budget:
            result = cast(dict[str, Any], compact(result, 240, 2))
            if "trade_proposal" in context:
                result["trade_proposal"] = context["trade_proposal"]
        if len(json.dumps(result, ensure_ascii=False).encode()) > budget:
            raise ValueError("context_cannot_fit_provider_budget")
        return result

    def handle(
        self, job: dict[str, Any], *, lease_guard: Callable[[], bool] | None = None
    ) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
        tenant, kind, payload = job["tenant_id"], job["kind"], job["payload"]
        if kind == "pipeline":
            return (
                {"flow": list(FLOW), "version": 3, "strategy": payload.get("strategy", "ma_trend")},
                "discover",
                {**payload, "pipeline_id": job["id"]},
            )
        if kind == "discover":
            snapshot = self.governance.call(
                "tool",
                "opportunities.scan",
                self.opportunity_scanner.scan,
                tenant,
                limit=5,
                deadline_seconds=45,
            )
            discovery_result = self.opportunities.record(tenant, snapshot)
            return (
                discovery_result,
                "research" if payload.get("pipeline_id") else None,
                dict(payload) if payload.get("pipeline_id") else None,
            )
        if kind == "import_context":
            return self._import_context(tenant), None, None
        if kind == "monitor":
            reconciliation = self.governance.call(
                "tool", "orders.reconcile", self.trading.reconcile, tenant
            )
            if reconciliation["unresolved_orders"]:
                reconciliation["error"] = "unresolved_orders_reconcile_required"
            quote = self._quote(payload)
            self.store.event(tenant, job["id"], "market_snapshot", quote)
            protection = []
            if self.config.auto_execute and quote.get("ready") and quote.get("bid"):
                portfolio = self.trading.portfolio(tenant)
                for position in portfolio["positions"]:
                    if (
                        position["venue"] != quote["venue"]
                        or position["instrument"] != quote["instrument"]
                    ):
                        continue
                    quantity, average = (
                        float(position["quantity"]),
                        float(position["average_price"]),
                    )
                    if (
                        quantity <= 0
                        or average <= 0
                        or float(quote["bid"]) > average * (1 - self.config.stop_loss_fraction)
                    ):
                        continue
                    quantity = min(
                        quantity, self.config.max_order_quote * 0.99 / float(quote["bid"])
                    )
                    try:
                        protection.append(
                            self.governance.call(
                                "tool",
                                "orders.protective_exit",
                                self.trading.submit,
                                tenant,
                                {
                                    "idempotency_key": f"protect:{job['id']}:{position['instrument']}",
                                    "venue": position["venue"],
                                    "instrument": position["instrument"],
                                    "side": "SELL",
                                    "quantity": quantity,
                                    "limit_price": quote["bid"],
                                    "quote_timestamp": quote["timestamp"],
                                },
                                lease_guard=lease_guard,
                            )
                        )
                    except ValueError:
                        protection.append(
                            {
                                "status": "blocked",
                                "reason": "protective_exit_risk_or_liquidity_block",
                            }
                        )
            snapshot = {
                "reconciliation": reconciliation,
                "quote": quote,
                "portfolio": self.trading.portfolio(tenant),
                "protection": protection,
            }
            schedule_id = payload.get("schedule_id")
            if payload.get("notify_only_on_change") and schedule_id:
                changed = self.store.note_schedule_state(
                    tenant,
                    str(schedule_id),
                    hashlib.sha256(
                        json.dumps(
                            {"quote": quote, "unresolved": reconciliation.get("unresolved_orders")},
                            sort_keys=True,
                            default=str,
                        ).encode()
                    ).hexdigest(),
                )
                snapshot["notify"] = changed
            return (snapshot, None, None)
        previous = self._ancestors(tenant, payload)
        if kind == "execute":
            return (
                self.execute(tenant, job, previous, lease_guard=lease_guard),
                "log_review" if payload.get("pipeline_id") else None,
                dict(payload) if payload.get("pipeline_id") else None,
            )
        if kind not in ROLES:
            raise ValueError("unknown_job_kind")
        query = str(payload.get("question", "Assess BTC market evidence and uncertainty."))[:8000]
        context: dict[str, Any] = self.governance.call(
            "retrieval",
            "context.assemble",
            self.context.assemble,
            tenant,
            query[:4000],
            budget_chars=self.config.context_budget_chars,
            run_id=str(payload.get("pipeline_id") or job["id"]),
        )
        self.skills.require_tool(kind, "context.search")
        context["skill"] = self.skills.summary(kind)
        if context.get("references"):
            self.skills.require_tool(kind, "context.read_chunk")
            context["retrieved_chunks"] = self.governance.call(
                "retrieval",
                "context.read_chunks",
                self.context.retrieve_chunks,
                tenant,
                query[:4000],
                context["references"],
            )
        context["previous_steps"] = {
            key: {
                k: v
                for k, v in value.items()
                if k in {"answer", "quote", "venue", "instrument", "status", "reason"}
            }
            for key, value in previous.items()
        }
        context["server_policy"] = {
            "mode": self.config.mode,
            "max_order_quote": self.config.max_order_quote,
            "instrument": payload.get("instrument", "BTCUSDT"),
            "venue": payload.get("venue", "binance"),
            "outcome_token": payload.get("outcome_token"),
        }
        if kind in {"research", "analysis", "forecast", "risk_review"}:
            self.skills.require_tool(kind, "opportunities.read")
            # A workflow consumes its immutable discovery snapshot. Standalone
            # roles may inspect the latest snapshot with explicit age, not silently
            # treat an old candidate as a current executable opportunity.
            discovered = previous.get("discover")
            if discovered is None:
                discovered = self.governance.call(
                    "retrieval", "opportunities.latest", self.opportunities.latest_scan, tenant
                )
            if discovered:
                context["opportunities"] = {
                    key: discovered[key]
                    for key in ("id", "sha256", "generated_at", "status", "candidates", "errors")
                    if key in discovered
                }
                context["opportunities"]["age_seconds"] = max(
                    0, time.time() - discovered.get("generated_at", 0)
                )
                context["opportunities"]["execution_authorized"] = False
                target = (
                    payload.get("outcome_token")
                    if payload.get("venue") == "polymarket"
                    else payload.get("instrument", "BTCUSDT")
                )
                context["opportunities"]["candidates"] = [
                    item
                    for item in discovered.get("candidates", [])
                    if item.get("venue") == payload.get("venue", "binance")
                    and item.get("instrument") == target
                ]
                context["opportunities"]["scope"] = "requested_venue_and_instrument_only"
            if payload.get("selected_candidate"):
                context["selected_opportunity"] = {
                    "observation": payload["selected_candidate"],
                    "is_historical_observation": True,
                    "execution_authorized": False,
                    "policy": "Original owner-selected evidence; current scan may have no matching candidate. Revalidate price, fees, resolution rules and forecast before any proposal.",
                }
        backtest = previous.get("backtest", {}).get("experiment")
        if isinstance(backtest, dict):
            context["verified_backtest"] = {
                key: value
                for key, value in backtest.items()
                if key
                in {
                    "status",
                    "metrics",
                    "input_hash",
                    "input_artifact",
                    "limitations",
                    "validation",
                }
            }
        if kind == "research":
            from qt.agent_hub.sources import collect

            self.skills.require_tool(kind, "research.collect")
            context["public_sources"] = self.governance.call(
                "tool",
                "research.collect",
                collect,
                self.context,
                tenant,
                self.root / "source-cache" / tenant,
            )
        if kind in {"research", "forecast", "risk_review"}:
            self.skills.require_tool(kind, "market.quote")
            try:
                context["market"] = self._quote(payload)
            except (httpx.HTTPError, ValueError, RuntimeError):
                context["market"] = {"ready": False, "reason": "market_data_unavailable"}
        if kind in {"analysis", "backtest", "forecast"}:
            self.skills.require_tool(kind, "market.history")
            try:
                history = self.governance.call("tool", "market.history", self.market.history)
                context["market_history"] = {
                    "source": "binance-public-closed-daily-candles",
                    "bars": len(history),
                    "recent": history[-30:],
                }
                if kind == "backtest":
                    self.skills.require_tool(kind, "experiment.run")
                    experiment_request = {
                        "task": "backtest",
                        "prices": [row["close"] for row in history],
                        "initial_cash": 1000,
                        "fee_bps": 10,
                        "window": 20,
                    }
                    input_artifact = self._artifact(
                        tenant,
                        hashlib.sha256(
                            f"backtest-input:{job['id']}:{job['lease_token']}".encode()
                        ).hexdigest(),
                        {
                            "request": experiment_request,
                            "bars": history,
                            "source": "https://data-api.binance.vision/api/v3/klines",
                            "retrieved_at": time.time(),
                            "closed_bars_only": True,
                        },
                    )
                    experiment = self.governance.call(
                        "tool",
                        "sandbox.backtest",
                        self.sandbox.run,
                        tenant,
                        job["id"],
                        experiment_request,
                    )
                    experiment["input_artifact"] = input_artifact
                    if experiment.get("status") not in {"completed", "succeeded"}:
                        raise RuntimeError("isolated_experiment_not_completed")
                    context["experiment"] = {
                        key: value
                        for key, value in experiment.items()
                        if key not in {"equity_curve", "fills"}
                    }
                    if self.research is not None:
                        if payload.get("venue", "binance") == "binance":
                            validation = self.governance.call(
                                "tool",
                                "research.native_validation",
                                self.research.run,
                                tenant,
                                job["id"],
                                history,
                                strategy=payload.get("strategy", "ma_trend"),
                            )
                        else:
                            validation = {
                                "status": "not_evaluated",
                                "eligible_for_paper": False,
                                "reason": "prediction_market_requires_resolved_event_dataset",
                            }
                        experiment["validation"] = validation
                        context["experiment"]["validation"] = validation
            except (httpx.HTTPError, ValueError, RuntimeError):
                if kind == "backtest":
                    raise RuntimeError("backtest_input_or_executor_unavailable") from None
                context["market_history"] = {"ready": False}
        if kind == "log_review":
            self.skills.require_tool(kind, "events.read")
            context["execution_events"] = self.store.events(tenant, limit=40)
        reviewed_proposal = previous.get("forecast", {}).get("proposal")
        if kind == "risk_review" and reviewed_proposal:
            self.skills.require_tool(kind, "proposal.verify")
            context["trade_proposal"] = reviewed_proposal
            context["previous_steps"] = {
                key: value
                for key, value in context["previous_steps"].items()
                if key in {"forecast", "backtest"}
            }
            context.update(
                critic_context(reviewed_proposal, list(context.get("references") or []))
            )
        instruction = self.config.roles[kind].instruction + "\nTask: "
        task = instruction + query[: max(0, 8192 - len(instruction))]
        context = self._fit_context(job, task, context)
        attach_tool_evidence(context)
        receipt = self.governance.receipt(
            tenant, payload.get("pipeline_id", job["id"]), job["id"], context
        )
        model_meta = {
            "model": self.config.roles[kind].model or "codex-account-default",
            "provider": "local-codex",
            "skill_sha256": self.skills.summary(kind)["sha256"],
        }

        def invoke_model(role: str, prompt: str, working: dict[str, Any]) -> dict[str, Any]:
            return self.governance.call(
                "model",
                "codex.invoke",
                self.provider.invoke,
                role,
                prompt,
                working,
                metadata=model_meta,
            )

        if kind in {"research", "analysis"}:
            answer = self.planner.run(
                invoke=invoke_model,
                execute_tool=lambda tool, args: self._planner_tool(tenant, job, tool, args),
                role=kind,
                task=task,
                context=context,
            )
        else:
            answer = invoke_model(kind, task, context)
        runtime_metadata = answer.pop("_runtime", {})
        planner_trace = answer.pop("planner", None)
        result: dict[str, Any] = {
            "agent": kind,
            "model": self.config.roles[kind].model or "codex-account-default",
            "provider": "local-codex",
            "answer": answer,
            "runtime": runtime_metadata,
            "config_sha256": hashlib.sha256(self.config.model_dump_json().encode()).hexdigest(),
            "evidence_receipt": receipt,
            "evidence_validation": self._validate_evidence(tenant, receipt, answer.get("evidence")),
            "skill": self.skills.summary(kind),
        }
        if planner_trace:
            result["planner"] = planner_trace
        if kind == "backtest":
            result["experiment"] = experiment
            validation = experiment.get("validation") if isinstance(experiment, dict) else {}
            snapshot_id = str((experiment or {}).get("input_hash") or job["id"])
            oos = []
            if isinstance(validation, dict):
                metrics = validation.get("metrics") or {}
                if isinstance(metrics, dict) and metrics.get("sharpe") is not None:
                    oos.append(float(metrics["sharpe"]))
            self.trials.record(
                tenant,
                {
                    "trial_id": job["id"],
                    "snapshot_id": snapshot_id,
                    "strategy_spec_hash": str((validation or {}).get("strategy_sha256") or ""),
                    "params_hash": str((validation or {}).get("strategy_version_id") or ""),
                    "cost_model_hash": str((experiment or {}).get("input_hash") or ""),
                    "arm": payload.get("strategy", "ma_trend"),
                    "oos_sharpe": oos[0] if oos else None,
                    "proposer": kind,
                },
            )
            experiment["search_accounting"] = self.trials.summary(tenant, snapshot_id, oos)
        if kind == "forecast":
            result["quote"] = context.get("market")
            result["venue"] = payload.get("venue", "binance")
            result["instrument"] = payload.get("instrument", "BTCUSDT")
            result["outcome_token"] = payload.get("outcome_token")
            result["proposal"] = proposal(result, backtest, result["config_sha256"])
            quote = result.get("quote") or {}
            horizon = answer.get("horizon_seconds")
            if not isinstance(horizon, (int, float)) or not 0 < horizon < float("inf"):
                # Expired/invalid proposals remain auditable research results,
                # but are never admitted to the forward-scoring population.
                result["forecast_recording"] = {
                    "status": "held", "reason": "positive_finite_forecast_horizon_required",
                }
            else:
                self.forecasts.record_forecast(
                    tenant,
                    {
                        "forecast_id": job["id"],
                        "run_id": payload.get("pipeline_id", job["id"]),
                        "market_id": result["instrument"],
                        "token_id": result.get("outcome_token"),
                        "p_model": answer.get("probability"),
                        "p_market_mid": quote.get("mid") if result["venue"] == "polymarket" else None,
                        "best_bid": quote.get("bid") if result["venue"] == "polymarket" else None,
                        "best_ask": quote.get("ask") if result["venue"] == "polymarket" else None,
                        "horizon_s": answer.get("horizon_seconds"),
                        "evidence_receipt": receipt.get("receipt_hash"),
                        "net_edge_reason": "missing_independent_calibrated_probability",
                    },
                )
        if kind == "risk_review":
            result["reviewed_proposal_hash"] = context.get("trade_proposal", {}).get(
                "proposal_hash"
            )
        from qt.agent_hub.release import release_fingerprint

        result["release_sha256"] = release_fingerprint(self)
        # Attempt-addressed artifacts are immutable across lease takeovers. A
        # late provider response cannot overwrite the accepted attempt's file.
        result["artifact"] = self._artifact(tenant, f"{job['id']}-{job['lease_token']}", result)
        if payload.get("pipeline_id") and kind in FLOW:
            index = FLOW.index(kind)
            next_kind = FLOW[index + 1] if index + 1 < len(FLOW) else None
            if next_kind == "execute" and not (
                self.config.auto_execute and payload.get("auto_execute")
            ):
                next_kind = "log_review"
            return result, next_kind, dict(payload) if next_kind else None
        return result, None, None

    def _planner_tool(self, tenant: str, job: dict[str, Any], tool: str, args: dict[str, Any]) -> Any:
        try:
            if tool == "search_evidence":
                return self.context.search(tenant, str(args.get("query") or "")[:4000])[:8]
            if tool == "read_chunk":
                doc_id = str(args.get("document_id") or args.get("id") or "")
                rows = self.context.search(tenant, str(args.get("query") or "BTC")[:400])
                match = next((row for row in rows if row["id"] == doc_id), rows[0] if rows else None)
                if match is None:
                    return {"error": "evidence_unavailable"}
                return self.context.read_chunk(
                    tenant,
                    match["id"],
                    match["sha256"],
                    [{"id": match["id"], "sha256": match["sha256"]}],
                    offset=int(args.get("offset") or 0),
                    limit_chars=min(1200, int(args.get("limit_chars") or 800)),
                )
            if tool == "market_history":
                history = self.market.history()
                return {"bars": len(history), "recent": history[-10:]}
            if tool == "fee_schedule":
                return {"known": False, "reason": "account_tier_fee_not_publicly_verified"}
            if tool == "validate_strategy":
                if self.research is None:
                    return {"status": "not_evaluated", "eligible_for_paper": False}
                return {"status": "deferred_to_backtest_role"}
            if tool == "forecast_ledger_lookup":
                return self.forecasts.lookup(tenant, str(args.get("market_id") or "BTCUSDT"))
            return {"error": "unknown_planner_tool"}
        except (ValueError, KeyError, TypeError, OSError) as error:
            return {"error": type(error).__name__}

    def _validate_evidence(
        self, tenant: str, receipt: dict[str, Any], citations: object
    ) -> dict[str, Any]:
        result = self.governance.validate(tenant, receipt, citations)
        if not result["valid"]:
            return result
        knowledge = [ref for ref in receipt["references"] if ":" not in ref["id"]]
        selected = [ref for ref in knowledge if ref["id"] in cast(list[str], citations)]
        if selected:
            checked = self.context.validate_evidence(tenant, selected, knowledge)
            if not checked["valid"]:
                result["valid"] = False
                result["errors"].append("knowledge_version_or_integrity_invalid")
        return result

    def execute(
        self,
        tenant: str,
        job: dict[str, Any],
        previous: dict[str, Any],
        *,
        lease_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if lease_guard is not None and not lease_guard():
            return {"status": "blocked", "reason": "execution_lease_lost"}
        execution_key = f"pipeline:{job['payload'].get('pipeline_id', job['id'])}"
        existing = self.trading.lookup_order(tenant, execution_key)
        if existing is not None:
            self.trading.record_effect(
                tenant, execution_key, status=str(existing.get("status") or "recovered"),
                external_id=existing.get("order_id") or existing.get("exchange_id"),
                result={"status": existing.get("status")},
            )
            return existing
        stored_intent = self.trading.get_execution_intent(tenant, execution_key)
        if stored_intent is not None:
            return self._submit_persisted_intent(tenant, stored_intent, job, lease_guard)
        forecast = previous.get("forecast", {})
        review = previous.get("risk_review", {}).get("answer", {})
        answer, quote = forecast.get("answer", {}), forecast.get("quote", {})
        if not self.config.auto_execute or not job["payload"].get("auto_execute"):
            return {"status": "blocked", "reason": "automatic_execution_disabled"}
        if review.get("decision") != "propose" or answer.get("decision") != "propose":
            return {"status": "held", "reason": "agent_review_did_not_propose"}
        if (
            min(float(review.get("confidence", 0)), float(answer.get("confidence", 0)))
            < self.config.minimum_confidence
        ):
            return {"status": "held", "reason": "insufficient_confidence"}
        if not answer.get("evidence") or not review.get("evidence"):
            return {"status": "held", "reason": "missing_evidence"}
        from qt.agent_hub.release import release_fingerprint

        current_release = release_fingerprint(self)
        if any(
            previous.get(role, {}).get("release_sha256") != current_release
            for role in ("forecast", "risk_review", "backtest")
        ):
            return {"status": "held", "reason": "prior_approval_release_changed"}
        for stage in (forecast, previous.get("risk_review", {})):
            if stage.get("evidence_receipt", {}).get("run_id") != job["payload"].get(
                "pipeline_id", job["id"]
            ):
                return {"status": "held", "reason": "evidence_run_mismatch"}
            if not stage.get("evidence_validation", {}).get("valid"):
                return {"status": "held", "reason": "unverified_evidence"}
            if not self._validate_evidence(
                tenant, stage.get("evidence_receipt", {}), stage["answer"].get("evidence")
            )["valid"]:
                return {"status": "held", "reason": "evidence_receipt_invalid"}
        expected = proposal(
            forecast,
            previous.get("backtest", {}).get("experiment"),
            hashlib.sha256(self.config.model_dump_json().encode()).hexdigest(),
        )
        for boundary in (
            (forecast.get("proposal") or {}).get("time_boundary") or {},
            expected.get("time_boundary") or {},
        ):
            expires_at = boundary.get("expires_at")
            if expires_at is not None and time.time() > float(expires_at):
                return {"status": "held", "reason": "evidence_expired"}
            cutoff, created = boundary.get("knowledge_cutoff"), boundary.get("created_at")
            if (
                cutoff is not None
                and created is not None
                and float(cutoff) > float(created) + 1
            ):
                return {"status": "held", "reason": "evidence_outside_time_boundary"}
        if (
            forecast.get("proposal") != expected
            or previous.get("risk_review", {}).get("reviewed_proposal_hash")
            != expected["proposal_hash"]
        ):
            return {"status": "held", "reason": "proposal_or_policy_changed"}
        stored_hash = (forecast.get("proposal") or {}).get("evidence_hash")
        if stored_hash and stored_hash != expected.get("evidence_hash"):
            return {"status": "held", "reason": "evidence_hash_mismatch"}
        validation = previous.get("backtest", {}).get("experiment", {}).get("validation", {})
        if validation.get("eligible_for_paper") is not True:
            return {"status": "held", "reason": "strategy_validation_not_eligible"}
        from qt.agent_hub.research import native_bridge_fingerprint

        try:
            current_engine = self.research.health()["engine"]
        except Exception:
            return {"status": "held", "reason": "research_engine_changed_or_unavailable"}
        if (
            not isinstance(current_engine, dict)
            or not current_engine
            or validation.get("engine") != current_engine
            or current_engine.get("native_bridge_sha256") != native_bridge_fingerprint()
        ):
            return {"status": "held", "reason": "research_engine_changed_or_unavailable"}
        if self.config.mode != "paper":
            return {"status": "held", "reason": "live_strategy_acceptance_not_verified"}
        venue, instrument, side = (
            forecast.get("venue"),
            forecast.get("instrument"),
            answer.get("side"),
        )
        if side not in {"BUY", "SELL"}:
            return {"status": "held", "reason": "no_directional_action"}
        signal = validation.get("execution_signal", {})
        if (
            signal.get("strategy_version_id") != validation.get("strategy_version_id")
            or signal.get("strategy_sha256") != validation.get("strategy_sha256")
            or signal.get("data_sha256") != validation.get("data_sha256")
            or signal.get("side") != side
        ):
            return {"status": "held", "reason": "action_not_bound_to_validated_strategy"}
        as_of_bar = signal.get("as_of_bar", 0) / 1000
        if as_of_bar > time.time() or time.time() - as_of_bar > 2 * 86400:
            return {"status": "held", "reason": "validated_strategy_signal_stale"}
        holdings = self.trading.portfolio(tenant)["positions"]
        if side == "BUY" and any(
            float(position["quantity"]) > 0
            for position in holdings
            if position["venue"] == venue and position["instrument"] == instrument
        ):
            return {"status": "held", "reason": "strategy_target_already_held"}
        # Get a fresh executable quote; the model's old snapshot never becomes an order price.
        if venue == "polymarket":
            instrument = forecast.get("outcome_token")
            if not instrument:
                return {"status": "held", "reason": "prediction_requires_explicit_outcome_intent"}
        current = self.governance.call(
            "tool", "market.execution_quote", self.trading.quote, venue, instrument
        )
        if venue == "polymarket":
            probability = answer.get("probability")
            if probability is None or not 0 <= probability <= 1:
                return {"status": "held", "reason": "prediction_probability_missing"}
            edge = (
                probability - float(current["ask"])
                if side == "BUY"
                else float(current["bid"]) - probability
            )
            if edge < self.config.minimum_prediction_edge:
                return {"status": "held", "reason": "prediction_edge_below_policy"}
        ask, bid = float(current["ask"]), float(current["bid"])
        price = ask if side == "BUY" else bid
        if not quote or not quote.get("timestamp"):
            return {"status": "held", "reason": "forecast_missing_market_snapshot"}
        horizon = answer.get("horizon_seconds")
        if not horizon or time.time() - float(quote["timestamp"]) > horizon:
            return {"status": "held", "reason": "forecast_horizon_expired"}
        if abs(price / float(quote.get("ask", price)) - 1) > 0.02:
            return {"status": "held", "reason": "forecast_price_drift"}
        quantity = self.config.max_order_quote / price * 0.99
        if self.crash_after == "quote_ready":
            raise RuntimeError("injected_crash_after_quote")
        if side == "SELL":
            held = next(
                (
                    float(position["quantity"])
                    for position in self.trading.portfolio(tenant)["positions"]
                    if position["venue"] == venue and position["instrument"] == instrument
                ),
                0.0,
            )
            quantity = min(quantity, held)
            if quantity <= 0:
                return {"status": "held", "reason": "no_position_to_sell"}
        intent = self.trading.persist_execution_intent(
            tenant,
            execution_key,
            {
                "idempotency_key": execution_key,
                "venue": venue,
                "instrument": instrument,
                "side": side,
                "quantity": quantity,
                "limit_price": price,
                "quote_timestamp": current["timestamp"],
            },
        )
        return self._submit_persisted_intent(tenant, intent, job, lease_guard)

    def _submit_persisted_intent(
        self,
        tenant: str,
        stored: dict[str, Any],
        job: dict[str, Any],
        lease_guard: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        intent = {
            key: stored[key]
            for key in (
                "idempotency_key",
                "venue",
                "instrument",
                "side",
                "quantity",
                "limit_price",
                "quote_timestamp",
            )
            if key in stored
        }
        execution_key = intent["idempotency_key"]
        if self.crash_after == "intent_persisted":
            raise RuntimeError("injected_crash_after_intent")
        if self.config.require_execution_approval:
            effect = self.trading.effect_id(execution_key)
            open_approval = next(
                (
                    item
                    for item in self.approvals.pending(tenant)
                    if item.get("effect_id") == effect and not item.get("expired")
                ),
                None,
            )
            if open_approval:
                return {
                    "status": "awaiting_approval",
                    "reason": "execution_requires_owner_approval",
                    "approval_id": open_approval["id"],
                    "effect_id": effect,
                    "expires_at": open_approval["expires_at"],
                }
            approval = self.approvals.request(
                tenant,
                job["payload"].get("pipeline_id", job["id"]),
                intent,
                risk_tier="funds",
                effect_id=self.trading.effect_id(execution_key),
            )
            return {
                "status": "awaiting_approval",
                "reason": "execution_requires_owner_approval",
                "approval_id": approval["id"],
                "effect_id": approval["effect_id"],
                "expires_at": approval["expires_at"],
            }
        submitted = self.governance.call(
            "tool",
            "orders.submit",
            self.trading.submit,
            tenant,
            intent,
            lease_guard=lease_guard,
        )
        self.trading.record_effect(
            tenant,
            execution_key,
            status=str(submitted.get("status") or "submitted"),
            external_id=submitted.get("order_id") or submitted.get("exchange_id"),
            result={"status": submitted.get("status")},
        )
        if self.crash_after == "order_submitted":
            raise RuntimeError("injected_crash_after_submit")
        return submitted

    def run_once(self, worker_id: str, kinds: list[str] | None = None) -> dict[str, Any] | None:
        tenant = self.config.tenant_id
        job = cast(
            dict[str, Any] | None,
            self.store.claim(
                tenant, worker_id, lease_seconds=self.config.lease_seconds, kinds=kinds
            ),
        )
        if not job:
            return None
        stopped = threading.Event()
        lease_lost = threading.Event()

        def lease_guard() -> bool:
            if lease_lost.is_set():
                return False
            valid = self.store.heartbeat(
                tenant,
                job["id"],
                worker_id,
                job["lease_token"],
                lease_seconds=self.config.lease_seconds,
            )
            if not valid:
                lease_lost.set()
            return valid

        def renew() -> None:
            while not stopped.wait(self.config.lease_seconds / 3):
                if not lease_guard():
                    break

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        started = time.monotonic()
        try:
            with self.governance.invocation(
                tenant,
                job["payload"].get("pipeline_id", job["id"]),
                job["id"],
                job["kind"],
                attempt=job["lease_token"],
                worker_id=worker_id,
                parent_step_id=job["payload"].get("parent_id"),
            ) as invocation:
                result, next_kind, next_payload = self.handle(job, lease_guard=lease_guard)
                if self.crash_after == "before_complete":
                    raise RuntimeError("injected_crash_before_complete")
                from qt.agent_hub.release import release_fingerprint

                result["release_sha256"] = release_fingerprint(self)
                if next_payload is not None:
                    next_payload["parent_id"] = job["id"]
                accepted = self.store.complete_and_enqueue(
                    tenant,
                    job["id"],
                    worker_id,
                    job["lease_token"],
                    result,
                    next_kind=next_kind,
                    next_payload=next_payload,
                    idempotency_key=f"{job['id']}:{next_kind}" if next_kind else None,
                )
                invocation["status"] = "succeeded" if accepted else "fenced"
                invocation["metadata"].update(
                    {
                        "accepted": accepted,
                        "reason": result.get("reason"),
                        "evidence_valid": result.get("evidence_validation", {}).get("valid"),
                    }
                )
            self.governance.trace(
                tenant,
                job["payload"].get("pipeline_id", job["id"]),
                job["id"],
                job["kind"],
                "succeeded" if accepted else "fenced",
                time.monotonic() - started,
                {
                    "accepted": accepted,
                    "lease_token": job["lease_token"],
                    "model": result.get("model"),
                    "provider": result.get("provider"),
                    "usage": result.get("runtime", {}).get("usage"),
                    "evidence_valid": result.get("evidence_validation", {}).get("valid"),
                    "reason": result.get("reason"),
                },
            )
            if accepted and isinstance(result.get("answer"), dict):
                try:
                    self.context.remember(
                        tenant,
                        f"job:{job['id']}",
                        json.dumps(result["answer"], ensure_ascii=False),
                        scope="short",
                        ttl_seconds=86400,
                        source=f"run:{job['payload'].get('pipeline_id', job['id'])}",
                    )
                except (ValueError, OSError):
                    LOG.warning("derived_memory_unavailable job=%s", job["id"])
            self.store.event(
                tenant,
                job["id"],
                "step_finished",
                {
                    "kind": job["kind"],
                    "accepted": accepted,
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
            )
        except CodexBusyError as error:
            self.governance.trace(
                tenant,
                job["payload"].get("pipeline_id", job["id"]),
                job["id"],
                job["kind"],
                "deferred",
                time.monotonic() - started,
                {"reason": "codex_executor_busy"},
            )
            self.store.defer(
                tenant,
                job["id"],
                worker_id,
                job["lease_token"],
                retry_delay=error.retry_after,
                reason="codex_executor_busy",
            )
            LOG.info(
                "job_deferred kind=%s job=%s reason=codex_executor_busy", job["kind"], job["id"]
            )
        except Exception as error:
            if isinstance(error, RuntimeError) and str(error).startswith("injected_crash"):
                raise
            # Provider/upstream bodies may contain credentials or private document contents.
            code = type(error).__name__
            self.governance.trace(
                tenant,
                job["payload"].get("pipeline_id", job["id"]),
                job["id"],
                job["kind"],
                "failed",
                time.monotonic() - started,
                {"error_type": code},
            )
            self.store.fail(tenant, job["id"], worker_id, job["lease_token"], code, retry_delay=30)
            LOG.warning("job_failed kind=%s job=%s error_type=%s", job["kind"], job["id"], code)
        finally:
            stopped.set()
            heartbeat.join(timeout=2)
        return cast(dict[str, Any] | None, self.store.get_job(tenant, job["id"]))

    def heartbeat_file(self, worker_id: str) -> None:
        folder = self.root / "heartbeats"
        folder.mkdir(exist_ok=True)
        target = folder / f"{worker_id}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps({"worker_id": worker_id, "timestamp": time.time()}))
        temp.replace(target)
