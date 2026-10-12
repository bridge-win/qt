"""Durable trading, reviewer gating, and protective exits across worker restarts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from qt.btc_agent.broker import Fill, Quote
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.paper import PaperBroker
from qt.btc_agent.reviewer import Review
from qt.btc_agent.service import Service
from qt.btc_agent.signals import DAY_MS, Candle, Signal
from qt.btc_agent.store import Store


@dataclass
class Clock:
    now: int = 210 * DAY_MS

    def __call__(self) -> int:
        return self.now


@dataclass
class Book:
    clock: Clock
    bid: float = 50_000
    ask: float = 50_001
    age_ms: int = 0

    def __call__(self) -> Quote:
        return Quote(self.bid, self.ask, self.clock() - self.age_ms)


@dataclass
class Harness:
    cfg: AgentConfig
    store: Store
    broker: PaperBroker
    worker: Service
    clock: Clock
    book: Book
    reviews: list[Signal] = field(default_factory=list)


def _candles(prices: list[float]) -> list[Candle]:
    return [Candle(index * DAY_MS, prices[max(index - 1, 0)],
                   max(prices[max(index - 1, 0)], close) * 1.01,
                   min(prices[max(index - 1, 0)], close) * 0.99, close, 100.0)
            for index, close in enumerate(prices)]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Harness]:
    cfg = AgentConfig(state_dir=tmp_path, mode="paper")
    store = Store(cfg.database)
    clock = Clock()
    book = Book(clock)
    broker = PaperBroker(store, book)
    reviews: list[Signal] = []

    def review(signal: Signal) -> Review:
        reviews.append(signal)
        return Review(approve=True, reason="Consistent synthetic test data")

    worker = Service(cfg, store, broker, lambda: _candles([50_000.0] * 210), review, clock)
    yield Harness(cfg, store, broker, worker, clock, book, reviews)
    store.close()


def _buy_signal(env: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def evaluate(candles: list[Candle], now_ms: int, *, has_position: bool = False) -> Signal:
        return Signal("hold" if has_position else "buy", "Synthetic buy candidate",
                      now_ms // DAY_MS * DAY_MS - DAY_MS, env.book.ask, 1000, {"stabilized": True})
    monkeypatch.setattr("qt.btc_agent.service.evaluate", evaluate)


def _unavailable(*args: object) -> object:
    raise RuntimeError("provider URL containing secret must not appear in status")


def test_full_synthetic_capitulation_cycle_and_durable_restart(env: Harness) -> None:
    env.worker.load_candles = lambda: _candles([50_000.0] * 208 + [30_000.0, 30_500.0])
    env.book.bid, env.book.ask = 30_500, 30_501
    result = env.worker.cycle()
    assert result["status"] == "executed"
    assert len(env.reviews) == 1 and env.reviews[0].action == "buy"
    position = env.store.position()
    balances = env.broker.balances()
    assert position.qty > 0 and position.cost <= env.cfg.max_order_quote
    assert balances["BTC"] == pytest.approx(position.qty)
    assert 1000 - balances["USDT"] == pytest.approx(position.cost)

    reopened = Store(env.cfg.database)
    try:
        broker = PaperBroker(reopened, env.book)
        restarted = Service(env.cfg, reopened, broker, env.worker.load_candles,
                            env.worker.review, env.clock)
        assert reopened.position() == position
        assert broker.balances() == balances
        assert restarted.cycle()["status"] == "watching"
        assert len(env.reviews) == 1
        assert reopened.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
    finally:
        reopened.close()


def test_paper_fill_and_store_finish_are_idempotent_after_restart(env: Harness) -> None:
    env.store.intent("durable-1", "buy", 0.0004, 50_100, env.clock())
    fill = env.broker.submit("durable-1", "buy", 0.0004, 50_100)
    # Simulate a crash after the exchange accepts but before inventory commits.
    assert env.store.position().qty == 0
    reopened = Store(env.cfg.database)
    try:
        broker = PaperBroker(reopened, env.book)
        recovered = broker.lookup("durable-1")
        assert recovered == fill
        assert reopened.finish(fill)
        expected = reopened.position()
        assert reopened.finish(fill)
        assert reopened.position() == expected
        assert broker.submit("durable-1", "buy", 0.0004, 50_100) == fill
        assert reopened.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1
        assert reopened.daily_buys(env.clock()) == pytest.approx(fill.quote_qty + fill.fee_quote)
    finally:
        reopened.close()


def test_partial_fill_waits_for_terminal_and_applies_once(env: Harness) -> None:
    env.store.intent("partial-1", "buy", 0.001, 50_100, env.clock())
    partial = Fill("partial-1", "buy", "PARTIALLY_FILLED", 0.0002, 10, fee_quote=0.01)
    assert env.store.finish(partial) is False
    assert len(env.store.pending()) == 1
    assert env.store.position().qty == 0
    terminal = Fill("partial-1", "buy", "EXPIRED", 0.0004, 20, fee_btc=0.0000004)
    assert env.store.finish(terminal)
    assert env.store.position().qty == pytest.approx(0.0003996)
    assert env.store.position().cost == 20
    assert env.store.pending() == []
    assert env.store.finish(terminal)
    assert env.store.position().cost == 20


class TimeoutBroker(PaperBroker):
    def __init__(self, original: PaperBroker, *, accepted: bool = False):
        super().__init__(original.store, original.quote)
        self.calls = 0
        self.accepted = accepted

    def submit(self, client_id: str, side: Literal["buy", "sell"], qty: float, limit_price: float) -> Fill:
        self.calls += 1
        if self.accepted:
            super().submit(client_id, side, qty, limit_price)
        raise TimeoutError("signature=must-never-be-logged")


@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_submit_survives_restart_without_duplicate(
    env: Harness, monkeypatch: pytest.MonkeyPatch, accepted: bool,
) -> None:
    _buy_signal(env, monkeypatch)
    broker = TimeoutBroker(env.broker, accepted=accepted)
    env.worker.broker = broker
    result = env.worker.cycle()
    assert result == {"status": "blocked", "reason": "TimeoutError"}
    assert len(env.store.pending()) == 1
    assert broker.calls == 1

    reopened = Store(env.cfg.database)
    try:
        recovered_broker = PaperBroker(reopened, env.book)
        restarted = Service(env.cfg, reopened, recovered_broker, env.worker.load_candles,
                            env.worker.review, env.clock)
        next_result = restarted.cycle()
        if accepted:
            assert next_result["status"] == "watching"
            assert reopened.position().qty > 0
            assert reopened.pending() == []
        else:
            assert next_result["reason"] == "unresolved_order"
            assert restarted.cycle()["reason"] == "unresolved_order"
            assert len(reopened.pending()) == 1
        assert reopened.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
        assert len(env.reviews) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("unavailable", [False, True])
def test_veto_or_unavailable_reviewer_blocks_and_latches_bar(
    env: Harness, monkeypatch: pytest.MonkeyPatch, unavailable: bool,
) -> None:
    _buy_signal(env, monkeypatch)
    calls: list[Signal] = []

    def review(signal: Signal) -> Review:
        calls.append(signal)
        if unavailable:
            raise ConnectionError("reviewer unavailable")
        return Review(approve=False, reason="Insufficient evidence")

    env.worker.review = review
    result = env.worker.cycle()
    assert result["reason"] == ("ConnectionError" if unavailable else "codex_veto")
    assert env.worker.cycle()["reason"] == "daily_candidate_already_reviewed"
    assert env.store.pending() == [] and env.store.position().qty == 0
    assert env.broker.balances()["USDT"] == 1000
    assert len(calls) == 1
    env.clock.now += DAY_MS
    env.worker.cycle()
    assert len(calls) == 2


@pytest.mark.parametrize("reason", ["stop_loss", "time_exit", "trailing_stop"])
def test_protective_exit_works_without_candles_or_reviewer(
    env: Harness, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    env.worker.execute("buy", "seed")
    pos = env.store.position()
    if reason == "stop_loss":
        env.book.bid, env.book.ask = 40_000, 40_001
    elif reason == "time_exit":
        env.clock.now += env.cfg.max_hold_days * DAY_MS
    else:
        pos.high_water = 70_000
        with env.store.db:
            env.store.put("position", asdict(pos))
        env.book.bid, env.book.ask = 55_000, 55_001
    monkeypatch.setattr(env.worker, "load_candles", _unavailable)
    monkeypatch.setattr(env.worker, "review", _unavailable)
    before = pos.qty
    result = env.worker.cycle()
    assert result["status"] == "executed"
    assert result["reason"] == reason
    assert env.store.position().qty < before


def test_exit_continues_in_capped_chunks(env: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    env.worker.execute("buy", "seed")
    env.book.bid, env.book.ask = 200_000, 200_001
    env.clock.now += env.cfg.max_hold_days * DAY_MS
    monkeypatch.setattr(env.worker, "load_candles", _unavailable)
    result = env.worker.cycle()
    assert result["reason"] == "time_exit"
    assert env.store.position().exiting
    assert env.store.position().qty > 0
    assert env.worker.cycle()["reason"] == "continue_exit"


def test_cooldown_survives_closed_position(env: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    env.worker.execute("buy", "seed")
    env.worker.execute("sell", "seed_exit")
    assert env.store.position().qty == 0
    _buy_signal(env, monkeypatch)
    assert env.worker.cycle()["reason"] == "entry_cooldown"
    assert env.reviews == []
    env.clock.now += env.cfg.cooldown_days * DAY_MS
    assert env.worker.cycle()["status"] == "executed"
    assert len(env.reviews) == 1


@pytest.mark.parametrize("limiting_cap", ["max_order_quote", "max_position_quote", "max_daily_buy_quote"])
def test_buy_includes_fee_reserve_under_each_cap(env: Harness, limiting_cap: str) -> None:
    env.cfg.max_order_quote = 50
    env.cfg.max_position_quote = 50
    env.cfg.max_daily_buy_quote = 50
    setattr(env.cfg, limiting_cap, 10)
    env.worker.execute("buy", "budget_test")
    pos = env.store.position()
    assert 0 < pos.cost <= 10
    assert env.store.daily_buys(env.clock()) == pytest.approx(pos.cost)


def test_daily_spend_budget_accounts_for_previous_fees(env: Harness) -> None:
    env.store.intent("prior", "buy", 0.0003, 50_100, env.clock())
    env.store.finish(Fill("prior", "buy", "FILLED", 0.0003, 15, fee_quote=0.015))
    env.store.intent("prior-exit", "sell", 0.0003, 50_000, env.clock())
    env.store.finish(Fill("prior-exit", "sell", "FILLED", 0.0003, 15, fee_quote=0.015))
    pos = env.store.position()
    pos.last_buy_ms = env.clock() - env.cfg.cooldown_days * DAY_MS
    with env.store.db:
        env.store.put("position", asdict(pos))
    env.worker.execute("buy", "remaining_daily_budget")
    assert 15.015 < env.store.daily_buys(env.clock()) <= 25


@pytest.mark.parametrize("case", ["stale", "future", "spread", "nan", "crossed", "kill"])
def test_bad_quotes_and_halt_block_without_review_or_orders(
    env: Harness, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    _buy_signal(env, monkeypatch)
    if case == "stale":
        env.book.age_ms = env.cfg.max_quote_age_seconds * 1000 + 1
    elif case == "future":
        env.book.age_ms = -1
    elif case == "spread":
        env.book.ask = 51_000
    elif case == "nan":
        env.book.ask = float("nan")
    elif case == "crossed":
        env.book.ask = env.book.bid - 1
    else:
        env.cfg.kill_file.touch()
    result = env.worker.cycle()
    assert result["status"] in {"blocked", "halted"}
    assert env.reviews == []
    assert env.store.pending() == []
    assert env.store.position().qty == 0


def test_slow_review_refreshes_quote_before_execution(env: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _buy_signal(env, monkeypatch)

    def review(signal: Signal) -> Review:
        env.book.age_ms = env.cfg.max_quote_age_seconds * 1000 + 1
        return Review(approve=True, reason="Approved before quote became stale")

    env.worker.review = review
    assert env.worker.cycle()["status"] == "blocked"
    assert env.store.pending() == []


def test_halt_during_review_blocks_submission(env: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    _buy_signal(env, monkeypatch)

    def review(signal: Signal) -> Review:
        env.cfg.kill_file.touch()
        return Review(approve=True, reason="Approved while another operator halted")

    env.worker.review = review
    assert env.worker.cycle()["status"] == "blocked"
    assert env.store.pending() == []


def test_testnet_reset_inventory_mismatch_blocks_exit(env: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    env.worker.execute("buy", "seed")
    env.book.bid, env.book.ask = 40_000, 40_001
    monkeypatch.setattr(env.broker, "balances", lambda: {"BTC": 0.0, "USDT": 1000.0})
    assert env.worker.cycle()["reason"] == "inventory_mismatch_or_testnet_reset"
    assert env.store.pending() == []


def test_no_live_mode_and_ledgers_are_separate(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AgentConfig(mode="live")  # type: ignore[arg-type]
    paper = AgentConfig(mode="paper", state_dir=tmp_path)
    testnet = AgentConfig(mode="testnet", state_dir=tmp_path)
    assert paper.database != testnet.database


def test_reconciled_partial_buy_cannot_exceed_its_limit_price(env: Harness) -> None:
    env.store.intent("bad-partial", "buy", 0.001, 50_000, env.clock())
    # Total is below the 50 USDT order budget, but the partial fill paid 100000/BTC.
    with pytest.raises(ValueError, match="limit"):
        env.store.finish(Fill("bad-partial", "buy", "EXPIRED", 0.0001, 10))
    assert len(env.store.pending()) == 1
    assert env.store.position().qty == 0


def _leave_exit_residual(env: Harness) -> None:
    env.worker.execute("buy", "seed")
    env.book.bid, env.book.ask = 55_000, 55_001
    env.worker.execute("sell", "capped_exit")
    pos = env.store.position()
    assert pos.exiting and pos.qty > 0
    assert env.broker.is_dust(pos.qty, env.book.bid)


def test_dust_persists_across_restart_and_reentry_counts_its_exposure(
    env: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _leave_exit_residual(env)
    residual = env.store.position()
    balances = env.broker.balances()
    assert balances["BTC"] == pytest.approx(residual.qty)
    result = env.worker.cycle()
    assert result["reason"] == "exit_residual_recorded_as_dust"
    assert env.store.position().qty == 0
    assert env.worker.dust_qty() == pytest.approx(residual.qty)
    assert env.store.get("dust_cost") == pytest.approx(residual.cost)
    assert env.broker.balances() == balances
    assert env.store.position().last_buy_ms == residual.last_buy_ms
    assert env.worker.settle_dust(env.book.bid) is False
    assert env.worker.dust_qty() == pytest.approx(residual.qty)

    _buy_signal(env, monkeypatch)
    env.cfg.max_position_quote = 10
    reopened = Store(env.cfg.database)
    try:
        broker = PaperBroker(reopened, env.book)
        restarted = Service(env.cfg, reopened, broker, env.worker.load_candles,
                            env.worker.review, env.clock)
        assert restarted.dust_qty() == pytest.approx(residual.qty)
        assert reopened.get("dust_cost") == pytest.approx(residual.cost)
        assert restarted.cycle()["reason"] == "entry_cooldown"
        env.clock.now += env.cfg.cooldown_days * DAY_MS
        assert restarted.cycle()["status"] == "executed"
        pos = reopened.position()
        assert pos.qty > 0
        assert restarted.dust_qty() == pytest.approx(residual.qty)
        assert reopened.get("dust_cost") == pytest.approx(residual.cost)
        assert broker.balances()["BTC"] == pytest.approx(pos.qty + residual.qty)
        assert pos.cost + residual.qty * env.book.ask <= env.cfg.max_position_quote
        assert reopened.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 3
    finally:
        reopened.close()


def test_failed_dust_metadata_keeps_residual_owned_and_blocks(
    env: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _leave_exit_residual(env)
    residual = env.store.position()
    monkeypatch.setattr(env.broker, "is_dust", _unavailable)
    assert env.worker.cycle() == {"status": "blocked", "reason": "RuntimeError"}
    assert env.store.position() == residual
    assert env.worker.dust_qty() == 0
    assert env.store.get("dust_cost") is None
    assert env.store.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 2


def test_tradeable_residual_is_never_written_to_dust(env: Harness) -> None:
    env.worker.execute("buy", "seed")
    env.book.bid, env.book.ask = 200_000, 200_001
    env.worker.execute("sell", "first_exit_chunk")
    residual = env.store.position()
    assert residual.exiting
    assert residual.qty * env.book.bid > 5
    assert env.worker.settle_dust(env.book.bid) is False
    assert env.store.position() == residual
    assert env.worker.dust_qty() == 0


def test_unresolved_order_prevents_dust_reclassification(env: Harness) -> None:
    _leave_exit_residual(env)
    residual = env.store.position()
    env.store.intent("ambiguous-exit", "sell", residual.qty, env.book.bid, env.clock())
    assert env.worker.settle_dust(env.book.bid) is False
    assert env.store.position() == residual
    assert env.worker.dust_qty() == 0


def test_account_reset_including_dust_blocks_new_entries(
    env: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _leave_exit_residual(env)
    assert env.worker.settle_dust(env.book.bid)
    env.clock.now += env.cfg.cooldown_days * DAY_MS
    _buy_signal(env, monkeypatch)
    monkeypatch.setattr(env.broker, "balances", lambda: {"BTC": 0.0, "USDT": 1000.0})
    assert env.worker.cycle()["reason"] == "inventory_mismatch_or_testnet_reset"
    assert env.reviews == []
    assert env.worker.dust_qty() > 0


def test_reviewer_failures_leave_sanitized_durable_audit(
    env: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _buy_signal(env, monkeypatch)
    monkeypatch.setattr(env.worker, "review", _unavailable)
    assert env.worker.cycle()["status"] == "blocked"
    audit = env.store.get("last_review")
    assert isinstance(audit, dict)
    assert audit["approve"] is False
    assert audit["error"] == "RuntimeError"
    assert audit["prompt_version"] == "btc-entry-v1"
    assert len(audit["input_sha256"]) == 64
    assert "secret" not in str(audit)
    assert env.store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1


@pytest.mark.parametrize("move", [-0.06, 0.06])
def test_large_price_move_during_review_blocks_entry_in_both_directions(
    env: Harness, monkeypatch: pytest.MonkeyPatch, move: float,
) -> None:
    _buy_signal(env, monkeypatch)
    reviewed_prices: list[float] = []
    initial_price = env.book.ask

    def review(signal: Signal) -> Review:
        reviewed_prices.append(signal.price)
        env.book.ask = initial_price * (1 + move)
        env.book.bid = env.book.ask - 1
        return Review(approve=True, reason="Approved the original candidate snapshot")

    env.worker.review = review
    assert env.worker.cycle() == {"status": "blocked", "reason": "ValueError"}
    assert reviewed_prices == [initial_price]
    assert env.store.pending() == []
    assert env.store.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
    assert env.broker.balances() == {"BTC": 0.0, "USDT": 1000.0}


def test_small_price_move_during_review_executes_at_refreshed_quote(
    env: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _buy_signal(env, monkeypatch)
    initial_price = env.book.ask

    def review(signal: Signal) -> Review:
        assert signal.price == initial_price
        env.book.ask = initial_price * 1.02
        env.book.bid = env.book.ask - 1
        return Review(approve=True, reason="Candidate remains valid within allowed deviation")

    env.worker.review = review
    result = env.worker.cycle()
    assert result["status"] == "executed"
    fill = result["fill"]
    assert isinstance(fill, dict)
    assert fill["quote_qty"] / fill["filled_qty"] == pytest.approx(env.book.ask)
    assert env.store.position().cost <= env.cfg.max_order_quote


def test_entry_deviation_guard_never_prevents_protective_exit(
    env: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env.worker.execute("buy", "seed")
    previous_qty = env.store.position().qty
    env.cfg.max_entry_deviation = 0.001
    env.book.bid, env.book.ask = 40_000, 40_001
    monkeypatch.setattr(env.worker, "load_candles", _unavailable)
    monkeypatch.setattr(env.worker, "review", _unavailable)
    result = env.worker.cycle()
    assert result["status"] == "executed"
    assert result["reason"] == "stop_loss"
    assert env.store.position().qty < previous_qty
