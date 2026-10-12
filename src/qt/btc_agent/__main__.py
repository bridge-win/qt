"""Run with python -m qt.btc_agent; no live-money command exists."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import threading
import time
from dataclasses import asdict

from qt.btc_agent import market
from qt.btc_agent.broker import BinanceTestnetBroker
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.paper import PaperBroker
from qt.btc_agent.reviewer import review_signal, serve
from qt.btc_agent.service import Service
from qt.btc_agent.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "run",
            "once",
            "status",
            "health",
            "halt",
            "resume",
            "reviewer",
            "probe-testnet",
            "testnet-round-trip",
            "backtest",
        ],
    )
    args = parser.parse_args()
    if args.command == "reviewer":
        serve()
        return 0
    cfg = AgentConfig()
    if args.command == "backtest":
        from qt.btc_agent.backtest import run_backtest

        closed = [
            c for c in market.candles() if c.open_time + 86_400_000 <= int(time.time() * 1000)
        ]
        print(json.dumps(run_backtest(closed), indent=2))
        return 0
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "halt":
        cfg.kill_file.touch()
        print("All new orders halted; reconciliation remains available.")
        return 0
    if args.command in {"status", "health"}:
        if not cfg.database.exists():
            print(json.dumps({"status": "not_started", "mode": cfg.mode}))
            return 1
        store = Store(cfg.database)
        heartbeat = store.get("heartbeat")
        if args.command == "status":
            print(
                json.dumps(
                    {
                        "mode": cfg.mode,
                        "heartbeat": heartbeat,
                        "position": asdict(store.position()),
                        "dust_btc": store.get("dust_btc"),
                        "dust_cost": store.get("dust_cost"),
                        "pending_orders": len(store.pending()),
                        "last_review": store.get("last_review"),
                    },
                    indent=2,
                )
            )
        store.close()
        return (
            0
            if isinstance(heartbeat, dict)
            and int(time.time() * 1000) - heartbeat["ts"]
            < (cfg.poll_seconds * 2 + cfg.reviewer_timeout_seconds) * 1000
            and heartbeat.get("status") not in {"blocked", "halted"}
            else 1
        )
    key, secret = (
        os.getenv("BINANCE_TESTNET_API_KEY", ""),
        os.getenv("BINANCE_TESTNET_API_SECRET", ""),
    )
    if args.command == "probe-testnet":
        broker = BinanceTestnetBroker(key, secret)
        print(
            json.dumps(
                {
                    "testnet_quote": asdict(broker.quote()),
                    "authenticated": bool(key and secret),
                    "balances": broker.balances() if key and secret else None,
                }
            )
        )
        return 0
    if args.command == "testnet-round-trip" and cfg.mode != "testnet":
        raise ValueError("testnet-round-trip requires BTC_AGENT_MODE=testnet")
    if cfg.mode == "testnet" and not (key and secret):
        raise ValueError(
            "Dedicated BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET are required"
        )
    # OS lock releases on process death; only one worker may own this database.
    with (cfg.state_dir / f"{cfg.mode}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another worker owns this mode's state directory") from None
        store = Store(cfg.database)
        try:
            if cfg.mode == "testnet":
                fingerprint = hashlib.sha256(key.encode()).hexdigest()
                existing = store.get("account_fingerprint")
                if existing and existing != fingerprint:
                    raise ValueError("testnet account changed; use a separate state directory")
                with store.db:
                    store.put("account_fingerprint", fingerprint)
            if args.command == "resume":
                if store.pending():
                    raise ValueError("unresolved orders must be reconciled before resuming")
                cfg.kill_file.unlink(missing_ok=True)
                print("Halt removed.")
                return 0
            execution = (
                BinanceTestnetBroker(key, secret)
                if cfg.mode == "testnet"
                else PaperBroker(store, market.quote)
            )
            worker = Service(
                cfg,
                store,
                execution,
                market.candles,
                lambda s: review_signal(s, cfg.reviewer_url, cfg.reviewer_timeout_seconds),
            )
            if args.command == "testnet-round-trip":
                # Explicit plumbing test, deliberately separate from the strategy.
                # Requires an empty ledger and an actual Codex review before its buy.
                if store.position().qty or store.pending():
                    raise ValueError("round-trip requires a clean, dedicated testnet ledger")
                from qt.btc_agent.signals import Signal

                book = execution.quote()
                candidate = Signal(
                    "buy",
                    "Explicit testnet connectivity smoke, not a strategy signal",
                    0,
                    book.ask,
                    0,
                    {"smoke_test": True},
                )
                review = worker.review(candidate)
                if not review.approve:
                    print(json.dumps({"status": "vetoed", "review": review.model_dump()}))
                    return 1
                buy_result = worker.execute("buy", "testnet_smoke")
                print(json.dumps(buy_result))
                if store.pending():
                    print(
                        json.dumps(
                            {"status": "blocked", "reason": "reconcile_smoke_buy_before_exit"}
                        )
                    )
                    return 1
                if store.position().qty <= 0:
                    print(json.dumps({"status": "failed", "reason": "no_buy_fill"}))
                    return 1
                sold = False
                for _ in range(5):
                    exit_result = worker.execute("sell", "testnet_smoke_exit")
                    print(json.dumps(exit_result))
                    exit_fill = exit_result.get("fill")
                    sold |= isinstance(exit_fill, dict) and float(exit_fill["filled_qty"]) > 0
                    if store.pending():
                        break
                    worker.settle_dust(execution.quote().bid)
                    if not store.position().qty:
                        break
                ok = sold and not store.pending() and store.position().qty == 0
                print(
                    json.dumps(
                        {
                            "status": "passed" if ok else "incomplete",
                            "remaining_btc": store.position().qty,
                            "dust_btc": worker.dust_qty(),
                            "pending_orders": len(store.pending()),
                        }
                    )
                )
                return 0 if ok else 1
            stopped = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stopped.set())
            while not stopped.is_set():
                result = worker.cycle()
                print(json.dumps(result, allow_nan=False), flush=True)
                if args.command == "once":
                    return 1 if result["status"] == "blocked" else 0
                stopped.wait(cfg.poll_seconds)
            return 0
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
