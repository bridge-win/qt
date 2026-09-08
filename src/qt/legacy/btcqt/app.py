# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Application orchestrator: wires collectors -> indicator engine -> strategies
-> risk -> broker, plus metrics API, telegram, recorder and watchdog.

Modes:
  record  Phase 1: data collection + indicators + metrics API (no trading, no keys)
  paper   Phase 3: adds strategies with simulated fills (no keys)
  live    Phase 4: real orders (requires keys + LIVE_TRADING=yes + explicit ack)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time

import uvicorn

from .api.server import StateStore, create_app
from .backtest.service import BacktestManager
from .config import Config
from .control import RuntimeController
from .data.binance_rest import BinanceRest
from .data.binance_ws import BinanceWs, RestPollers
from .data.fng import poll_fng
from .data.recorder import Recorder, event_to_row
from .data.backfill import download_range, load_funding, load_klines, load_oi
from .execution.paper import PaperBroker
from .indicators.engine import IndicatorEngine
from .models import (Candle, Fill, FundingRate, MarketState, OISnapshot, Trade,
                     intent_notional)
from .ops.telegram import build_notifier
from .ops.watchdog import Watchdog
from .risk import RiskManager
from .strategy.s0_trend import S0Trend
from .strategy.s1_wick_catcher import S1WickCatcher
from .strategy.s2_crowding_fader import S2CrowdingFader
from .backtest.engine import _TradeTracker
from .data.bybit_ws import BybitWs
from .data.okx_ws import OkxWs

log = logging.getLogger(__name__)

STATE_JOURNAL_FIELDS = tuple(MarketState.__dataclass_fields__)


class TradingApp:
    def __init__(self, cfg: Config, mode: str, ack_live: bool = False):
        assert mode in ("record", "paper", "live")
        self.cfg = cfg
        self.mode = mode
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)
        self.ws = BinanceWs(cfg.symbol, self.queue)
        self.bybit = BybitWs(cfg.data.bybit_symbol, self.queue) if cfg.data.bybit_enabled else None
        self.okx = OkxWs(cfg.data.okx_inst, self.queue) if cfg.data.okx_enabled else None
        self.engine = IndicatorEngine(cfg)
        self.store = StateStore()
        self.recorder = Recorder(cfg.data_path)
        self._pause_state_path = cfg.state_path / f"control_{mode}.json"
        self.paused = self._load_paused()
        self._stop = asyncio.Event()
        self._known_funding_ts: set[int] = set()
        self._last_candle_ts = 0
        self._last_alerted_drop_count = 0
        self.storage_failed = False
        self._storage_alerted = False
        self._emergency_active = False
        self.tracker = _TradeTracker()
        self.trades: list[Trade] = []

        self.strategies = []
        self.risk: RiskManager | None = None
        self.broker = None
        if mode in ("paper", "live"):
            if cfg.s0.enabled:
                self.strategies.append(S0Trend(cfg.s0))
            if cfg.s1.enabled:
                self.strategies.append(S1WickCatcher(cfg.s1))
            if cfg.s2.enabled:
                self.strategies.append(S2CrowdingFader(cfg.s2))
            self.risk = RiskManager(cfg.risk, cfg.account.initial_equity,
                                    cfg.state_path / f"risk_{mode}.json")
            if mode == "paper":
                self.broker = PaperBroker(cfg.account.maker_fee, cfg.account.taker_fee,
                                          cfg.account.stop_slippage_bps)
            else:
                from .execution.gateway import LiveBroker
                if not cfg.secrets.live_trading:
                    raise RuntimeError("live mode requires LIVE_TRADING=yes in .env")
                if not cfg.secrets.binance_api_key or not cfg.secrets.binance_api_secret:
                    raise RuntimeError("live mode requires BINANCE_API_KEY/SECRET in .env")
                if not cfg.secrets.metrics_api_token or not cfg.secrets.control_api_token:
                    raise RuntimeError("live mode requires distinct METRICS_API_TOKEN and CONTROL_API_TOKEN")
                if cfg.secrets.metrics_api_token == cfg.secrets.control_api_token:
                    raise RuntimeError("metrics and control tokens must be different")
                if min(len(cfg.secrets.metrics_api_token), len(cfg.secrets.control_api_token)) < 32:
                    raise RuntimeError("API tokens must be at least 32 characters in live mode")
                if "*" in cfg.api.cors_origins:
                    raise RuntimeError("wildcard CORS is forbidden in live mode")
                self.broker = LiveBroker(cfg.secrets.binance_api_key,
                                         cfg.secrets.binance_api_secret,
                                         cfg.symbol, ack_live)

        self.notifier = build_notifier(cfg.secrets.telegram_bot_token,
                                       cfg.secrets.telegram_chat_id, {
                                           "status": self._cb_status,
                                           "pause": lambda: self.controller.command("pause"),
                                           "resume": lambda: self.controller.command("resume"),
                                           "flatten": lambda: self.controller.command("flatten"),
                                           "kill": lambda: self.controller.command("kill"),
                                       })
        self.watchdog = Watchdog(cfg.risk.data_staleness_sec, cfg.watchdog.check_interval_sec,
                                 lambda: self.ws.last_msg_monotonic,
                                 self._on_stale, self._on_recovered)
        self.controller = RuntimeController(
            cfg, cfg.state_path,
            {"pause": self._cb_pause, "resume": self._cb_resume,
             "flatten": self._cb_flatten, "kill": self._cb_kill},
            self.engine.apply_runtime_config)
        self.controller.apply_saved()
        self.backtests = BacktestManager(cfg, cfg.data_path, cfg.api.backtest_max_days)
        self.api_app = create_app(self.store, cfg.data_path,
                                  token=cfg.secrets.metrics_api_token,
                                  cors_origins=cfg.api.cors_origins,
                                  history_max_rows=cfg.api.history_max_rows,
                                  controller=self.controller,
                                  control_token=cfg.secrets.control_api_token,
                                  control_token_required=cfg.api.control_token_required,
                                  backtests=self.backtests,
                                  dashboard_enabled=cfg.api.dashboard_enabled,
                                  assessment_config=cfg)

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        tasks: list[asyncio.Task] = []
        monitor: asyncio.Task | None = None
        pollers = None
        notifier_started = False
        try:
            await self._warmup()
            tasks = [asyncio.create_task(self.ws.run(), name="ws"),
                     asyncio.create_task(self._consume(), name="consume"),
                     asyncio.create_task(self._serve_api(), name="api"),
                     asyncio.create_task(self.watchdog.run(), name="watchdog"),
                     asyncio.create_task(poll_fng(self.queue), name="fng")]
            if self.bybit is not None:
                tasks.append(asyncio.create_task(self.bybit.run(), name="bybit"))
            if self.okx is not None:
                tasks.append(asyncio.create_task(self.okx.run(), name="okx"))
            async with BinanceRest() as rest:
                pollers = RestPollers(rest, self.cfg.symbol, self.queue,
                                      last_kline_ts=self._last_candle_ts)
                await pollers.poll_klines_once()
                tasks += [asyncio.create_task(pollers.run_oi(), name="oi"),
                          asyncio.create_task(pollers.run_lsr(), name="lsr"),
                          asyncio.create_task(pollers.run_klines(), name="klines-rest"),
                          asyncio.create_task(self._poll_funding(rest), name="funding")]
                if self.mode == "live":
                    await self.broker.reconcile(self.strategies)
                    self.risk.sync_external_equity(await self.broker.account_equity())
                    if self.risk.persistence_failed:
                        raise RuntimeError("risk state is not durable; refusing live startup")
                    tasks.append(asyncio.create_task(self._stream_live_fills(), name="fills-ws"))
                    tasks.append(asyncio.create_task(
                        self._poll_live_fills(), name="fills-rest-fallback"))
                await self.notifier.start()
                notifier_started = True
                monitor = asyncio.create_task(self._supervise(tasks), name="supervisor")
                log.info("btc-qt started: mode=%s symbol=%s paused=%s",
                         self.mode, self.cfg.symbol, self.paused)
                await self._stop.wait()
        finally:
            log.info("shutting down...")
            self.ws.stop()
            if self.bybit is not None:
                self.bybit.stop()
            if self.okx is not None:
                self.okx.stop()
            if pollers is not None:
                pollers.stop()
            self.watchdog.stop()
            if monitor is not None:
                monitor.cancel()
            for t in tasks:
                t.cancel()
            if monitor is not None:
                await asyncio.gather(monitor, return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.broker is not None:
                with contextlib.suppress(Exception):
                    await self.broker.cancel_entries()
                with contextlib.suppress(Exception):
                    await self.broker.close()
            try:
                self.recorder.flush()
            except Exception:
                log.exception("final recorder flush failed")
            if notifier_started:
                await self.notifier.stop()

    async def _supervise(self, tasks: list[asyncio.Task]) -> None:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if self._stop.is_set():
            return
        task = next(iter(done))
        exc = task.exception() if not task.cancelled() else None
        detail = f"critical task {task.get_name()} stopped"
        if exc is not None:
            detail += f": {type(exc).__name__}: {exc}"
        log.critical(detail)
        self._set_paused(True)
        try:
            if self.broker is not None:
                await self.broker.cancel_entries()
            await self.notifier.send(f"🚨 {detail}; entries paused and service stopping")
        except Exception:
            log.exception("critical-task safety cleanup failed")
        finally:
            self._stop.set()

    async def _serve_api(self) -> None:
        config = uvicorn.Config(self.api_app, host=self.cfg.api.host,
                                port=self.cfg.api.port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _warmup(self) -> None:
        """Restore rolling indicator state from closed 1m history before opening entries."""
        end_ms = int(time.time() * 1000) - 2 * 60_000
        start_ms = end_ms - 45 * 86_400_000
        history_start_ms = end_ms - 30 * 86_400_000
        latest_journal_ts = self.recorder.latest_ts("states")
        journal_after_ms = max(history_start_ms - 1, latest_journal_ts or 0)
        klines = await asyncio.to_thread(load_klines, self.cfg.data_path, start_ms, end_ms)
        expected = (end_ms - start_ms) // 60_000
        coverage_ok = (not klines.empty and len(klines) >= expected * 0.995
                       and int(klines.ts.iloc[-1]) >= end_ms - 120_000)
        if not coverage_ok:
            log.warning("startup history incomplete; downloading 45-day warm-up")
            try:
                download_start_ms = start_ms
                if not klines.empty and len(klines) >= expected * 0.995:
                    download_start_ms = int(klines.ts.iloc[-1]) + 60_000
                await download_range(
                    self.cfg.symbol, self.cfg.data_path, download_start_ms, end_ms)
            except Exception:
                if self.mode in ("paper", "live"):
                    raise
                log.exception("record warm-up download failed; continuing cold")
                return
            klines = await asyncio.to_thread(load_klines, self.cfg.data_path, start_ms, end_ms)
        coverage_ok = (not klines.empty and len(klines) >= expected * 0.995
                       and int(klines.ts.iloc[-1]) >= end_ms - 120_000)
        if not coverage_ok:
            if self.mode in ("paper", "live"):
                raise RuntimeError("45-day minute warm-up coverage below 99.5%; refusing trading mode")
            log.warning("record mode starting cold because warm-up data is unavailable")
            return

        funding = await asyncio.to_thread(load_funding, self.cfg.data_path)
        funding = funding[(funding.ts >= start_ms) & (funding.ts <= end_ms)]
        oi = await asyncio.to_thread(load_oi, self.cfg.data_path)
        if not oi.empty:
            oi = oi[(oi.ts >= start_ms) & (oi.ts <= end_ms)]
        funding_rows = list(funding.itertuples(index=False))
        self._known_funding_ts.update(int(row.ts) for row in funding_rows)
        oi_rows = list(oi.itertuples(index=False))
        fi = oi_i = 0
        state_rows: list[dict] = []
        last_state = None
        for row in klines.itertuples(index=False):
            close_ts = int(row.ts) + 60_000
            while fi < len(funding_rows) and int(funding_rows[fi].ts) <= close_ts:
                item = funding_rows[fi]
                self.engine.on_funding_settled(FundingRate(int(item.ts), float(item.rate)))
                fi += 1
            while oi_i < len(oi_rows) and int(oi_rows[oi_i].ts) <= close_ts:
                item = oi_rows[oi_i]
                self.engine.on_oi(OISnapshot(int(item.ts), float(item.oi)))
                oi_i += 1
            last_state = self.engine.on_candle(
                Candle(int(row.ts), float(row.open), float(row.high),
                       float(row.low), float(row.close), float(row.volume)))
            self._last_candle_ts = int(row.ts)
            if last_state.ts > journal_after_ms:
                state = last_state.to_dict()
                state_rows.append({key: state.get(key) for key in STATE_JOURNAL_FIELDS})
        self.engine.reset_transient_event()
        if state_rows:
            written = await asyncio.to_thread(
                self.recorder.append_many, "states", state_rows)
            log.info("dashboard state history rebuilt: %d minute rows", written)
        if last_state is not None:
            self.store.update(last_state.to_dict(), equity=self._equity(last_state.price),
                              positions=self._positions(), risk=self._risk_flags())
        log.info("indicator warm-up restored: %d minute bars (%s to %s)", len(klines),
                 int(klines.ts.iloc[0]), int(klines.ts.iloc[-1]))

    # ------------------------------------------------------------------ event loop

    async def _consume(self) -> None:
        while True:
            kind, ev = await self.queue.get()
            try:
                await self._handle(kind, ev)
            except Exception:
                log.exception("event handling failed: %s", kind)

    async def _handle(self, kind: str, ev) -> None:
        row = event_to_row(kind, ev)
        if row is not None:
            self._record(*row)
        if kind == "liq":                          # Binance (throttled snapshot)
            if not self._bybit_liq_fresh():        # engine feed only as fallback
                self.engine.on_force_order(ev)
        elif kind == "liq_bybit":                  # Bybit all-liquidation (primary)
            self._record("forceorder_bybit",
                         {"ts": ev.ts, "side": ev.side, "price": ev.price, "qty": ev.qty})
            if self.cfg.data.liq_primary == "bybit":
                self.engine.on_force_order(ev)
        elif kind == "xprice":
            venue, ts, price = ev
            self.engine.on_xprice(venue, ts, price)
        elif kind == "taker":
            ts, buy, sell = ev
            self.engine.on_taker(ts, buy, sell)
        elif kind == "book":
            self.engine.on_book(ev)
        elif kind == "mark":
            self.engine.on_mark_price(ev)
        elif kind == "oi":
            self.engine.on_oi(ev)
        elif kind == "fng":
            self.engine.on_fng(ev)
        elif kind == "lsr":
            self.engine.on_lsr(ev)
        elif kind == "funding_settled":
            self.engine.on_funding_settled(ev)
            if self.risk is not None and self.mode == "paper" and self.engine.last_state:
                mark = self.engine.last_state.price
                for strategy in self.strategies:
                    if not strategy.pos.is_flat():
                        pnl = -strategy.pos.side.sign * ev.rate * strategy.pos.qty * mark
                        self.risk.on_realized(ev.ts, pnl)
                        self.tracker.on_carry(strategy.name, pnl)
            elif self.risk is not None and self.mode == "live":
                # Wallet balance is exchange truth and includes settled funding/fees.
                self.risk.sync_external_equity(await self.broker.account_equity())
        elif kind == "candle":
            await self._on_candle(ev)

    async def _on_candle(self, c: Candle) -> None:
        if c.ts <= self._last_candle_ts:
            return
        self._last_candle_ts = c.ts
        if self.storage_failed and not self._storage_alerted:
            self._storage_alerted = True
            if self.broker is not None:
                await self.broker.cancel_entries()
            await self.notifier.send("🚨 data journal failed; entries blocked until operator restart")
        if self.ws.drop_count > self._last_alerted_drop_count:
            self._last_alerted_drop_count = self.ws.drop_count
            if self.broker is not None:
                await self.broker.cancel_entries()
            await self.notifier.send(
                f"⚠️ market queue dropped {self.ws.drop_count} events; entries blocked for 60s")
        # 1) fills for paper mode (backtest-identical rules)
        if self.mode == "paper":
            for f in await self.broker.on_candle(c):
                await self._route_fill(f)
        # 2) close candle -> state
        state = self.engine.on_candle(c)
        self._journal_state(state)
        # 3) mark-to-market risk checks include unrealized PnL.
        risk_mark = self.engine.current_mark or c.close
        mark_equity = self._equity(risk_mark)
        if self.risk is not None:
            mark_events = self.risk.on_mark(state.ts, mark_equity)
            for event in mark_events:
                await self.notifier.send(f"⛔ RISK {event.kind}: {event.detail}")
                if event.kind == "HALT_DAY_SOFT":
                    await self.broker.cancel_entries()
                else:
                    await self._flatten(event.kind)
        self.store.update(state.to_dict(), equity=mark_equity,
                          positions=self._positions(), risk=self._risk_flags())
        # 4) Strategies still manage exits while paused/stale; risk blocks only entries.
        if self.mode in ("paper", "live") and state.warmed_up:
            gross = sum(s.pos.qty * c.close for s in self.strategies if not s.pos.is_flat())
            portfolio_sides = {x.pos.side for x in self.strategies
                               if not x.pos.is_flat() and x.pos.side is not None}
            for s in self.strategies:
                intents = s.on_state(state, self.risk.sizing_equity)
                approved = self.risk.filter_intents(
                    intents, state.ts, c.close, gross,
                    spread_pctl=state.spread_pctl,
                    data_fresh=(not self.paused and not self.watchdog.stale
                                and not self.storage_failed
                                and (self.ws.last_drop_monotonic == 0
                                     or time.monotonic() - self.ws.last_drop_monotonic > 60)),
                    open_sides=portfolio_sides)
                for it in approved:
                    await self.broker.apply(it)
                gross += sum(intent_notional(it, c.close) for it in approved)
                portfolio_sides.update(it.side for it in approved
                                       if it.action in ("MARKET_ENTER", "LIMIT_IOC_ENTER")
                                       and it.side is not None)

    async def _route_fill(self, f: Fill) -> None:
        strat = next((s for s in self.strategies if s.name == f.strategy), None)
        if strat is None:
            return
        intents, realized = strat.on_fill(f, self.engine.last_state)
        halt_events = self.risk.on_realized(f.ts, realized - f.fee)
        fill_mark = self.engine.current_mark or (self.engine.last_state.price
                                                  if self.engine.last_state else f.price)
        halt_events += self.risk.on_mark(f.ts, self._equity(fill_mark))
        tr = self.tracker.on_fill(f, strat.pos.is_flat(), realized, strat.pos.side)
        # A newly filled entry is not accepted as safe until an exchange-side stop exists.
        entry_fill = realized == 0.0 and not strat.pos.is_flat()
        protection = sorted(intents, key=lambda it: 0 if it.action == "PLACE_STOP" else 1)
        stop_intents = [it for it in protection if it.action == "PLACE_STOP"]
        if entry_fill and not stop_intents:
            await self._protection_failure(strat.name, "strategy emitted no protective stop")
            return
        for it in protection:
            try:
                if it.action == "PLACE_STOP":
                    self._validate_stop(strat, it, f.price)
                await self.broker.apply(it)
            except Exception as exc:
                if it.action == "PLACE_STOP":
                    await self._protection_failure(strat.name, str(exc))
                    return
                log.exception("follow-up order failed: %s", it.action)
                await self.notifier.send(f"⚠️ {strat.name} {it.action} failed: {exc}")
        if tr is not None:
            self.trades.append(tr)
            self._record("trades", tr.__dict__)
            await self.notifier.send(
                f"{'🟢' if tr.pnl > 0 else '🔴'} {tr.strategy} {tr.side} closed "
                f"{tr.exit_kind} pnl={tr.pnl:+.2f} eq={self.risk.equity:.2f}")
        for e in halt_events:
            if e.kind == "HALT_DAY_SOFT":
                await self.notifier.send(f"⛔ RISK {e.kind}: {e.detail} — entries halted")
                await self.broker.cancel_entries()
            else:
                await self.notifier.send(f"⛔ RISK {e.kind}: {e.detail} — flatten + halt")
                await self._flatten(e.kind)

    async def _poll_live_fills(self) -> None:
        while True:
            await asyncio.sleep(1 if (self.broker.needs_fast_poll
                                      or not self.broker.private_stream_connected) else 10)
            try:
                for f in await self.broker.poll_fills():
                    await self._route_fill(f)
            except Exception as exc:
                from .execution.gateway import OrderStateUnknown
                if isinstance(exc, OrderStateUnknown):
                    await self._handle_order_state_unknown(exc)
                    return
                log.exception("live fill polling failed")

    async def _stream_live_fills(self) -> None:
        try:
            async for fill in self.broker.stream_fills():
                await self._route_fill(fill)
        except Exception as exc:
            from .execution.gateway import OrderStateUnknown
            if isinstance(exc, OrderStateUnknown):
                await self._handle_order_state_unknown(exc)
                return
            raise

    async def _handle_order_state_unknown(self, exc: Exception) -> None:
        if self._emergency_active:
            return
        self._emergency_active = True
        log.critical("%s", exc)
        self.risk.kill(int(time.time() * 1000))
        self._set_paused(True)
        await self.notifier.send(f"🚨 {exc}; flattening exchange truth and stopping")
        try:
            await self.broker.emergency_flatten_exchange()
        finally:
            self._stop.set()

    async def _poll_funding(self, rest: BinanceRest) -> None:
        while True:
            try:
                rows = await rest.funding_history(self.cfg.symbol, limit=3)
                for r in rows:
                    ts = int(r["fundingTime"])
                    if ts not in self._known_funding_ts:
                        self._known_funding_ts.add(ts)
                        self.queue.put_nowait(("funding_settled",
                                               FundingRate(ts, float(r["fundingRate"]))))
            except Exception as e:
                log.warning("funding poll failed: %s", e)
            await asyncio.sleep(600)

    # ------------------------------------------------------------------ helpers

    def _bybit_liq_fresh(self) -> bool:
        if self.bybit is None or self.cfg.data.liq_primary != "bybit":
            return False
        age = time.monotonic() - self.bybit.last_msg_monotonic
        return self.bybit.last_msg_monotonic > 0 and age <= self.cfg.data.liq_fallback_stale_sec

    def _journal_state(self, state) -> None:
        d = state.to_dict()
        self._record("states", {k: d.get(k) for k in STATE_JOURNAL_FIELDS})

    def _record(self, table: str, row: dict) -> None:
        try:
            self.recorder.add(table, row)
        except Exception:
            self.storage_failed = True
            log.exception("journal write failed for %s; entries fail closed", table)

    def _equity(self, price: float) -> float | None:
        if self.risk is None:
            return None
        u = sum((price - s.pos.avg_price) * s.pos.qty * s.pos.side.sign
                for s in self.strategies if not s.pos.is_flat())
        return self.risk.equity + u

    def _positions(self) -> list[dict]:
        return [{"strategy": s.name, "side": s.pos.side.value, "qty": round(s.pos.qty, 6),
                 "avg_price": round(s.pos.avg_price, 2)}
                for s in self.strategies if not s.pos.is_flat()]

    def _risk_flags(self) -> dict:
        if self.risk is None:
            return {"mode": self.mode}
        return {"mode": self.mode, "paused": self.paused, "killed": self.risk.killed,
                "halted_day_soft": self.risk.halted_day_soft,
                "halted_day": self.risk.halted_day, "halted_week": self.risk.halted_week,
                "halted_system": self.risk.halted_system,
                "risk_persistence_failed": self.risk.persistence_failed,
                "data_stale": self.watchdog.stale,
                "queue_drop_count": self.ws.drop_count,
                "storage_failed": self.storage_failed,
                "sources": {
                    "binance": self.ws.last_msg_monotonic > 0,
                    "bybit": bool(self.bybit and self.bybit.last_msg_monotonic > 0),
                    "okx": bool(self.okx and self.okx.last_msg_monotonic > 0),
                }}

    def _validate_stop(self, strat, intent, fill_price: float) -> None:
        if intent.price is None or intent.qty is None or intent.qty <= 0:
            raise RuntimeError("invalid stop payload")
        side = strat.pos.side
        if side is None:
            raise RuntimeError("cannot protect a flat position")
        anchor = strat.pos.avg_price or fill_price
        correct_side = ((side.value == "BUY" and intent.price < anchor)
                        or (side.value == "SELL" and intent.price > anchor))
        distance_bps = abs(intent.price - anchor) / anchor * 10_000
        if not correct_side or distance_bps < self.cfg.risk.min_stop_distance_bps:
            raise RuntimeError(f"unsafe stop geometry ({distance_bps:.2f} bps)")

    async def _protection_failure(self, strategy: str, detail: str) -> None:
        if self._emergency_active:
            return
        self._emergency_active = True
        log.critical("PROTECTION FAILURE %s: %s", strategy, detail)
        if self.risk is not None:
            self.risk.kill(int(time.time() * 1000))
        self._set_paused(True)
        await self.notifier.send(f"🚨 PROTECTION FAILURE {strategy}: {detail}; emergency flatten")
        if self.mode == "live":
            try:
                await self.broker.emergency_flatten_exchange()
            finally:
                self._stop.set()
        else:
            await self._flatten("protection_failure")

    async def _flatten(self, reason: str) -> None:
        log.warning("flatten all (%s)", reason)
        if self.mode == "live":
            await self.broker.flatten_all(self.strategies)
        elif self.mode == "paper":
            await self.broker.cancel_everything()
            for s in self.strategies:
                if not s.pos.is_flat():
                    from .models import Intent
                    await self.broker.apply(Intent("MARKET_EXIT", s.name,
                                                   side=s.pos.side.opposite,
                                                   qty=s.pos.qty, reason=reason))

    # ------------------------------------------------------------------ telegram callbacks

    async def _cb_status(self) -> str:
        s = self.store.snapshot or {}
        lines = [f"mode={self.mode} regime={s.get('regime')} price={s.get('price')}",
                 f"cascade={s.get('cascade_score', 0):.0f} crowding={s.get('crowding_score', 0):.0f} "
                 f"eatfear={s.get('eatfear', 0):.0f}",
                 f"equity={self.store.equity} positions={self.store.positions}",
                 f"risk={self._risk_flags()}"]
        return "\n".join(lines)

    async def _cb_pause(self) -> str:
        self._set_paused(True)
        if self.broker is not None:
            await self.broker.cancel_entries()
        return "paused: no new entries; protective stop/TP orders preserved"

    async def _cb_resume(self) -> str:
        if self.risk is not None:
            self.risk.resume(int(time.time() * 1000))
        self._set_paused(False)
        return "resumed"

    async def _cb_flatten(self) -> str:
        self._set_paused(True)
        await self._flatten("manual")
        return "paused and flatten requested"

    async def _cb_kill(self) -> str:
        if self.risk is not None:
            self.risk.kill(int(time.time() * 1000))
        self._set_paused(True)
        await self._flatten("kill")
        return "KILL engaged: flat + halted. /resume to re-enable."

    def _load_paused(self) -> bool:
        try:
            return bool(json.loads(self._pause_state_path.read_text()).get("paused", False))
        except FileNotFoundError:
            return False
        except Exception:
            log.exception("control pause state unreadable; failing closed as paused")
            return True

    def _set_paused(self, value: bool) -> None:
        if value:
            self.paused = True                    # safety action must survive disk failure
        try:
            self._pause_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pause_state_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"paused": value, "ts": int(time.time() * 1000)}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._pause_state_path)
            self.paused = value
        except OSError:
            log.exception("pause state persistence failed")
            if not value:
                raise RuntimeError("cannot durably resume; remaining paused")

    # ------------------------------------------------------------------ watchdog callbacks

    async def _on_stale(self) -> None:
        if self.broker is not None:
            await self.broker.cancel_entries()
        await self.notifier.send("⚠️ 数据流中断 >10s：已撤入场单；交易所止损/止盈保持有效")

    async def _on_recovered(self) -> None:
        await self.notifier.send("✅ 数据流已恢复")

