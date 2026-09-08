# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Risk manager — the layer ABOVE strategies (v2 plan §5 table).

Every intent passes through filter_intents(); strategies cannot override it.
Tracks realized PnL by UTC day/week using EVENT time (so it works identically
in backtest and live), enforces halts, and exposes the kill switch.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import RiskCfg
from .models import Intent, Side, intent_notional

log = logging.getLogger(__name__)

ENTRY_ACTIONS = {"REPLACE_LADDER", "MARKET_ENTER", "LIMIT_IOC_ENTER"}
EXIT_ACTIONS = {"MARKET_EXIT", "CANCEL_LADDER", "CANCEL_ENTRIES", "CANCEL_ALL", "PLACE_TP", "PLACE_STOP"}


@dataclass
class RiskEvent:
    ts: int
    kind: str            # HALT_DAY | HALT_WEEK | KILL | RESUME
    detail: str = ""


class RiskManager:
    def __init__(self, cfg: RiskCfg, initial_equity: float, state_file: Path | None = None):
        self.cfg = cfg
        self.equity = initial_equity
        self.last_mark_equity = initial_equity
        self.peak_equity = initial_equity
        self.killed = False
        self.halted_day = False
        self.halted_day_soft = False
        self.halted_week = False
        self.halted_system = False        # -6% from peak: full stop, manual review required
        self.persistence_failed = False
        self.events: list[RiskEvent] = []
        self._day: int | None = None
        self._week: int | None = None
        self._day_start_equity = initial_equity
        self._week_start_equity = initial_equity
        self._state_file = state_file
        self._audit_file = state_file.with_suffix(".events.jsonl") if state_file else None
        self._load()

    # ------------------------------------------------------------ time & pnl

    def on_ts(self, ts: int) -> None:
        day = ts // 86_400_000
        week = (ts // 86_400_000 + 3) // 7          # epoch day 0 was a Thursday
        if self._day != day:
            self._day = day
            self._day_start_equity = self.equity
            if self.halted_day:
                log.info("new UTC day: daily halt auto-cleared")
            self.halted_day = False
            self.halted_day_soft = False
        if self._week != week:
            self._week = week
            self._week_start_equity = self.equity
            self.halted_week = False

    def on_realized(self, ts: int, pnl_net: float) -> list[RiskEvent]:
        """Called by the executor with realized pnl net of fees. Returns new halt events."""
        self.on_ts(ts)
        self.equity += pnl_net
        self.last_mark_equity = self.equity
        self.peak_equity = max(self.peak_equity, self.equity)
        out: list[RiskEvent] = []
        # A realized loss is also a mark-to-market loss. Apply the soft gate
        # immediately so a fill cannot reopen risk before the next price mark.
        if (not self.halted_day_soft
                and self.equity <= self._day_start_equity * (1 - self.cfg.daily_soft_loss_halt)):
            self.halted_day_soft = True
            out.append(RiskEvent(ts, "HALT_DAY_SOFT",
                                 f"realized day pnl {self.equity - self._day_start_equity:.2f}"))
        if (not self.halted_system
                and self.equity <= self.peak_equity * (1 - self.cfg.system_drawdown_halt)):
            self.halted_system = True
            out.append(RiskEvent(ts, "HALT_SYSTEM_DD",
                                 f"equity {self.equity:.2f} vs peak {self.peak_equity:.2f}"))
        if (not self.halted_day
                and self.equity <= self._day_start_equity * (1 - self.cfg.daily_loss_halt)):
            self.halted_day = True
            out.append(RiskEvent(ts, "HALT_DAY",
                                 f"day pnl {self.equity - self._day_start_equity:.2f}"))
        if (not self.halted_week
                and self.equity <= self._week_start_equity * (1 - self.cfg.weekly_loss_halt)):
            self.halted_week = True
            out.append(RiskEvent(ts, "HALT_WEEK",
                                 f"week pnl {self.equity - self._week_start_equity:.2f}"))
        for e in out:
            log.warning("RISK %s %s", e.kind, e.detail)
            self.events.append(e)
            self._audit_event(e)
        self._save()
        return out

    def on_mark(self, ts: int, mark_equity: float) -> list[RiskEvent]:
        """Apply equity-based halts using realised + unrealised PnL."""
        previous_day, previous_week = self._day, self._week
        self.on_ts(ts)
        self.last_mark_equity = mark_equity
        if previous_day != self._day:
            self._day_start_equity = mark_equity
        if previous_week != self._week:
            self._week_start_equity = mark_equity
        self.peak_equity = max(self.peak_equity, mark_equity)
        out: list[RiskEvent] = []
        if not self.halted_day_soft and mark_equity <= self._day_start_equity * (
                1 - self.cfg.daily_soft_loss_halt):
            self.halted_day_soft = True
            out.append(RiskEvent(ts, "HALT_DAY_SOFT", f"mark equity {mark_equity:.2f}"))
        if not self.halted_day and mark_equity <= self._day_start_equity * (
                1 - self.cfg.daily_loss_halt):
            self.halted_day = True
            out.append(RiskEvent(ts, "HALT_DAY", f"mark equity {mark_equity:.2f}"))
        if not self.halted_week and mark_equity <= self._week_start_equity * (
                1 - self.cfg.weekly_loss_halt):
            self.halted_week = True
            out.append(RiskEvent(ts, "HALT_WEEK", f"mark equity {mark_equity:.2f}"))
        if not self.halted_system and mark_equity <= self.peak_equity * (
                1 - self.cfg.system_drawdown_halt):
            self.halted_system = True
            out.append(RiskEvent(ts, "HALT_SYSTEM_DD", f"mark equity {mark_equity:.2f}"))
        self.events.extend(out)
        for event in out:
            self._audit_event(event)
        if out:
            self._save()
        return out

    # ------------------------------------------------------------ gates

    @property
    def entries_allowed(self) -> bool:
        return not (self.killed or self.halted_day_soft or self.halted_day
                    or self.halted_week or self.halted_system or self.persistence_failed)

    @property
    def sizing_equity(self) -> float:
        return max(0.0, min(self.equity, self.last_mark_equity))

    def filter_intents(self, intents: list[Intent], ts: int, ref_price: float,
                       gross_notional: float, *, spread_pctl: float | None = None,
                       data_fresh: bool = True,
                       open_sides: set[Side] | None = None) -> list[Intent]:
        """Approve/adjust intents. Exits always pass; entries are gated."""
        self.on_ts(ts)
        out: list[Intent] = []
        reserved_gross = gross_notional
        reserved_by_strategy: dict[str, float] = {}
        portfolio_sides = set(open_sides or set())
        for it in intents:
            if it.action in EXIT_ACTIONS:
                out.append(it)
                continue
            if (not self.entries_allowed or not data_fresh
                    or (spread_pctl is not None and spread_pctl >= self.cfg.spread_guard_pctl)):
                # convert attempted arming into a cancel so ladders come down
                if it.action == "REPLACE_LADDER":
                    out.append(Intent("CANCEL_LADDER", it.strategy, side=it.side, reason="risk_halted"))
                continue
            if (it.action in ("MARKET_ENTER", "LIMIT_IOC_ENTER") and it.side is not None
                    and portfolio_sides and any(side is not it.side for side in portfolio_sides)):
                log.warning("risk: opposite-side entry rejected in one-way account")
                continue
            if it.action == "REPLACE_LADDER":
                gated = self._gate_ladder(
                    it, ref_price, reserved_gross, reserved_by_strategy.get(it.strategy, 0.0))
                out.append(gated)
                add = intent_notional(gated, ref_price) if gated is not None else 0.0
                reserved_gross += add
                reserved_by_strategy[it.strategy] = reserved_by_strategy.get(it.strategy, 0.0) + add
            elif it.action in ("MARKET_ENTER", "LIMIT_IOC_ENTER"):
                add = intent_notional(it, ref_price)
                if self._notional_ok(add, reserved_gross, it.strategy,
                                     reserved_by_strategy.get(it.strategy, 0.0)):
                    out.append(it)
                    reserved_gross += add
                    reserved_by_strategy[it.strategy] = reserved_by_strategy.get(it.strategy, 0.0) + add
                    if it.side is not None:
                        portfolio_sides.add(it.side)
                else:
                    log.warning("risk: MARKET_ENTER rejected (leverage cap)")
            else:
                out.append(it)
        return [i for i in out if i is not None]

    def _notional_ok(self, add_notional: float, gross_notional: float,
                     strategy: str, strategy_reserved: float = 0.0) -> bool:
        sizing = self.sizing_equity
        per_order = min(self.cfg.max_order_notional,
                        self.cfg.max_position_notional_frac * sizing)
        strategy_cap = self.cfg.strategy_notional_caps.get(strategy, 0.0) * sizing
        return (add_notional <= per_order and
                strategy_reserved + add_notional <= strategy_cap and
                gross_notional + add_notional <= self.cfg.max_gross_leverage * sizing)

    def _gate_ladder(self, it: Intent, ref_price: float, gross_notional: float,
                     strategy_reserved: float = 0.0) -> Intent | None:
        prices, qtys = [], []
        for p, q in zip(it.prices, it.qtys):
            if ref_price > 0 and abs(p - ref_price) / ref_price > self.cfg.price_sanity_band:
                continue                                   # fat-finger guard
            prices.append(p)
            qtys.append(q)
        add_notional = sum(p * q for p, q in zip(prices, qtys))
        sizing = self.sizing_equity
        gross_room = max(0.0, self.cfg.max_gross_leverage * sizing - gross_notional)
        order_cap = min(self.cfg.max_order_notional,
                        self.cfg.max_position_notional_frac * sizing)
        strategy_room = max(0.0, self.cfg.strategy_notional_caps.get(it.strategy, 0.0)
                            * sizing - strategy_reserved)
        allowed = min(gross_room, order_cap, strategy_room)
        if add_notional > allowed and add_notional > 0:
            scale = allowed / add_notional
            qtys = [q * scale for q in qtys]
        if not prices or all(q <= 0 for q in qtys):
            return Intent("CANCEL_LADDER", it.strategy, side=it.side, reason="risk_scaled_to_zero")
        return Intent("REPLACE_LADDER", it.strategy, side=it.side,
                      prices=prices, qtys=qtys, reason=it.reason)

    # ------------------------------------------------------------ manual controls

    def sync_external_equity(self, equity: float) -> None:
        """Adopt the exchange wallet balance at live startup, including deposits/withdrawals."""
        if equity <= 0:
            raise ValueError("external equity must be positive")
        previous = self.equity
        material = abs(equity - previous) / max(previous, 1e-9) > 0.05
        self.equity = equity
        self.last_mark_equity = equity
        if self._day is None or material:
            self._day_start_equity = equity
            self._week_start_equity = equity
            self.peak_equity = equity
            log.warning("risk anchors reset to exchange equity %.2f (was %.2f)", equity, previous)
        else:
            self.peak_equity = max(self.peak_equity, equity)
        self._save()

    def kill(self, ts: int = 0) -> None:
        self.killed = True
        self.events.append(RiskEvent(ts, "KILL"))
        self._audit_event(self.events[-1])
        log.warning("RISK KILL SWITCH ENGAGED")
        self._save()

    def resume(self, ts: int = 0, *, force: bool = False) -> None:
        if not force and (self.halted_day or self.halted_week or self.halted_system):
            raise RuntimeError("hard risk halt requires offline reviewed reset")
        self.killed = self.halted_day_soft = self.halted_day = self.halted_week = self.halted_system = False
        self.peak_equity = self.equity                 # reset the drawdown anchor on manual resume
        self.last_mark_equity = self.equity
        self.events.append(RiskEvent(ts, "RESUME"))
        self._audit_event(self.events[-1])
        log.warning("risk: manual resume")
        self._save()

    # ------------------------------------------------------------ persistence

    def _audit_event(self, event: RiskEvent) -> None:
        if self._audit_file is None:
            return
        try:
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": event.ts, "kind": event.kind,
                                         "detail": event.detail}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            log.exception("risk audit write failed; risk action remains active")

    def _save(self) -> None:
        if not self._state_file:
            return
        payload = json.dumps({
            "equity": self.equity, "peak_equity": self.peak_equity, "killed": self.killed,
            "last_mark_equity": self.last_mark_equity,
            "halted_system": self.halted_system,
            "halted_day_soft": self.halted_day_soft,
            "halted_day": self.halted_day, "halted_week": self.halted_week,
            "day": self._day, "week": self._week,
            "day_start_equity": self._day_start_equity,
            "week_start_equity": self._week_start_equity,
        })
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._state_file)
            self.persistence_failed = False
        except OSError:
            self.persistence_failed = True
            self.killed = True
            log.exception("risk state persistence failed; entries fail closed")

    def _load(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            d = json.loads(self._state_file.read_text())
            self.equity = d["equity"]
            self.last_mark_equity = d.get("last_mark_equity", self.equity)
            self.peak_equity = d.get("peak_equity", self.equity)
            self.halted_system = d.get("halted_system", False)
            self.halted_day_soft = d.get("halted_day_soft", False)
            self.killed = d["killed"]
            self.halted_day = d["halted_day"]
            self.halted_week = d["halted_week"]
            self._day, self._week = d["day"], d["week"]
            self._day_start_equity = d["day_start_equity"]
            self._week_start_equity = d["week_start_equity"]
            log.info("risk state restored: equity=%.2f killed=%s", self.equity, self.killed)
        except Exception:                                  # corrupt state -> fail closed
            self.killed = True
            self.halted_system = True
            log.exception("risk state file unreadable; failing closed until offline reset")

