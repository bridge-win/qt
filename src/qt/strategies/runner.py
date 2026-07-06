"""Multi-strategy runner.

Runs every enabled strategy in its own thread, on its own cadence,
inside a single Python process. Each strategy writes a durable
heartbeat to ``<runtime_dir>/strategies/<name>.json`` that the
dashboard reads to render the per-strategy sub-routes.

Opportunities are routed through the RiskEngine → PaperBroker → PortfolioLedger
for deterministic paper execution and account tracking.

Designed for the "one-line startup" use case: a single
``python scripts/run_all.py`` brings up every strategy + the
dashboard with no extra orchestration.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Literal

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.core.types import OrderSide, OrderType, Position, Signal, SignalKind
from qt.execution.base import Broker, Order
from qt.execution.paper import PaperBroker
from qt.monitoring.alerts import alert
from qt.monitoring.state import MonitorStateStore, new_snapshot, with_update
from qt.portfolio import PortfolioLedger
from qt.risk.engine import RiskEngine
from qt.strategies.base import Opportunity, Strategy

log = get_logger(__name__)


def _make_broker(settings: Settings, initial_cash: float) -> Broker:
    """Select a broker from config.

    Defaults to PaperBroker. Only builds a LiveBroker when execution.mode is
    'live' AND execution.live_enabled is True — and even then the LiveBroker
    itself stays in dry_run unless explicitly disabled. Any failure to build a
    live broker (bad key, ccxt missing, unsafe key) falls back to paper so the
    runner never crashes and never trades unexpectedly.
    """
    exec_cfg = settings.execution
    if getattr(exec_cfg, "mode", "paper") == "live" and getattr(exec_cfg, "live_enabled", False):
        try:
            from qt.execution.live import LiveBroker

            broker = LiveBroker.from_settings(settings)
            log.info("live_broker_selected", venue=exec_cfg.venue, dry_run=exec_cfg.dry_run)
            return broker
        except Exception as exc:
            log.warning("live_broker_unavailable_falling_back_to_paper", error=str(exc))
    return PaperBroker(initial_cash=initial_cash)


def _opp_to_signal(opp: Opportunity, score: float = 0.6) -> Signal:
    """Convert an Opportunity to a Signal for risk engine evaluation."""
    kind = SignalKind.ENTRY_LONG if opp.action in ("buy", "open") else SignalKind.EXIT
    return Signal(
        ts=opp.ts,
        kind=kind,
        score=score,
        reasons=(opp.reason,),
        target_quote_alloc=0.05,  # 5% default allocation per trade
    )


def strategy_state_dir(runtime_dir: str | Path) -> Path:
    """Where per-strategy heartbeat JSON files live."""
    p = Path(runtime_dir) / "strategies"
    p.mkdir(parents=True, exist_ok=True)
    return p


def strategy_state_path(runtime_dir: str | Path, strategy_name: str) -> Path:
    return strategy_state_dir(runtime_dir) / f"{strategy_name}.json"


def run_strategy_forever(
    strategy: Strategy,
    settings: Settings,
    *,
    runtime_dir: str | Path,
    stop_event: threading.Event,
    max_backoff_seconds: int = 300,
    initial_cash: float = 100_000.0,
) -> None:
    """Drive a single strategy through repeated tick → sleep cycles.

    For every Opportunity, routes through RiskEngine → PaperBroker → PortfolioLedger.
    Persists a heartbeat on every cycle and fires ``alert(...)`` when
    an execution occurs or the strategy returns a non-None Opportunity at or above
    ``config.min_alert_severity`` urgency.
    """

    state_path = strategy_state_path(runtime_dir, strategy.name)
    store = MonitorStateStore(state_path)
    snapshot = new_snapshot(
        name=strategy.name, mode=settings.execution.mode,
        details={"description": strategy.description},
    )
    store.write(snapshot)

    # Init portfolio + broker + risk engine
    runtime_path = Path(runtime_dir)
    portfolio = PortfolioLedger(strategy.name, runtime_path)
    portfolio.set_initial_cash(initial_cash)

    paper_broker = _make_broker(settings, initial_cash)
    risk_engine = RiskEngine(cfg=settings.risk)

    cycle = 0
    consecutive_failures = 0
    interval = max(1, int(strategy.config.interval_seconds))
    last_mark_price = 50_000.0  # fallback for BTC
    while not stop_event.is_set():
        cycle += 1
        try:
            data = strategy.fetch_data(settings)
            result = strategy.evaluate(data)
        except Exception as exc:
            consecutive_failures += 1
            backoff = min(max_backoff_seconds, interval * consecutive_failures)
            fail_status: Literal["failed", "degraded"] = (
                "failed" if consecutive_failures >= 5 else "degraded"
            )
            snapshot = with_update(
                snapshot, status=fail_status, cycle=cycle,
                consecutive_failures=consecutive_failures, last_error=str(exc),
            )
            store.write(snapshot)
            log.warning("strategy_cycle_failed", strategy=strategy.name, error=str(exc))
            alert(
                f"strategy {strategy.name} cycle failed",
                severity="warning",
                strategy=strategy.name, cycle=cycle, error=str(exc),
                backoff_seconds=backoff,
            )
            stop_event.wait(timeout=backoff)
            continue

        consecutive_failures = 0

        # Extract mark price from data (if available; fallback to last_mark_price)
        if "mark_price" in data:
            last_mark_price = data["mark_price"]
        if "close" in data:
            last_mark_price = data["close"]

        details: dict[str, object] = {
            "description": strategy.description,
            "params": strategy.config.params,
            "last_evaluation": result.as_dict(),
        }

        # Route opportunity through risk engine → broker → ledger
        trades_executed = []
        if result.opportunity is not None:
            opp = result.opportunity
            pos = Position(
                symbol="BTC/USDT",
                qty=paper_broker.position_qty("BTC/USDT"),
                avg_price=portfolio.avg_price.get("BTC/USDT", 0.0),
            )
            # Broker-agnostic equity from the durable ledger.
            cur_equity = portfolio.snapshot({"BTC/USDT": last_mark_price}).equity

            # Entry decision
            if opp.action == "buy":
                decision = risk_engine.evaluate_entry(
                    signal=_opp_to_signal(opp, score=opp.confidence),
                    equity=cur_equity,
                    mark_price=last_mark_price,
                    atr_value=last_mark_price * 0.02,  # estimate: 2% ATR
                    realized_vol_annual=0.8,  # estimate
                    position=pos,
                )
                if decision.action == "open" and decision.size_quote > 10:  # min 10 USD
                    qty = decision.size_quote / last_mark_price
                    order = Order(
                        symbol="BTC/USDT",
                        side=OrderSide.BUY,
                        type=OrderType.MARKET,
                        qty=qty,
                        note=f"[{strategy.name}] {opp.reason}",
                    )
                    trade = paper_broker.submit(order, last_mark_price)
                    portfolio.record_trade(
                        trade,
                        mark_prices={"BTC/USDT": last_mark_price},
                        total_initial_cash=initial_cash,
                    )
                    trades_executed.append(trade)
                    log.info(
                        "trade_executed",
                        strategy=strategy.name,
                        symbol=order.symbol,
                        side="buy",
                        qty=qty,
                        price=last_mark_price,
                    )

            # Exit decision (if position is open)
            elif opp.action == "sell" and not pos.is_flat:
                pos_qty = paper_broker.position_qty("BTC/USDT")
                if pos_qty > 1e-8:
                    order = Order(
                        symbol="BTC/USDT",
                        side=OrderSide.SELL,
                        type=OrderType.MARKET,
                        qty=pos_qty,
                        note=f"[{strategy.name}] {opp.reason}",
                    )
                    trade = paper_broker.submit(order, last_mark_price)
                    portfolio.record_trade(
                        trade,
                        mark_prices={"BTC/USDT": last_mark_price},
                        total_initial_cash=initial_cash,
                    )
                    trades_executed.append(trade)
                    log.info(
                        "trade_executed",
                        strategy=strategy.name,
                        symbol=order.symbol,
                        side="sell",
                        qty=pos_qty,
                        price=last_mark_price,
                    )

            # Alert on trade
            alert(
                f"{strategy.name} {opp.action} opportunity → {len(trades_executed)} trade(s) executed: {opp.reason}",
                severity=strategy.config.min_alert_severity,
                strategy=strategy.name,
                action=opp.action,
                trades=len(trades_executed),
                **{k: v for k, v in opp.details.items()},
            )
            details["last_opportunity"] = opp.as_dict()
            details["last_trades"] = [
                {"ts": t.ts.isoformat(), "side": t.side.value, "qty": t.qty, "price": t.price}
                for t in trades_executed
            ]
        elif "last_opportunity" in snapshot.details:
            details["last_opportunity"] = snapshot.details["last_opportunity"]

        # Update portfolio snapshot in details
        snap = portfolio.snapshot({"BTC/USDT": last_mark_price})
        details["portfolio"] = {
            "cash": snap.cash,
            "equity": snap.equity,
            "positions": snap.positions,
            "realized_pnl": snap.realized_pnl,
            "unrealized_pnl": snap.unrealized_pnl,
        }

        snapshot = with_update(
            snapshot, status="healthy", cycle=cycle,
            consecutive_failures=0, last_error=None, details=details,
        )
        store.write(snapshot)
        stop_event.wait(timeout=interval)

    snapshot = with_update(snapshot, status="stopped", cycle=cycle)
    store.write(snapshot)


def start_all_strategies(
    strategies: list[Strategy],
    settings: Settings,
    *,
    runtime_dir: str | Path,
    stop_event: threading.Event,
    initial_cash: float = 100_000.0,
) -> list[threading.Thread]:
    """Spawn one daemon thread per strategy. Returns the threads so the
    caller can ``.join`` on shutdown."""

    threads: list[threading.Thread] = []
    for s in strategies:
        t = threading.Thread(
            target=run_strategy_forever,
            kwargs={
                "strategy": s, "settings": settings,
                "runtime_dir": runtime_dir, "stop_event": stop_event,
                "initial_cash": initial_cash,
            },
            name=f"strategy-{s.name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("strategy_started", strategy=s.name, interval=s.config.interval_seconds)
    return threads


def wait_for_shutdown(stop_event: threading.Event, poll: float = 1.0) -> None:
    """Block the main thread until ``stop_event`` is set."""

    try:
        while not stop_event.is_set():
            time.sleep(poll)
    except KeyboardInterrupt:
        stop_event.set()


__all__ = [
    "run_strategy_forever",
    "start_all_strategies",
    "strategy_state_dir",
    "strategy_state_path",
    "wait_for_shutdown",
]
