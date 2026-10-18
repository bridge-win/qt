"""Independent alert loop; review approval never grants any order authority."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from .knowledge import evidence_for
from .models import Bar, Candidate, Policy, Quote, ReviewBatch, digest
from .reviewer import PROMPT_VERSION, call_reviewer, validate_reviews
from .signals import evaluate, revalidate
from .store import Store
from .telegram import Telegram


class Market(Protocol):
    def packet(self, product: Literal["spot", "perpetual"], now: float, policy: Policy) -> tuple[dict[str, list[Bar]], Quote]: ...
    def quote(self, product: Literal["spot", "perpetual"], now: float, policy: Policy) -> Quote: ...
    def stream_ready(self, now: float) -> bool: ...


def render_notification(candidate: Candidate, summary: str) -> str:
    direction = {"long": "买入研究", "short": "做空研究", "reduce_long": "现货减仓研究"}[candidate.direction]
    note = "\n减仓仅适用于已有 BTC 持仓。" if candidate.direction == "reduce_long" else ""
    triggers = "\n".join(
        f"- {item.strategy_id}：观察 {item.observations}；阈值 {item.thresholds}" for item in candidate.triggers
    )
    sources = evidence_for([item.strategy_id for item in candidate.triggers], perpetual=candidate.direction == "short")
    urls = list(dict.fromkeys(source["url"] for item in sources for source in item.get("sources", [])))
    return (
        f"QT BTC｜{direction}｜高证据等级\n{candidate.venue} / {candidate.product}\n"
        f"参考价 {candidate.reference_price:.2f}；失效位 {candidate.invalidation_price:.2f}；"
        f"目标参考 {candidate.target_price:.2f}\n扣假设成本后收益风险比 {candidate.net_reward_risk:.2f}\n"
        f"{triggers}\nCodex：{summary}\n"
        f"证据：{' '.join(urls[:3])}\n"
        f"触发时间 {candidate.bar_close}；入场参考有效至 {int(candidate.expires_at)}\n"
        f"ID {candidate.candidate_id}；策略 {candidate.strategy_version}{note}\n"
        "研究提醒，未自动下单；证据等级不代表获利概率。"
    )[:4000]


class AlertService:
    def __init__(
        self, store: Store, market: Market, telegram: Telegram,
        reviewer: Callable[..., dict[str, Any]] = call_reviewer,
        clock: Callable[[], float] = time.time,
    ):
        self.store, self.market, self.telegram, self.reviewer, self.clock = store, market, telegram, reviewer, clock

    def tick(self) -> dict[str, Any]:
        now = self.clock()
        revision, policy = self.store.policy()
        state: dict[str, Any] = {
            "at": now, "mode": policy.mode, "revision": revision, "status": "paused",
            "strategy_version": policy.strategy_version, "live_execution_enabled": False,
            "notifications_configured": self.telegram.configured,
            "recipient_verified": self.telegram.verified(self.store), "markets": {},
        }
        self.store.expire_and_retain(now)
        if policy.mode != "off":
            if self.store.get(f"shadow_started:{policy.strategy_version}") is None:
                self.store.put(f"shadow_started:{policy.strategy_version}", now)
            for product in ("spot", "perpetual"):
                key = f"evaluated:{policy.strategy_version}:{product}"
                bar_close = int(now // 900) * 900
                if self.store.get(key) == bar_close:
                    state["markets"][product] = self.store.get(f"market_status:{product}", {})
                    continue
                try:
                    bars, quote = self.market.packet(product, now, policy)
                    if product == "spot" and not self.market.stream_ready(self.clock()):
                        raise ValueError("spot_websocket_not_current")
                    candidates = evaluate(bars, quote, policy, self.clock())
                    for candidate in candidates:
                        self.store.add_candidate(candidate, now)
                    self.store.put(key, bar_close)
                    state["markets"][product] = {"ready": True, "as_of": self.clock(), "candidates": len(candidates), "bar_close": bar_close}
                except Exception as error:
                    state["markets"][product] = {"ready": False, "as_of": self.clock(), "reason": str(error) if isinstance(error, ValueError) and str(error) in {
                        "spot_websocket_not_current", "insufficient_closed_history", "stale_bars",
                        "noncontiguous_bars", "quote_stale_or_spread", "funding_context_insufficient",
                        "perpetual_context_unavailable", "opposing_triggers", "closed_bar_revision_requires_research",
                    } else type(error).__name__}
                self.store.put(f"market_status:{product}", state["markets"][product])
            self._review(policy)
            self.dispatch()
            state["status"] = "watching" if any(item.get("ready") for item in state["markets"].values()) else "data_unavailable"
        state["at"] = self.clock()
        state["next_allowed_at"] = self.store.next_allowed(self.telegram.recipient_key)
        state["reviewer"] = self.store.get("reviewer_status", {"authenticated": False, "reason": "not_probed"})
        self.store.put("worker_status", state)
        return state

    def _review(self, policy: Policy) -> None:
        now = self.clock()
        candidates = self.store.candidates(now)
        if not candidates:
            return
        batch_id = f"{policy.strategy_version}:{max(item.bar_close for item in candidates)}"
        payload: dict[str, Any] = {
            "schema_version": 1, "candidates": [item.model_dump() for item in candidates],
            "evidence": {item.candidate_id: self.store.knowledge([value.strategy_id for value in item.triggers], perpetual=item.direction == "short") for item in candidates},
            "validation": {item.candidate_id: {
                value.strategy_id: self.store.validation(item.strategy_version, value.strategy_id, item.direction)
                for value in item.triggers
            } for item in candidates},
            "prompt_version": PROMPT_VERSION, "as_of": now,
        }
        audit = {"input": payload, "input_sha256": digest(payload), "status": "claimed"}
        if not self.store.review_budget(batch_id, now, audit):
            for candidate in candidates:
                self.store.record(candidate.candidate_id, "budget_blocked", "review_batch_or_budget_limit", now)
            return
        for candidate in candidates:
            self.store.record(candidate.candidate_id, "reviewing", "", now)
        try:
            response = self.reviewer(payload)
            batch = ReviewBatch.model_validate(response["result"])
            validate_reviews(batch, candidates)
            self.store.finish_review(batch_id, {**audit, "status": "completed", "output": response})
            self.store.put("reviewer_status", {
                "authenticated": True, "at": self.clock(), "model": response.get("model"),
                "model_pinned": response.get("model_pinned", False), "prompt_version": PROMPT_VERSION,
            })
            if response.get("model_pinned"):
                self.store.start_review_shadow(policy.strategy_version, str(response.get("model", "")), self.clock())
        except Exception as error:
            self.store.finish_review(batch_id, {**audit, "status": "failed", "error": type(error).__name__})
            self.store.put("reviewer_status", {"authenticated": False, "at": self.clock(), "reason": type(error).__name__})
            for candidate in candidates:
                self.store.record(candidate.candidate_id, "review_unavailable", type(error).__name__, self.clock())
            return
        conflict = any(item.direction == "long" for item in candidates) and any(item.direction != "long" for item in candidates)
        by_id = {item.candidate_id: item for item in batch.reviews}
        for candidate in candidates:
            review = by_id[candidate.candidate_id]
            recorded_review = {**review.model_dump(), "audit": {
                "model": response.get("model"), "model_pinned": response.get("model_pinned", False),
                "prompt_version": PROMPT_VERSION, "input_sha256": digest(payload),
            }}
            reasons = self.store.promotion_reasons(candidate, self.clock())
            if not response.get("model_pinned"):
                reasons.append("model_identity_not_pinned")
            if candidate.missing_evidence:
                reasons.extend(candidate.missing_evidence)
            if conflict:
                reasons.append("opposing_directions")
            if review.verdict != "approve":
                self.store.record(candidate.candidate_id, "review_rejected", review.verdict, self.clock(), recorded_review)
            elif reasons:
                self.store.record(candidate.candidate_id, "evidence_pending", ",".join(reasons), self.clock(), recorded_review)
            elif policy.mode == "shadow":
                self.store.record(candidate.candidate_id, "shadow_approved", "shadow_mode", self.clock(), recorded_review)
            else:
                self.store.record(candidate.candidate_id, "approved", "", self.clock(), recorded_review)
                self.store.enqueue(candidate.candidate_id, render_notification(candidate, review.summary), candidate.expires_at, self.clock())

    def dispatch(self) -> None:
        if not self.telegram.verified(self.store):
            return
        for item in self.store.pending(self.clock()):
            _, policy = self.store.policy()
            if policy.mode == "off":
                return
            if item["attempts"] >= 3:
                self.store.sent(item["event_id"], self.telegram.recipient_key, self.clock(), "failed", error_code="attempt_limit")
                continue
            strategy_version = None
            if not item["is_test"]:
                event = self.store.event(item["event_id"])
                if event is None:
                    continue
                candidate = Candidate.model_validate(event["payload"])
                strategy_version = candidate.strategy_version
                reasons = self.store.promotion_reasons(candidate, self.clock())
                reviewer_status = self.store.get("reviewer_status", {})
                if not reviewer_status.get("authenticated") or not reviewer_status.get("model_pinned") or not 0 <= self.clock() - reviewer_status.get("at", 0) <= 86400:
                    reasons.append("reviewer_identity_unavailable")
                try:
                    bars, quote = self.market.packet(candidate.product, self.clock(), policy)
                    snapshot = digest({key: [bar.model_dump() for bar in rows] for key, rows in bars.items()})
                    if snapshot != candidate.data_snapshot_hash:
                        reasons.append("market_snapshot_changed")
                    reasons.extend(revalidate(candidate, quote, policy, self.clock()))
                    if candidate.product == "spot" and not self.market.stream_ready(self.clock()):
                        reasons.append("spot_websocket_not_current")
                except Exception:
                    reasons.append("quote_unavailable")
                if reasons:
                    self.store.sent(item["event_id"], self.telegram.recipient_key, self.clock(), "obsolete", error_code="send_revalidation_failed")
                    continue
                if policy.mode != "notify":
                    continue
            if not self.store.claim(item["event_id"], self.telegram.recipient_key, self.clock(), strategy_version=strategy_version):
                self.store.record(item["event_id"], "cooldown", "hourly_limit_or_policy", self.clock())
                continue
            try:
                result = self.telegram.send(item["payload"]["text"])
            except Exception:
                result = {"status": "unknown", "error_code": "transport_unknown"}
            status = result["status"]
            retry_at = self.clock() + result.get("retry_after", 0)
            if status == "pending" and retry_at >= item["expires_at"]:
                status = "expired"
            self.store.sent(
                item["event_id"], self.telegram.recipient_key, self.clock(), status,
                message_id=result.get("message_id"), error_code=result.get("error_code"), retry_at=retry_at,
            )
