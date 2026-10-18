"""Operate the alert service. This CLI intentionally contains no trading commands."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .market import PublicMarket
from .projection import projection, update_policy
from .reviewer import call_reviewer, serve
from .service import AlertService
from .store import Store
from .telegram import Telegram


def configured_telegram() -> Telegram:
    return Telegram(os.getenv("QT_TELEGRAM_BOT_TOKEN", ""), os.getenv("QT_TELEGRAM_CHAT_ID", ""),
                    os.getenv("QT_BTC_ALERTS_EXPECTED_BOT", "radarxdotbot"))


def health(root: Path) -> bool:
    path = root / "alerts.sqlite3"
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as db:
            row = db.execute("SELECT payload FROM runtime WHERE key='heartbeat'").fetchone()
            cycle = db.execute("SELECT payload FROM runtime WHERE key='cycle_started'").fetchone()
        now = time.time()
        return bool(row and 0 <= now - json.loads(row[0]) <= 20 and cycle and 0 <= now - json.loads(cycle[0]) <= 240)
    except (sqlite3.Error, ValueError):
        return False


def run(store: Store, telegram: Telegram, monitor_root: Path | None) -> None:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with (store.path.parent / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover(time.time())
        market = PublicMarket(monitor_root)
        service = AlertService(store, market, telegram)

        def work() -> None:
            while not stop.is_set():
                store.put("cycle_started", time.time())
                try:
                    service.tick()
                    from .evaluation import label_forward_events

                    label_forward_events(store, market, time.time())
                except Exception as error:
                    store.put("worker_status", {"at": time.time(), "status": "cycle_failed", "reason": type(error).__name__, "live_execution_enabled": False})
                stop.wait(10)

        thread = threading.Thread(target=work, name="btc-alert-cycle", daemon=True)
        thread.start()
        try:
            while not stop.is_set():
                store.put("heartbeat", time.time())
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                destination = store.path.parent / "backups" / f"alerts-{today}.sqlite3"
                if not destination.exists():
                    store.backup(destination)
                    backups = sorted(destination.parent.glob("alerts-????-??-??.sqlite3"))
                    for old in backups[:-7]:
                        old.unlink()
                stop.wait(5)
        finally:
            thread.join(timeout=125)
            store.put("heartbeat", 0)
            store.put("worker_status", {"at": time.time(), "status": "stopped", "live_execution_enabled": False})
            market.close()
            telegram.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "reviewer", "status", "health", "mode", "doctor", "test-notification", "verify-recipient", "backup", "research"])
    parser.add_argument("--state-dir", type=Path, default=Path(os.getenv("QT_BTC_ALERTS_STATE_DIR", "data/runtime/btc-alerts")))
    parser.add_argument("--monitor-dir", type=Path, default=os.getenv("QT_BTC_MONITOR_STATE_DIR"))
    parser.add_argument("--mode", choices=["off", "shadow", "notify"])
    parser.add_argument("--nonce")
    parser.add_argument("--request-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--days", type=int, default=730)
    args = parser.parse_args()
    if args.command == "reviewer":
        serve()
        return 0
    if args.command == "health":
        return 0 if health(args.state_dir) else 1
    store = Store(args.state_dir)
    telegram = configured_telegram()
    try:
        if args.command == "run":
            run(store, telegram, args.monitor_dir)
        elif args.command == "status":
            print(json.dumps(projection(store, time.time()), ensure_ascii=False))
        elif args.command == "mode":
            if args.mode is None:
                parser.error("--mode is required")
            revision, policy = store.policy()
            update_policy(store, revision, policy.model_copy(update={"mode": args.mode}), time.time())
            print(json.dumps({"mode": args.mode, "live_execution_enabled": False}))
        elif args.command == "verify-recipient":
            if args.nonce is None:
                parser.error("--nonce is required")
            print(json.dumps(telegram.verify(store, args.nonce, time.time())))
        elif args.command == "test-notification":
            state = projection(store, time.time())
            if not telegram.verified(store) or not state["worker"]["alive"]:
                raise ValueError("verified_recipient_and_running_worker_required")
            event_id = store.request_test(args.request_id or uuid.uuid4().hex, time.time(), state["strategy_version"])
            print(json.dumps({"event_id": event_id, "status": "queued", "is_test": True}))
        elif args.command == "doctor":
            now = time.time()
            batch_id = f"probe:{int(now // 3600)}"
            report: dict[str, Any] = {"worker_alive": health(args.state_dir),
                                     "telegram_configured": telegram.configured,
                                     "recipient_verified": telegram.verified(store), "model": False}
            if store.review_budget(batch_id, now, {"kind": "authentication_probe"}):
                try:
                    result = call_reviewer({}, probe=True)
                    state = {"authenticated": True, "at": time.time(), "model": result.get("model"),
                             "model_pinned": result.get("model_pinned", False)}
                    store.put("reviewer_status", state)
                    store.finish_review(batch_id, {"kind": "authentication_probe", "result": result})
                except Exception as error:
                    store.put("reviewer_status", {"authenticated": False, "at": time.time(), "reason": type(error).__name__})
            state = store.get("reviewer_status", {})
            report["model"] = bool(state.get("authenticated") and 0 <= time.time() - state.get("at", 0) <= 86400)
            report["model_pinned"] = state.get("model_pinned", False)
            report["strategy_validation_required"] = True
            print(json.dumps(report))
            return 0 if all(report[key] for key in ("worker_alive", "recipient_verified", "model", "model_pinned")) else 1
        elif args.command == "backup":
            if args.output is None:
                parser.error("--output is required")
            store.backup(args.output)
            print(json.dumps({"backup": "completed"}))
        elif args.command == "research":
            from .evaluation import run_research

            print(json.dumps(run_research(store, days=args.days), ensure_ascii=False))
        return 0
    except Exception as error:
        safe = str(error) if isinstance(error, ValueError) and str(error) in {
            "notification_readiness_pending", "strategy_validation_or_forward_shadow_pending",
            "verified_recipient_and_running_worker_required", "telegram_bot_identity_mismatch",
            "telegram_webhook_or_unavailable", "telegram_discovery_unavailable",
            "private_recipient_challenge_not_found", "invalid_verification_challenge",
        } else type(error).__name__
        print(json.dumps({"ok": False, "reason": safe, "live_execution_enabled": False}))
        return 1
    finally:
        if args.command != "run":
            telegram.close()


if __name__ == "__main__":
    raise SystemExit(main())
