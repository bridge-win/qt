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

import math
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, SupportsFloat, SupportsIndex, cast

import pandas as pd

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.core.types import OrderSide, OrderType, Position, Signal, SignalKind
from qt.execution.base import Broker, Order
from qt.execution.paper import PaperBroker
from qt.monitoring.alerts import alert
from qt.monitoring.state import MonitorStateStore, new_snapshot, with_update
from qt.portfolio import PortfolioLedger
from qt.risk.engine import RiskEngine
from qt.strategies.base import EvaluationResult, Opportunity, Strategy

log = get_logger(__name__)

ENTRY_ACTIONS = frozenset({"buy", "open"})
EXIT_ACTIONS = frozenset({"sell", "close"})
PRICE_KEYS = ("mark_price", "close", "price", "spot_price", "last_price", "weekly_close")
QUOTE_KEYS = ("amount_quote", "rung_quote", "target_quote", "quote")


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


def _opp_to_signal(
    opp: Opportunity,
    score: float = 0.6,
    target_quote_alloc: float = 0.05,
) -> Signal:
    """Convert an Opportunity to a Signal for risk engine evaluation."""
    kind = SignalKind.ENTRY_LONG if opp.action in ("buy", "open") else SignalKind.EXIT
    return Signal(
        ts=opp.ts,
        kind=kind,
        score=score,
        reasons=(opp.reason,),
        target_quote_alloc=target_quote_alloc,
    )


def _positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(cast(SupportsFloat | SupportsIndex | str, value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def _latest_ohlcv_close(value: object) -> float | None:
    if not isinstance(value, pd.DataFrame) or value.empty or "close" not in value:
        return None
    return _positive_float(value["close"].iloc[-1])


def _extract_mark_price(
    data: Mapping[str, object],
    result: EvaluationResult,
    fallback: float,
) -> float:
    for key in PRICE_KEYS:
        price = _positive_float(data.get(key))
        if price is not None:
            return price

    ohlcv_price = _latest_ohlcv_close(data.get("ohlcv"))
    if ohlcv_price is not None:
        return ohlcv_price

    for key in PRICE_KEYS:
        price = _positive_float(result.metrics.get(key))
        if price is not None:
            return price

    if result.opportunity is not None:
        for key in PRICE_KEYS:
            price = _positive_float(result.opportunity.details.get(key))
            if price is not None:
                return price

    return fallback


def _opportunity_symbol(opp: Opportunity, settings: Settings) -> str:
    symbol = opp.details.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        return symbol.strip()
    return settings.execution.symbol


def _target_quote_alloc(opp: Opportunity, equity: float, default: float = 0.05) -> float:
    if equity <= 0:
        return default
    for key in QUOTE_KEYS:
        amount_quote = _positive_float(opp.details.get(key))
        if amount_quote is not None:
            return min(1.0, amount_quote / equity)
    return default


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

        last_mark_price = _extract_mark_price(data, result, last_mark_price)

        details: dict[str, object] = {
            "description": strategy.description,
            "params": strategy.config.params,
            "last_evaluation": result.as_dict(),
        }

        # Route opportunity through risk engine → broker → ledger
        trades_executed = []
        cycle_status: Literal["healthy", "degraded", "failed"] = "healthy"
        cycle_error: str | None = None
        sleep_seconds = interval
        active_symbol = settings.execution.symbol
        if result.opportunity is not None:
            opp = result.opportunity
            symbol = _opportunity_symbol(opp, settings)
            active_symbol = symbol
            mark_prices = {symbol: last_mark_price}
            try:
                pos = Position(
                    symbol=symbol,
                    qty=paper_broker.position_qty(symbol),
                    avg_price=portfolio.avg_price.get(symbol, 0.0),
                )
                # Broker-agnostic equity from the durable ledger.
                cur_equity = portfolio.snapshot(mark_prices).equity

                # Entry decision
                if opp.action in ENTRY_ACTIONS:
                    decision = risk_engine.evaluate_entry(
                        signal=_opp_to_signal(
                            opp,
                            score=opp.confidence,
                            target_quote_alloc=_target_quote_alloc(opp, cur_equity),
                        ),
                        equity=cur_equity,
                        mark_price=last_mark_price,
                        atr_value=last_mark_price * 0.02,  # estimate: 2% ATR
                        realized_vol_annual=0.8,  # estimate
                        position=pos,
                    )
                    if decision.action == "open" and decision.size_quote > 10:  # min 10 USD
                        qty = decision.size_quote / last_mark_price
                        order = Order(
                            symbol=symbol,
                            side=OrderSide.BUY,
                            type=OrderType.MARKET,
                            qty=qty,
                            note=f"[{strategy.name}] {opp.reason}",
                        )
                        trade = paper_broker.submit(order, last_mark_price)
                        portfolio.record_trade(
                            trade,
                            mark_prices=mark_prices,
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
                elif opp.action in EXIT_ACTIONS and not pos.is_flat:
                    pos_qty = paper_broker.position_qty(symbol)
                    if pos_qty > 1e-8:
                        order = Order(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            type=OrderType.MARKET,
                            qty=pos_qty,
                            note=f"[{strategy.name}] {opp.reason}",
                        )
                        trade = paper_broker.submit(order, last_mark_price)
                        portfolio.record_trade(
                            trade,
                            mark_prices=mark_prices,
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
            except Exception as exc:
                consecutive_failures += 1
                cycle_status = "failed" if consecutive_failures >= 5 else "degraded"
                cycle_error = str(exc)
                sleep_seconds = min(max_backoff_seconds, interval * consecutive_failures)
                details["execution_error"] = cycle_error
                log.warning(
                    "strategy_execution_failed",
                    strategy=strategy.name,
                    action=opp.action,
                    error=cycle_error,
                )
                alert(
                    f"strategy {strategy.name} execution failed",
                    severity="warning",
                    strategy=strategy.name,
                    action=opp.action,
                    cycle=cycle,
                    error=cycle_error,
                    backoff_seconds=sleep_seconds,
                )
            else:
                consecutive_failures = 0

            if cycle_error is None:
                # Alert on trade
                alert(
                    f"{strategy.name} {opp.action} opportunity → {len(trades_executed)} trade(s) executed: {opp.reason}",
                    severity=strategy.config.min_alert_severity,
                    strategy=strategy.name,
                    action=opp.action,
                    trades=len(trades_executed),
                    **{k: v for k, v in opp.details.items()},
                )
                details["last_trades"] = [
                    {"ts": t.ts.isoformat(), "side": t.side.value, "qty": t.qty, "price": t.price}
                    for t in trades_executed
                ]
            else:
                details["last_trades"] = []

            details["last_opportunity"] = opp.as_dict()
        else:
            consecutive_failures = 0
            if "last_opportunity" in snapshot.details:
                details["last_opportunity"] = snapshot.details["last_opportunity"]

        # Update portfolio snapshot in details
        portfolio_mark_prices = {symbol: last_mark_price for symbol in portfolio.positions}
        portfolio_mark_prices.setdefault(active_symbol, last_mark_price)
        snap = portfolio.snapshot(portfolio_mark_prices)
        details["portfolio"] = {
            "cash": snap.cash,
            "equity": snap.equity,
            "positions": snap.positions,
            "realized_pnl": snap.realized_pnl,
            "unrealized_pnl": snap.unrealized_pnl,
        }

        snapshot = with_update(
            snapshot, status=cycle_status, cycle=cycle,
            consecutive_failures=consecutive_failures, last_error=cycle_error, details=details,
        )
        store.write(snapshot)
        stop_event.wait(timeout=sleep_seconds)

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
