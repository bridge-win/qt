"""Single-writer monitoring loop with bounded execution and durable reconciliation."""

import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Literal, Protocol

from qt.btc_agent.broker import Fill, Quote
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.reviewer import Review
from qt.btc_agent.signals import DAY_MS, Candle, Signal, evaluate
from qt.btc_agent.store import Position, Store


class Broker(Protocol):
    def quote(self) -> Quote: ...
    def balances(self) -> dict[str, float]: ...
    def prepare_price(self, price: float, side: Literal["buy", "sell"]) -> float: ...
    def prepare_quantity(self, qty: float, price: float) -> float: ...
    def is_dust(self, qty: float, price: float) -> bool: ...
    def submit(
        self, client_id: str, side: Literal["buy", "sell"], qty: float, limit_price: float
    ) -> Fill: ...
    def lookup(self, client_id: str) -> Fill | None: ...


def check_quote(book: Quote, cfg: AgentConfig, now_ms: int) -> None:
    if not all(math.isfinite(v) and v > 0 for v in (book.bid, book.ask)) or book.bid > book.ask:
        raise ValueError("invalid market quote")
    if not 0 <= now_ms - book.timestamp_ms <= cfg.max_quote_age_seconds * 1000:
        raise ValueError("stale or future market quote")
    if (book.ask / book.bid - 1) * 10_000 > cfg.max_spread_bps:
        raise ValueError("spread exceeds configured limit")


class Service:
    def __init__(
        self,
        cfg: AgentConfig,
        store: Store,
        broker: Broker,
        load_candles: Callable[[], list[Candle]],
        review: Callable[[Signal], Review],
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.cfg, self.store, self.broker = cfg, store, broker
        self.load_candles, self.review, self.clock = load_candles, review, clock

    def cycle(self) -> dict[str, object]:
        try:
            result = self._cycle()
        except Exception as exc:
            # Provider exceptions can contain signed URLs; never log their text.
            result = {"status": "blocked", "reason": type(exc).__name__}
        result = {"test_environment": self.cfg.test_environment, **result}
        self.store.event(self.clock(), result)
        return result

    def _cycle(self) -> dict[str, object]:
        for row in self.store.pending():
            fill = self.broker.lookup(row["client_id"])
            if fill is None or not self.store.finish(fill):
                return {
                    "status": "blocked",
                    "reason": "unresolved_order",
                    "client_id": row["client_id"],
                }
        if self.cfg.kill_file.exists():
            return {"status": "halted", "reason": "kill_file"}
        now = self.clock()
        book = self.broker.quote()
        check_quote(book, self.cfg, self.clock())
        balances = self.broker.balances()
        if any(not math.isfinite(balances[c]) or balances[c] < 0 for c in ("BTC", "USDT")):
            raise ValueError("invalid balances")
        pos = self.store.position()
        dust = self.dust_qty()
        if balances["BTC"] + 1e-10 < pos.qty + dust:
            return {"status": "blocked", "reason": "inventory_mismatch_or_testnet_reset"}
        if pos.qty > 0 and pos.exiting and self.settle_dust(book.bid):
            return {
                "status": "watching",
                "reason": "exit_residual_recorded_as_dust",
                "dust_btc": self.dust_qty(),
            }
        exit_reason = ""
        if pos.qty > 0:
            pos.high_water = max(pos.high_water, book.bid)
            entry = pos.cost / pos.qty
            if pos.exiting:
                exit_reason = "continue_exit"
            elif book.bid <= entry * (1 - self.cfg.stop_loss):
                exit_reason = "stop_loss"
            elif pos.high_water >= entry * (
                1 + self.cfg.trailing_activation
            ) and book.bid <= pos.high_water * (1 - self.cfg.trailing_loss):
                exit_reason = "trailing_stop"
            elif now - pos.opened_ms >= self.cfg.max_hold_days * DAY_MS:
                exit_reason = "time_exit"
            with self.store.db:
                self.store.put("position", asdict(pos))
        # Protective exits remain available when the daily feed or AI is unavailable.
        if exit_reason:
            return self.execute("sell", exit_reason)
        signal = evaluate(self.load_candles(), self.clock(), has_position=pos.qty > 0)
        snapshot: dict[str, object] = {"status": "watching", "signal": asdict(signal)}
        if signal.action == "sell":
            return self.execute("sell", signal.reason)
        if signal.action != "buy":
            return snapshot
        if now - pos.last_buy_ms < self.cfg.cooldown_days * DAY_MS:
            return {**snapshot, "reason": "entry_cooldown"}
        # A durable per-bar latch limits cost and disallows repeated attempts after veto/failure.
        if self.store.get("reviewed_bar") == signal.bar_time:
            return {**snapshot, "reason": "daily_candidate_already_reviewed"}
        with self.store.db:
            self.store.put("reviewed_bar", signal.bar_time)
        audit: dict[str, object] = {
            "bar_time": signal.bar_time,
            "reviewed_at": self.clock(),
            "prompt_version": "btc-entry-v1",
            "input": asdict(signal),
            "input_sha256": hashlib.sha256(
                json.dumps(asdict(signal), sort_keys=True).encode()
            ).hexdigest(),
        }
        try:
            review = self.review(signal)
        except Exception as exc:
            self.store.review(
                self.clock(), {**audit, "approve": False, "error": type(exc).__name__}
            )
            raise
        self.store.review(self.clock(), {**audit, **review.model_dump()})
        if not review.approve:
            return {**snapshot, "reason": "codex_veto", "review": review.model_dump()}
        return self.execute("buy", signal.reason, reference_price=signal.price)

    def dust_qty(self) -> float:
        value = self.store.get("dust_btc")
        return float(value) if isinstance(value, (int, float)) else 0.0

    def settle_dust(self, price: float) -> bool:
        pos = self.store.position()
        if (
            not pos.exiting
            or pos.qty <= 0
            or self.store.pending()
            or not self.broker.is_dust(pos.qty, price)
        ):
            return False
        # Dust is retained as owned inventory, never silently written off or sold.
        with self.store.db:
            self.store.put("dust_btc", self.dust_qty() + pos.qty)
            old_cost = self.store.get("dust_cost")
            self.store.put(
                "dust_cost",
                (float(old_cost) if isinstance(old_cost, (int, float)) else 0.0) + pos.cost,
            )
            self.store.put("position", asdict(Position(last_buy_ms=pos.last_buy_ms)))
        return True

    def execute(
        self, side: Literal["buy", "sell"], reason: str, *, reference_price: float | None = None
    ) -> dict[str, object]:
        """All order paths, including the explicit testnet smoke, pass the same limits."""
        if self.cfg.kill_file.exists() or self.store.pending():
            raise ValueError("orders blocked by halt or unresolved intent")
        now = self.clock()
        book = self.broker.quote()  # refresh after potentially slow Codex review
        check_quote(book, self.cfg, self.clock())
        if (
            side == "buy"
            and reference_price is not None
            and (
                not math.isfinite(reference_price)
                or reference_price <= 0
                or abs(book.ask / reference_price - 1) > self.cfg.max_entry_deviation
            )
        ):
            raise ValueError("execution price has moved too far from the daily candidate")
        pos = self.store.position()
        balances = self.broker.balances()
        if any(not math.isfinite(balances[c]) or balances[c] < 0 for c in ("BTC", "USDT")):
            raise ValueError("invalid balances")
        offset = self.cfg.limit_offset_bps / 10_000
        price = self.broker.prepare_price(
            book.ask * (1 + offset) if side == "buy" else book.bid * (1 - offset), side
        )
        budget = self.cfg.max_order_quote / (1 + self.cfg.fee_reserve)
        if side == "buy":
            if pos.qty > 0 or now - pos.last_buy_ms < self.cfg.cooldown_days * DAY_MS:
                raise ValueError("position or cooldown blocks entry")
            budget = min(
                budget,
                balances["USDT"] / (1 + self.cfg.fee_reserve),
                (self.cfg.max_position_quote - self.dust_qty() * book.ask)
                / (1 + self.cfg.fee_reserve),
                (self.cfg.max_daily_buy_quote - self.store.daily_buys(now))
                / (1 + self.cfg.fee_reserve),
            )
            qty = budget / price
        else:
            if balances["BTC"] + 1e-10 < pos.qty + self.dust_qty():
                raise ValueError("inventory mismatch")
            qty = min(pos.qty, budget / price)
        qty = self.broker.prepare_quantity(qty, price)
        if not all(math.isfinite(v) and v > 0 for v in (qty, price)) or qty * price > budget * (
            1 + 1e-9
        ):
            raise ValueError("rounded order violates limit")
        # Recheck immediately before writing an intent and handing it to the broker.
        check_quote(book, self.cfg, self.clock())
        if self.cfg.kill_file.exists():
            raise ValueError("halted before submission")
        client_id = f"qtc{uuid.uuid4().hex[:28]}"
        if side == "sell":
            pos.exiting = True
            with self.store.db:
                self.store.put("position", asdict(pos))
        self.store.intent(client_id, side, qty, price, now)
        fill = self.broker.submit(client_id, side, qty, price)
        finished = self.store.finish(fill)
        status = "pending"
        if finished:
            status = "executed" if fill.filled_qty > 0 else "no_fill"
        return {
            "status": status,
            "reason": reason,
            "fill": asdict(fill),
        }
