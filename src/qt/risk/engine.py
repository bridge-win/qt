"""Risk engine: enforces position sizing, drawdown, cooldowns, kill-switches."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from qt.core.config import RiskConfig
from qt.core.logging import get_logger
from qt.core.types import Position, Signal, SignalKind
from qt.risk.sizing import combine_sizers, fractional_kelly, vol_target_size
from qt.risk.stops import atr_stop_price, atr_take_profit, should_exit, time_stop_deadline

log = get_logger(__name__)


@dataclass
class RiskState:
    equity_peak: float = 0.0
    last_loss_ts: datetime | None = None
    kill_switch_armed: bool = False
    realized_pnl_total: float = 0.0


@dataclass
class RiskDecision:
    """Decision returned by the risk engine for a given signal/bar."""

    action: str   # "open" | "close" | "noop" | "block"
    size_quote: float = 0.0
    stop_price: float | None = None
    take_profit_price: float | None = None
    time_stop_ts: datetime | None = None
    reason: str = ""


@dataclass
class RiskEngine:
    cfg: RiskConfig
    bar_seconds: int = 3600
    state: RiskState = field(default_factory=RiskState)

    # ---- entry decision -------------------------------------------------

    def evaluate_entry(
        self,
        signal: Signal,
        equity: float,
        mark_price: float,
        atr_value: float,
        realized_vol_annual: float,
        position: Position,
        empirical_win_rate: float = 0.6,
        empirical_win_loss: float = 1.5,
    ) -> RiskDecision:
        """Decide whether and how much to deploy."""

        if signal.kind != SignalKind.ENTRY_LONG:
            return RiskDecision(action="noop")

        if self.state.kill_switch_armed:
            return RiskDecision(action="block", reason="kill_switch")

        # Drawdown kill-switch
        if equity > self.state.equity_peak:
            self.state.equity_peak = equity
        peak = self.state.equity_peak
        if peak > 0 and (peak - equity) / peak > self.cfg.max_drawdown_pct:
            self.state.kill_switch_armed = True
            log.warning("kill_switch_armed", peak=peak, equity=equity)
            return RiskDecision(action="block", reason="max_drawdown")

        # Cooldown after recent loss
        if self.state.last_loss_ts is not None:
            elapsed = signal.ts.timestamp() - self.state.last_loss_ts.timestamp()
            if elapsed < self.cfg.cooldown_bars * self.bar_seconds:
                return RiskDecision(action="block", reason="cooldown")

        # Don't pyramid for now: skip if already long.
        if not position.is_flat and position.qty > 0:
            return RiskDecision(action="block", reason="already_long")

        # Sizing — take min of (signal-suggested, Kelly, vol-target, hard cap)
        kelly = fractional_kelly(
            edge=signal.score,
            win_rate=empirical_win_rate,
            win_loss_ratio=empirical_win_loss,
            fraction=self.cfg.kelly_fraction,
        )
        vol_scalar = vol_target_size(realized_vol_annual, self.cfg.vol_target_annual)
        target_alloc = combine_sizers(
            signal.target_quote_alloc,
            kelly,
            vol_scalar,
            self.cfg.max_position_pct,
        )
        size_quote = max(0.0, equity * target_alloc)
        if size_quote <= 0:
            return RiskDecision(action="block", reason="size_zero")

        stop = atr_stop_price(mark_price, atr_value, mult=self.cfg.atr_stop_mult, side="long")
        tp = atr_take_profit(mark_price, atr_value, mult=self.cfg.atr_take_profit_mult, side="long")
        time_stop = time_stop_deadline(signal.ts, self.cfg.time_stop_bars, self.bar_seconds)

        return RiskDecision(
            action="open",
            size_quote=size_quote,
            stop_price=stop,
            take_profit_price=tp,
            time_stop_ts=time_stop,
            reason=f"score={signal.score:.2f}",
        )

    # ---- exit decision --------------------------------------------------

    def evaluate_exit(self, position: Position, mark_price: float, now: datetime) -> RiskDecision:
        if position.is_flat:
            return RiskDecision(action="noop")
        side = "long" if position.qty > 0 else "short"
        reason = should_exit(
            side=side,
            mark_price=mark_price,
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            now=now,
            time_stop_ts=position.time_stop_ts,
        )
        if reason is None:
            return RiskDecision(action="noop")
        return RiskDecision(action="close", reason=reason)

    # ---- post-trade bookkeeping ----------------------------------------

    def record_realized(self, pnl: float, ts: datetime) -> None:
        self.state.realized_pnl_total += pnl
        if pnl < 0:
            self.state.last_loss_ts = ts
