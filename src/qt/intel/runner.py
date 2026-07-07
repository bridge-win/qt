"""Background runner for the intelligence-discovery pseudo strategy."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, TypeAlias

import pandas as pd
import yaml

from qt.core.config import Settings
from qt.core.logging import get_logger
from qt.data.market import fetch_ohlcv
from qt.intel.ranker import persist_opportunities, rank_opportunities
from qt.intel.scanners import (
    BasisScanner,
    DepegScanner,
    ExchangeFactory,
    FundingScanner,
    Opportunity,
    ScannerSettings,
    SpreadScanner,
    WickScanner,
    ccxt_exchange_factory,
)
from qt.monitoring.alerts import alert
from qt.monitoring.state import MonitorStateStore, new_snapshot, with_update
from qt.strategies.base import StrategyConfig

log = get_logger(__name__)

ScannerName: TypeAlias = str


class Scanner(Protocol):
    def scan(self, settings: ScannerSettings) -> list[Opportunity]: ...


OhlcvProvider: TypeAlias = Callable[[ScannerSettings], pd.DataFrame]


def load_intel_config(config_path: str | Path) -> StrategyConfig:
    path = Path(config_path)
    if not path.exists():
        return StrategyConfig(
            name="intel",
            enabled=False,
            interval_seconds=300,
            description="Intelligence discovery scanner",
            params={},
        )
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("name", "intel")
    return StrategyConfig.model_validate(raw)


def run_intel_once(
    *,
    params: dict[str, object],
    runtime_dir: str | Path,
    exchange_factory: ExchangeFactory = ccxt_exchange_factory,
    ohlcv_provider: OhlcvProvider | None = None,
) -> list[Opportunity]:
    settings = ScannerSettings.from_mapping(params)
    scanners = _build_scanners(
        params,
        exchange_factory=exchange_factory,
        ohlcv_provider=ohlcv_provider or _live_ohlcv_provider,
    )
    opportunities: list[Opportunity] = []
    for name, scanner in scanners:
        try:
            opportunities.extend(scanner.scan(settings))
        except Exception as exc:
            log.warning("intel_scanner_failed", scanner=name, error=str(exc))
    top_n_raw = params.get("top_n", 20)
    top_n = top_n_raw if isinstance(top_n_raw, int) else 20
    ranked = rank_opportunities(opportunities, top_n=top_n)
    persist_opportunities(runtime_dir, ranked)
    return ranked


def run_intel_forever(
    *,
    settings: Settings,
    runtime_dir: str | Path,
    config_path: str | Path = "config/strategies/intel.yaml",
    stop_event: threading.Event,
    exchange_factory: ExchangeFactory = ccxt_exchange_factory,
    max_backoff_seconds: int = 300,
) -> None:
    runtime = Path(runtime_dir)
    state_path = runtime / "intel" / "state.json"
    store = MonitorStateStore(state_path)
    snapshot = new_snapshot(
        name="intel",
        mode=settings.execution.mode,
        status="starting",
        details={"description": "Intelligence discovery scanner"},
    )
    store.write(snapshot)
    cycle = 0
    consecutive_failures = 0

    while not stop_event.is_set():
        cycle += 1
        cfg = load_intel_config(config_path)
        interval = max(1, int(cfg.interval_seconds))
        if not cfg.enabled:
            snapshot = with_update(
                snapshot,
                status="stopped",
                cycle=cycle,
                consecutive_failures=0,
                last_error=None,
                details={"description": cfg.description, "enabled": False},
            )
            store.write(snapshot)
            stop_event.wait(timeout=interval)
            continue

        try:
            ranked = run_intel_once(
                params=cfg.params,
                runtime_dir=runtime,
                exchange_factory=exchange_factory,
            )
            consecutive_failures = 0
            if ranked:
                top = ranked[0]
                alert(
                    f"intel opportunity: {top.kind} {top.symbol} {top.edge_bps:.1f} bps",
                    severity=cfg.min_alert_severity,
                    venue=top.venue,
                    action=top.action,
                    why=top.why,
                )
            snapshot = with_update(
                snapshot,
                status="healthy",
                cycle=cycle,
                consecutive_failures=0,
                last_error=None,
                next_run_at=datetime.now(tz=timezone.utc) + timedelta(seconds=interval),
                details={
                    "description": cfg.description,
                    "enabled": True,
                    "count": len(ranked),
                    "top": ranked[0].as_dict() if ranked else None,
                },
            )
            store.write(snapshot)
            stop_event.wait(timeout=interval)
        except Exception as exc:
            consecutive_failures += 1
            backoff = min(max_backoff_seconds, interval * consecutive_failures)
            snapshot = with_update(
                snapshot,
                status="degraded",
                cycle=cycle,
                consecutive_failures=consecutive_failures,
                last_error=str(exc),
                next_run_at=datetime.now(tz=timezone.utc) + timedelta(seconds=backoff),
                details={"description": cfg.description, "enabled": True},
            )
            store.write(snapshot)
            stop_event.wait(timeout=backoff)

    snapshot = with_update(snapshot, status="stopped", cycle=cycle)
    store.write(snapshot)


def start_intel_thread(
    *,
    settings: Settings,
    runtime_dir: str | Path,
    config_path: str | Path,
    stop_event: threading.Event,
) -> threading.Thread:
    thread = threading.Thread(
        target=run_intel_forever,
        kwargs={
            "settings": settings,
            "runtime_dir": runtime_dir,
            "config_path": config_path,
            "stop_event": stop_event,
        },
        name="intel",
        daemon=True,
    )
    thread.start()
    return thread


def _build_scanners(
    params: dict[str, object],
    *,
    exchange_factory: ExchangeFactory,
    ohlcv_provider: OhlcvProvider,
) -> list[tuple[ScannerName, Scanner]]:
    requested_raw = params.get("scanners", ["funding", "spread", "basis", "depeg", "wick"])
    requested = (
        [str(item) for item in requested_raw]
        if isinstance(requested_raw, list)
        else ["funding", "spread", "basis", "depeg", "wick"]
    )
    out: list[tuple[ScannerName, Scanner]] = []
    for name in requested:
        if name == "funding":
            out.append((name, FundingScanner(exchange_factory)))
        elif name == "spread":
            out.append((name, SpreadScanner(exchange_factory)))
        elif name == "basis":
            out.append((name, BasisScanner(exchange_factory)))
        elif name == "depeg":
            out.append((name, DepegScanner(exchange_factory)))
        elif name == "wick":
            out.append((name, WickScanner(ohlcv_provider)))
    return out


def _live_ohlcv_provider(scanner_settings: ScannerSettings) -> pd.DataFrame:
    since = datetime.now(tz=timezone.utc) - timedelta(days=scanner_settings.wick_history_days)
    symbol = scanner_settings.symbols[0] if scanner_settings.symbols else "BTC/USDT"
    return fetch_ohlcv(
        scanner_settings.wick_exchange,
        symbol,
        scanner_settings.wick_timeframe,
        since=since,
    )


__all__ = [
    "load_intel_config",
    "run_intel_forever",
    "run_intel_once",
    "start_intel_thread",
]
