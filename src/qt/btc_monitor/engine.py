"""Pure bounded microstructure state and explainable research-only grid gates."""

from __future__ import annotations

import math
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise
from typing import Literal, TypedDict

from .config import MonitorConfig
from .models import (
    AnomalyDecision,
    ChannelHealth,
    DecisionRecord,
    Gate,
    Health,
    MarketEvent,
    MonitorSnapshot,
    Source,
)


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _quantile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * fraction
    index = int(position)
    return values[index] + (values[min(index + 1, len(values) - 1)] - values[index]) * (
        position - index
    )


@dataclass
class Bucket:
    second: int
    opened: float
    high: float
    low: float
    close: float
    path: float = 0
    reversals: int = 0
    first_sign: int = 0
    last_sign: int = 0
    count: int = 1
    volume: float = 0

    def add(self, price: float, quantity: float) -> None:
        delta = price - self.close
        direction = _sign(delta)
        self.path += abs(delta)
        if direction:
            if self.last_sign and direction != self.last_sign:
                self.reversals += 1
            if not self.first_sign:
                self.first_sign = direction
            self.last_sign = direction
        self.high, self.low, self.close = max(self.high, price), min(self.low, price), price
        self.count += 1
        self.volume += quantity


class WindowMetrics(TypedDict):
    buckets: int
    high: float | None
    low: float | None
    open: float
    close: float
    path: float
    efficiency: float
    reversals: int
    volume: float
    count: int


class MicroState:
    """One bucket per second; raw trade rate cannot evict the required time horizon."""

    def __init__(self, max_seconds: int = 30000):
        self.buckets: deque[Bucket] = deque(maxlen=max_seconds)

    def clear(self) -> None:
        self.buckets.clear()

    def add(self, event: MarketEvent) -> None:
        assert event.event_time is not None
        second = int(event.event_time)
        if self.buckets and self.buckets[-1].second == second:
            self.buckets[-1].add(event.price, event.quantity)
        else:
            self.buckets.append(
                Bucket(
                    second,
                    event.price,
                    event.price,
                    event.price,
                    event.price,
                    volume=event.quantity,
                )
            )
        while self.buckets and self.buckets[0].second < second - 3600:
            self.buckets.popleft()

    def window(self, seconds: int, cutoff: float) -> WindowMetrics:
        buckets = []
        for bucket in reversed(self.buckets):
            if bucket.second <= cutoff - seconds:
                break
            if bucket.second <= cutoff:
                buckets.append(bucket)
        buckets.reverse()
        if not buckets:
            return {
                "buckets": 0,
                "open": 0.0,
                "close": 0.0,
                "high": None,
                "low": None,
                "path": 0,
                "efficiency": 1,
                "reversals": 0,
                "volume": 0,
                "count": 0,
            }
        path, reversals, last_sign = 0.0, 0, 0
        previous = buckets[0].opened
        for bucket in buckets:
            transition = bucket.opened - previous
            direction = _sign(transition)
            if direction:
                reversals += int(bool(last_sign and direction != last_sign))
                last_sign = direction
            path += abs(transition) + bucket.path
            if bucket.first_sign:
                reversals += int(bool(last_sign and last_sign != bucket.first_sign))
                last_sign = bucket.last_sign
            reversals += bucket.reversals
            previous = bucket.close
        displacement = abs(buckets[-1].close - buckets[0].opened)
        return {
            "buckets": len(buckets),
            "high": max(x.high for x in buckets),
            "low": min(x.low for x in buckets),
            "open": buckets[0].opened,
            "close": buckets[-1].close,
            "path": path,
            "efficiency": displacement / path if path else 1.0,
            "reversals": reversals,
            "volume": sum(x.volume for x in buckets),
            "count": sum(x.count for x in buckets),
        }


class MonitorEngine:
    def __init__(self, config: MonitorConfig, *, boot_id: str | None = None):
        self.config = config
        self.boot_id = boot_id or uuid.uuid4().hex
        self.source = Source.for_config(config)
        self.micro = MicroState(config.ring_size)
        self.channels: dict[str, MarketEvent] = {}
        self.backlogs: dict[str, float] = {}
        self.candles: deque[MarketEvent] = deque(maxlen=config.bootstrap_candles)
        self.errors: Counter[str] = Counter()
        self.coverage_start: float | None = None
        self.gap_reason = "startup_warmup"
        self.last_progress_at: float | None = None
        self.last_shock_mono: float | None = None
        self.last_extreme_mono: float | None = None
        self.events: deque[AnomalyDecision] = deque(maxlen=50)
        self.active: AnomalyDecision | None = None
        self.recovery_start: float | None = None
        self.assessment_revision = 0
        self.last_assessment: DecisionRecord | None = None
        self.pending_notifications: list[dict[str, object]] = []

    def restore(self, snapshot: MonitorSnapshot, now: float, mono: float) -> None:
        """Restore identities only. Persisted prices never restore current readiness."""
        if snapshot.source != self.source or any(
            item.source != self.source for item in snapshot.recent_events
        ):
            self.error("checkpoint_source_mismatch")
            return
        for item in snapshot.recent_events[-50:]:
            self.events.append(item)
        if self.events:
            self.active = self.events[-1]
            if self.active.state in {"WATCH", "CONFIRMED"}:
                self._revise("INVALIDATED", now, self.active.recovery_fraction, "restart_warmup")
            if now < self.active.expires_at:
                self.last_shock_mono = self.last_extreme_mono = mono

    def error(self, reason: str) -> None:
        self.errors[reason] += 1

    def reset(self, reason: str, now: float | None = None) -> None:
        self.micro.clear()
        self.coverage_start = None
        self.gap_reason = reason
        self.channels.pop("trade", None)
        self.channels.pop("quote", None)
        self.channels.pop("candle", None)
        self.backlogs.clear()
        if self.active and self.active.state in {"WATCH", "CONFIRMED"}:
            self._revise(
                "INVALIDATED",
                now if now is not None else self.active.as_of,
                self.active.recovery_fraction,
                reason,
            )

    def ingest(self, event: MarketEvent, now: float, mono: float) -> bool:
        """Reject disorder/loss before path calculations. Duplicate IDs never refresh freshness."""
        cfg = self.config
        if (
            event.received_mono > mono + cfg.max_future_seconds
            or event.received_at > now + cfg.max_future_seconds
        ):
            self.error("future_receive_time")
            self.reset("future_receive_time", now)
            return False
        if event.event_time is not None and event.event_time > now + cfg.max_future_seconds:
            self.error("future_event_time")
            self.reset("future_event_time", now)
            return False
        if event.historical and event.channel != "candle":
            self.error("historical_trade_rejected")
            return False
        if event.channel == "candle":
            if event.close_time is None or event.close_time >= now or event.open_time is None:
                self.error("unclosed_candle")
                return False
            if self.candles and event.sequence <= self.candles[-1].sequence:
                return False
            if self.candles and event.sequence != self.candles[-1].sequence + 60000:
                self.candles.clear()
                self.error("candle_gap")
                self.reset("candle_gap", now)
            self.candles.append(event)
            # History is context only, never a current receive heartbeat.
            if not event.historical:
                self.channels["candle"] = event
                self.backlogs["candle"] = max(0, mono - event.received_mono)
            return True
        if event.channel == "trade" and event.event_time is None:
            self.error("missing_trade_source_time")
            self.reset("missing_trade_source_time", now)
            return False
        previous = self.channels.get(event.channel)
        if previous:
            if event.sequence == previous.sequence:
                self.error("duplicate")
                return False
            if (
                event.sequence < previous.sequence
                or event.received_mono < previous.received_mono
                or (
                    event.event_time is not None
                    and previous.event_time is not None
                    and event.event_time < previous.event_time
                )
            ):
                self.error("out_of_order")
                self.reset("out_of_order", now)
                return False
            if event.channel == "trade" and event.sequence != previous.sequence + 1:
                self.error("trade_sequence_gap")
                self.reset("trade_sequence_gap", now)
            elif (
                event.channel == "trade"
                and event.event_time is not None
                and previous.event_time is not None
                and event.event_time - previous.event_time > cfg.trade_stale_seconds
            ):
                self.error("trade_time_gap")
                self.reset("trade_time_gap", now)
        self.channels[event.channel] = event
        self.backlogs[event.channel] = max(0, mono - event.received_mono)
        if event.channel == "trade":
            if (
                event.event_time is None
                or now - event.event_time > cfg.trade_stale_seconds
                or mono - event.received_mono > cfg.trade_stale_seconds
            ):
                self.error("delayed_trade")
                self.reset("delayed_trade", now)
                return False
            if self.backlogs[event.channel] > cfg.max_backlog_seconds:
                self.error("consumer_backlog")
                self.reset("consumer_backlog", now)
                return False
            if self.coverage_start is None:
                self.coverage_start = mono
            self.micro.add(event)
            self._anomaly(event, now, mono)
        self.last_progress_at = now
        return True

    def _revise(
        self, state: str, now: float, recovery: float, reason: str, extreme: float | None = None
    ) -> None:
        assert self.active is not None
        self.active = self.active.model_copy(
            update={
                "revision": self.active.revision + 1,
                "state": state,
                "as_of": now,
                "recovery_fraction": recovery,
                "reason": reason,
                "extreme": extreme if extreme is not None else self.active.extreme,
            }
        )
        self.events.append(self.active)
        self.recovery_start = None
        if state in {"CONFIRMED", "INVALIDATED"}:
            self.pending_notifications.append(
                {
                    "event_id": self.active.event_id,
                    "revision": self.active.revision,
                    "notification_class": state.lower(),
                    "text": f"{self.source.exchange} {self.source.environment} BTC/USDT spot {state}: {self.active.direction} wick; research only. {reason}. "
                    f"Baseline {self.active.baseline:.2f}, extreme {self.active.extreme:.2f}, recovery {self.active.recovery_fraction:.0%}. "
                    f"As of {datetime.fromtimestamp(self.active.as_of, timezone.utc).isoformat()}; "
                    f"expires {datetime.fromtimestamp(self.active.expires_at, timezone.utc).isoformat()}; "
                    f"ID {self.active.event_id} r{self.active.revision}.",
                    "as_of": self.active.as_of,
                    "decision": self.active.model_dump(mode="json"),
                    "expires_at": self.active.expires_at,
                    "positive": state == "CONFIRMED",
                }
            )

    def _anomaly(self, event: MarketEvent, now: float, mono: float) -> None:
        cfg, price = self.config, event.price
        assert event.event_time is not None
        window = self.micro.window(cfg.anomaly_window_seconds, event.event_time)
        baseline = float(window.get("open", price))
        down = (baseline - price) / baseline * 10000
        up = (price - baseline) / baseline * 10000
        ordinary = self.micro.window(60, event.event_time - cfg.anomaly_window_seconds)
        adaptive_threshold = cfg.wick_threshold_bps
        if ordinary["buckets"] >= 45 and ordinary["high"] and ordinary["low"]:
            adaptive_threshold = max(
                adaptive_threshold, (ordinary["high"] - ordinary["low"]) / baseline * 15000
            )
        impulse = max(down, up) >= adaptive_threshold
        if impulse:
            self.last_shock_mono = mono
        if self.active and self.active.state in {"WATCH", "CONFIRMED"}:
            active = self.active
            if now >= active.expires_at:
                self._revise("EXPIRED", now, active.recovery_fraction, "event_expired")
                return
            extended = (
                price < active.extreme if active.direction == "down" else price > active.extreme
            )
            if extended:
                self.last_shock_mono = self.last_extreme_mono = mono
                if active.state == "CONFIRMED":
                    self._revise("INVALIDATED", now, 0, "renewed_extreme", price)
                else:
                    self._revise("WATCH", now, 0, "extreme_extended", price)
                return
            distance = abs(active.baseline - active.extreme)
            recovery = (
                (
                    (price - active.extreme)
                    if active.direction == "down"
                    else (active.extreme - price)
                )
                / distance
                if distance
                else 0
            )
            if recovery >= cfg.wick_recovery_fraction:
                if self.recovery_start is None:
                    self.recovery_start = mono
                if (
                    active.state == "WATCH"
                    and mono - self.recovery_start >= cfg.confirmation_seconds
                ):
                    self._revise("CONFIRMED", now, recovery, "observed_recovery")
            else:
                self.recovery_start = None
                if active.state == "CONFIRMED":
                    self._revise("INVALIDATED", now, recovery, "recovery_failed")
            return
        if impulse:
            self.last_shock_mono = mono
            if self.active and now < self.active.expires_at:
                return  # alert dedup never suppresses fresh shock/cooldown evidence
            direction: Literal["down", "up"] = "down" if down >= up else "up"
            self.active = AnomalyDecision(
                event_id=f"{self.boot_id}-{event.sequence}",
                revision=1,
                state="WATCH",
                as_of=now,
                expires_at=now + cfg.event_ttl_seconds,
                direction=direction,
                baseline=baseline,
                extreme=price,
                recovery_fraction=0,
                reason="intrasecond_path_impulse",
                source=self.source,
            )
            self.events.append(self.active)
            self.last_shock_mono = self.last_extreme_mono = mono
            self.recovery_start = None

    def snapshot(
        self, now: float, mono: float, *, notifications_configured: bool = False
    ) -> MonitorSnapshot:
        cfg = self.config
        channels: dict[str, ChannelHealth] = {}
        reasons = []
        for name, stale in (("trade", cfg.trade_stale_seconds), ("quote", cfg.quote_stale_seconds)):
            event = self.channels.get(name)
            event_age = now - event.event_time if event and event.event_time is not None else None
            receive_age = mono - event.received_mono if event else None
            backlog = self.backlogs.get(name)
            ready = bool(
                event
                and receive_age is not None
                and 0 <= receive_age <= stale
                and (event_age is None or -cfg.max_future_seconds <= event_age <= stale)
                and backlog is not None
                and backlog <= cfg.max_backlog_seconds
            )
            channels[name] = ChannelHealth(
                last_event_at=event.event_time if event else None,
                last_receive_at=event.received_at if event else None,
                event_age_seconds=event_age,
                receive_age_seconds=receive_age,
                backlog_seconds=backlog,
                ready=ready,
                source_time_known=event.event_time is not None if event else name != "quote",
            )
            if not ready:
                reasons.append(f"{name}_unhealthy")
        last_candle = self.candles[-1] if self.candles else None
        candle_age = (
            now - last_candle.close_time
            if last_candle and last_candle.close_time is not None
            else None
        )
        candle_ready = bool(
            len(self.candles) >= 3
            and candle_age is not None
            and 0 <= candle_age <= cfg.candle_stale_seconds
        )
        candle_event = self.channels.get("candle")
        if candle_event is not None and self.backlogs.get("candle", 0) > cfg.max_backlog_seconds:
            candle_ready = False
        channels["candle"] = ChannelHealth(
            last_event_at=last_candle.close_time if last_candle else None,
            last_receive_at=candle_event.received_at if candle_event else None,
            event_age_seconds=candle_age,
            receive_age_seconds=mono - candle_event.received_mono if candle_event else None,
            backlog_seconds=self.backlogs.get("candle"),
            ready=candle_ready,
        )
        if not candle_ready:
            reasons.append("closed_candle_context_unavailable")
        coverage = mono - self.coverage_start if self.coverage_start is not None else 0
        latest = self.channels.get("trade")
        cutoff = latest.event_time if latest and latest.event_time is not None else now
        windows = {str(seconds): self.micro.window(seconds, cutoff) for seconds in (5, 15, 60)}
        sufficient = (
            coverage >= cfg.minimum_coverage_seconds
            and windows["60"]["buckets"] >= cfg.minimum_trade_buckets
        )
        if not sufficient:
            reasons.extend(["insufficient_micro_coverage", self.gap_reason])
        data_ready = all(item.ready for item in channels.values()) and sufficient
        if self.active and self.active.state in {"WATCH", "CONFIRMED"}:
            if now >= self.active.expires_at:
                self._revise("EXPIRED", now, self.active.recovery_fraction, "event_expired")
            elif not all(item.ready for item in channels.values()):
                self._revise("INVALIDATED", now, self.active.recovery_fraction, "source_unhealthy")
        grid_window = self.micro.window(cfg.grid_horizon_seconds, cutoff)
        grid_sufficient = (
            sufficient
            and coverage >= cfg.grid_minimum_coverage_seconds
            and grid_window["buckets"] >= cfg.grid_minimum_buckets
        )
        assessment = self._assessment(now, mono, grid_window, grid_sufficient, data_ready, reasons)
        if not data_ready:
            self.pending_notifications = [
                item for item in self.pending_notifications if not item["positive"]
            ]
        self.last_progress_at = now  # reached only after a real bounded assessment/health pass
        health = Health(
            data_ready=data_ready,
            assessment_ready=data_ready and grid_sufficient,
            status="ready" if data_ready else "degraded" if sufficient else "warming_up",
            reasons=list(dict.fromkeys(reasons)),
            channels=channels,
            coverage_seconds=coverage,
            last_progress_at=now,
            errors=dict(self.errors),
        )
        quote = self.channels.get("quote")
        spread = (
            (quote.ask - quote.bid) / quote.price * 10000
            if quote and quote.ask and quote.bid
            else None
        )
        return MonitorSnapshot(
            config_version=cfg.version,
            boot_id=self.boot_id,
            published_at=now,
            source=self.source,
            health=health,
            micro={
                "price": latest.price if latest else None,
                "spread_bps": spread,
                "windows": windows,
            },
            assessment=assessment,
            recent_events=list(self.events),
            context={
                "closed_candles": len(self.candles),
                "latest_closed_candle_at": last_candle.close_time if last_candle else None,
                "grid_horizon_seconds": cfg.grid_horizon_seconds,
                "grid_observed_buckets": grid_window["buckets"],
                "cross_venue": {"available": False, "reason": "not_configured"},
                "notifications_configured": notifications_configured,
                "tick_size_assumed": True,
                "tick_size": cfg.tick_size,
                "sensitivity_preset": "balanced",
                "thresholds_calibrated": False,
                "freshness_limits": {
                    key: getattr(cfg, key)
                    for key in (
                        "trade_stale_seconds",
                        "quote_stale_seconds",
                        "candle_stale_seconds",
                        "max_backlog_seconds",
                        "max_future_seconds",
                    )
                },
            },
        )

    def _assessment(
        self,
        now: float,
        mono: float,
        window: WindowMetrics,
        sufficient: bool,
        healthy: bool,
        reasons: list[str],
    ) -> DecisionRecord:
        cfg = self.config
        buckets = [
            item
            for item in self.micro.buckets
            if now - cfg.grid_horizon_seconds < item.second <= now
        ]
        prices = [item.close for item in buckets]
        lower = _quantile(prices, cfg.range_quantile) if prices else None
        upper = _quantile(prices, 1 - cfg.range_quantile) if prices else None
        price = prices[-1] if prices else None
        quote = self.channels.get("quote")
        spread = (
            (quote.ask - quote.bid) / quote.price * 10000
            if quote and quote.ask and quote.bid
            else None
        )
        cost = (
            2 * cfg.fee_per_side_bps
            + 2 * cfg.slippage_per_side_bps
            + cfg.cost_buffer_bps
            + (spread or 0)
        )
        spacing = (
            (math.floor((upper - lower) / (cfg.grid_levels - 1) / cfg.tick_size) * cfg.tick_size)
            / price
            * 10000
            if price and upper and lower
            else None
        )
        net = spacing - cost if spacing is not None else None
        cooldown = mono - self.last_shock_mono if self.last_shock_mono is not None else None
        # Require recent robust bounds to remain inside earlier range; no new trend expansion.
        old = prices[: max(1, len(prices) // 2)]
        recent = prices[max(1, len(prices) // 2) :]
        tolerance = (upper - lower) * 0.1 if lower and upper else 0
        stable = bool(
            old
            and recent
            and _quantile(recent, 0.95) <= _quantile(old, 0.95) + tolerance
            and _quantile(recent, 0.05) >= _quantile(old, 0.05) - tolerance
        )
        gates = [
            Gate(
                name="fresh_coverage",
                passed=healthy,
                value=window["buckets"],
                threshold=cfg.minimum_trade_buckets,
            ),
            Gate(
                name="shock_stabilized",
                passed=stable and (cooldown is None or cooldown >= cfg.shock_cooldown_seconds),
                value={"stable_range": stable, "seconds_since_shock": cooldown},
                threshold=cfg.shock_cooldown_seconds,
            ),
            Gate(
                name="two_way_motion",
                passed=window["reversals"] >= cfg.minimum_reversals
                and window["efficiency"] <= cfg.maximum_efficiency,
                value={
                    "reversals": window["reversals"],
                    "directional_efficiency": window["efficiency"],
                },
                threshold={
                    "minimum_reversals": cfg.minimum_reversals,
                    "maximum_efficiency": cfg.maximum_efficiency,
                },
            ),
            Gate(
                name="inside_robust_range",
                passed=bool(
                    price and lower and upper and lower <= price <= upper and upper > lower
                ),
                value=price,
                threshold={"lower": lower, "upper": upper},
            ),
            Gate(
                name="spread",
                passed=spread is not None and spread <= cfg.maximum_spread_bps,
                value=spread,
                threshold=cfg.maximum_spread_bps,
            ),
            Gate(
                name="net_cost_edge",
                passed=net is not None and net > 0,
                value=spacing,
                threshold=cost,
            ),
        ]
        gates.insert(
            1,
            Gate(
                name="range_history",
                passed=sufficient,
                value=window["buckets"],
                threshold=cfg.grid_minimum_buckets,
            ),
        )
        failed = [gate.name for gate in gates if not gate.passed]
        state: Literal["INSUFFICIENT_DATA", "DATA_UNHEALTHY", "UNSUITABLE", "CANDIDATE"] = (
            "INSUFFICIENT_DATA"
            if not sufficient
            else "DATA_UNHEALTHY"
            if not healthy
            else "UNSUITABLE"
            if failed
            else "CANDIDATE"
        )
        self.assessment_revision += 1
        record = DecisionRecord(
            source=self.source,
            assessment_id=f"{self.boot_id}-grid",
            revision=self.assessment_revision,
            as_of=now,
            expires_at=now + cfg.decision_ttl_seconds,
            state=state,
            reasons=list(dict.fromkeys(reasons + failed)),
            gates=gates,
            evidence={
                "directional_efficiency": window["efficiency"],
                "reversals": window["reversals"],
                "path": window["path"],
                "realized_volatility_bps": math.sqrt(
                    sum(math.log(b / a) ** 2 for a, b in pairwise(prices))
                )
                * 10000,
                "range_width_bps": (upper - lower) / price * 10000
                if price and upper and lower
                else None,
                "round_trip_cost_bps": cost,
                "fee_per_side_bps": cfg.fee_per_side_bps,
                "slippage_per_side_bps": cfg.slippage_per_side_bps,
                "cost_buffer_bps": cfg.cost_buffer_bps,
                "spread_bps": spread,
                "coverage_seconds": mono - self.coverage_start
                if self.coverage_start is not None
                else 0,
                "thresholds": "assumed_calibration_starting_points",
                "cross_venue_confirmation": "unavailable",
            },
            range_lower=lower,
            range_upper=upper,
            spacing_bps=spacing,
            net_spacing_bps=net,
            levels=cfg.grid_levels,
        )
        if (
            self.last_assessment
            and self.last_assessment.state == "CANDIDATE"
            and state != "CANDIDATE"
        ):
            self.pending_notifications.append(
                {
                    "event_id": record.assessment_id,
                    "revision": record.revision,
                    "notification_class": "withdrawn",
                    "text": f"BTC/USDT spot grid research candidate withdrawn: {', '.join(record.reasons)}. "
                    f"As of {datetime.fromtimestamp(record.as_of, timezone.utc).isoformat()}; "
                    f"expires {datetime.fromtimestamp(record.expires_at, timezone.utc).isoformat()}; "
                    f"ID {record.assessment_id} r{record.revision}.",
                    "as_of": record.as_of,
                    "decision": record.model_dump(mode="json"),
                    "expires_at": record.expires_at,
                    "positive": False,
                }
            )
        # An uninterrupted candidate is refreshed by observations, not repeatedly notified.
        if state == "CANDIDATE" and (
            self.last_assessment is None or self.last_assessment.state != state
        ):
            assert (
                record.range_lower is not None
                and record.range_upper is not None
                and record.net_spacing_bps is not None
            )
            self.pending_notifications.append(
                {
                    "event_id": record.assessment_id,
                    "revision": record.revision,
                    "notification_class": "candidate",
                    "text": "BTC/USDT spot: market range passes assumed grid research gates. No execution permission or profit guarantee. "
                    f"Range {record.range_lower:.2f}-{record.range_upper:.2f}; net spacing {record.net_spacing_bps:.1f} bps after assumed costs. "
                    f"As of {datetime.fromtimestamp(record.as_of, timezone.utc).isoformat()}; "
                    f"expires {datetime.fromtimestamp(record.expires_at, timezone.utc).isoformat()}; "
                    f"ID {record.assessment_id} r{record.revision}.",
                    "as_of": record.as_of,
                    "decision": record.model_dump(mode="json"),
                    "expires_at": record.expires_at,
                    "positive": True,
                }
            )
        self.last_assessment = record
        return record

    def drain_notifications(self) -> list[dict[str, object]]:
        pending, self.pending_notifications = self.pending_notifications, []
        return pending
