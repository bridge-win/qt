"""Long-running paper trading loop.

Pulls live data every `--interval` seconds, recomputes the composite
score on the trailing window, asks the risk engine for a decision, and
routes orders to `PaperBroker`. State is *not* persisted across restarts
in this stub — see `docs/architecture.md` for the production checklist.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

from rich.console import Console

from qt.backtest.fills import FillModel
from qt.core.config import load_settings
from qt.core.logging import configure_logging, get_logger
from qt.core.types import OrderSide, OrderType, Position
from qt.data.derivatives import fetch_funding_rate_history, fetch_open_interest_history
from qt.data.market import fetch_ohlcv
from qt.data.onchain import fetch_coinmetrics
from qt.data.sentiment import fetch_fear_greed
from qt.execution.base import Order
from qt.execution.paper import PaperBroker
from qt.indicators.price import atr
from qt.indicators.volatility import realized_vol
from qt.risk.engine import RiskEngine
from qt.signal.engine import SignalEngine

console = Console()
log = get_logger(__name__)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--interval", type=int, default=3600, help="seconds between cycles")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    args = p.parse_args()

    configure_logging("INFO")
    settings = load_settings(args.config)
    sig_engine = SignalEngine(thresholds=settings.thresholds)
    risk_engine = RiskEngine(cfg=settings.risk, bar_seconds=3600)
    broker = PaperBroker(
        initial_cash=args.cash,
        fills=FillModel(fee_bps=settings.execution.fee_bps,
                        slippage_bps=settings.execution.slippage_bps),
    )

    cycle = 0
    position = Position(symbol="BTC/USDT")
    while args.cycles == 0 or cycle < args.cycles:
        cycle += 1
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(days=180)
        ohlcv = fetch_ohlcv("binance", "BTC/USDT", "1h", since=since)
        if ohlcv.empty:
            log.warning("no_ohlcv_pulled")
            time.sleep(args.interval)
            continue
        funding = fetch_funding_rate_history(symbol="BTCUSDT", since=since)
        oi = fetch_open_interest_history(symbol="BTCUSDT")
        fg = fetch_fear_greed(limit=0)
        mvrv = fetch_coinmetrics("mvrv", since=since)

        score = sig_engine.evaluate(
            ohlcv=ohlcv,
            funding=funding["funding_rate"] if not funding.empty else None,
            oi=oi["oi_usd"] if not oi.empty else None,
            fear_greed=fg["fear_greed"] if not fg.empty else None,
            mvrv_z=mvrv["mvrv"] if not mvrv.empty else None,  # mvrv proxy if Z unavailable
        )
        sigs = sig_engine.to_signals(score)
        latest_ts = ohlcv.index[-1]
        mark = float(ohlcv["close"].iloc[-1])
        equity = broker.equity({"BTC/USDT": mark})

        # Exit check
        if not position.is_flat:
            dec = risk_engine.evaluate_exit(position, mark, now)
            if dec.action == "close":
                tr = broker.submit(
                    Order(symbol="BTC/USDT", side=OrderSide.SELL, type=OrderType.MARKET,
                          qty=position.qty, note=dec.reason),
                    mark_price=mark,
                )
                pnl = (tr.price - position.avg_price) * position.qty - tr.fee
                risk_engine.record_realized(pnl, now)
                console.print(f"[yellow]EXIT[/] {tr} pnl={pnl:.2f} reason={dec.reason}")
                position = Position(symbol="BTC/USDT")

        # Entry check: only on bars whose timestamp matches the latest signal.
        sig_for_now = next((s for s in sigs if s.ts == latest_ts.to_pydatetime()), None)
        if sig_for_now is not None and position.is_flat:
            atr_val = float(atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14).iloc[-1])
            rv_val = float(realized_vol(ohlcv["close"], window=24 * 7).iloc[-1])
            dec = risk_engine.evaluate_entry(
                signal=sig_for_now, equity=equity, mark_price=mark,
                atr_value=atr_val, realized_vol_annual=rv_val, position=position,
            )
            if dec.action == "open" and dec.size_quote > 0:
                qty = dec.size_quote / mark
                tr = broker.submit(
                    Order(symbol="BTC/USDT", side=OrderSide.BUY, type=OrderType.MARKET,
                          qty=qty, note=dec.reason),
                    mark_price=mark,
                )
                position = Position(
                    symbol="BTC/USDT", qty=tr.qty, avg_price=tr.price, opened_ts=now,
                    stop_price=dec.stop_price, take_profit_price=dec.take_profit_price,
                    time_stop_ts=dec.time_stop_ts,
                )
                console.print(f"[green]ENTRY[/] {tr} score={sig_for_now.score:.2f}")

        console.print(
            f"[dim]cycle={cycle} ts={now.isoformat()} score_latest="
            f"{float(score.score.iloc[-1]):.2f} equity={equity:.2f}[/]"
        )
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
