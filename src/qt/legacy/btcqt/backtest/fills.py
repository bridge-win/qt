# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Pessimistic fill simulation (v2 plan §6 'cost and fill realism').

Rules:
- Resting LIMIT fills only if price trades THROUGH the level (strict <  for buys
  against candle.low, strict > for sells against candle.high). Touch != fill.
- STOP triggers on touch (candle range reaching it) and fills as taker at the
  stop price worsened by a slippage buffer.
- If a candle could hit both a position's STOP and TP, the STOP wins (worst case).
- MARKET orders execute at the NEXT candle's open, worsened by slippage, taker fee.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Candle, Fill, Order, Side


@dataclass
class PendingMarket:
    strategy: str
    side: Side
    qty: float
    reason: str
    delay: int = 0        # extra candles before execution (latency stress testing)
    limit_price: float | None = None


@dataclass
class SimBook:
    maker_fee: float
    taker_fee: float
    slippage_bps: float
    market_delay_bars: int = 0   # latency stress: market orders wait N extra candles
    orders: dict[str, Order] = field(default_factory=dict)
    pending_market: list[PendingMarket] = field(default_factory=list)
    _seq: int = 0

    # ------------------------------------------------------------ order admin

    def _oid(self, strategy: str, kind: str) -> str:
        self._seq += 1
        return f"{strategy}-{kind}-{self._seq}"

    def place_limit(self, strategy: str, side: Side, price: float, qty: float, tag: str = "") -> str:
        oid = self._oid(strategy, "LIMIT")
        self.orders[oid] = Order(oid, strategy, side, "LIMIT", price, qty, tag=tag)
        return oid

    def place_tp(self, strategy: str, side: Side, price: float, qty: float) -> str:
        self.cancel_kind(strategy, "TP")
        oid = self._oid(strategy, "TP")
        self.orders[oid] = Order(oid, strategy, side, "TP", price, qty, reduce_only=True)
        return oid

    def place_stop(self, strategy: str, side: Side, price: float, qty: float) -> str:
        self.cancel_kind(strategy, "STOP")
        oid = self._oid(strategy, "STOP")
        self.orders[oid] = Order(oid, strategy, side, "STOP", price, qty, reduce_only=True)
        return oid

    def market(self, strategy: str, side: Side, qty: float, reason: str = "") -> None:
        self.pending_market.append(
            PendingMarket(strategy, side, qty, reason, delay=self.market_delay_bars))

    def ioc(self, strategy: str, side: Side, qty: float, limit_price: float,
            reason: str = "") -> None:
        self.pending_market.append(PendingMarket(strategy, side, qty, reason,
                                                 delay=self.market_delay_bars,
                                                 limit_price=limit_price))

    def cancel_kind(self, strategy: str, kind: str, side: Side | None = None) -> None:
        for oid in [o for o, v in self.orders.items()
                    if v.strategy == strategy and v.kind == kind
                    and (side is None or v.side is side)]:
            del self.orders[oid]

    def cancel_all(self, strategy: str) -> None:
        for oid in [o for o, v in self.orders.items() if v.strategy == strategy]:
            del self.orders[oid]

    def ladder_orders(self, strategy: str, side: Side) -> list[Order]:
        return [v for v in self.orders.values()
                if v.strategy == strategy and v.kind == "LIMIT" and v.side is side]

    # ------------------------------------------------------------ candle processing

    def process_candle(self, c: Candle) -> list[Fill]:
        """Returns fills in pessimistic order: market opens, then stops, then
        limit entries, then TPs (a TP never rescues a candle that hit the stop)."""
        fills: list[Fill] = []
        slip = self.slippage_bps / 10_000.0

        # 1) queued market orders at open (delayed ones wait — latency stress)
        still_pending: list[PendingMarket] = []
        for pm in self.pending_market:
            if pm.delay > 0:
                pm.delay -= 1
                still_pending.append(pm)
                continue
            px = c.open * (1 + slip) if pm.side is Side.BUY else c.open * (1 - slip)
            if pm.limit_price is not None:
                fillable = px <= pm.limit_price if pm.side is Side.BUY else px >= pm.limit_price
                if not fillable:
                    continue
            fills.append(Fill(ts=c.ts, order_id=self._oid(pm.strategy, "MKT"),
                              strategy=pm.strategy, side=pm.side, price=px, qty=pm.qty,
                              fee=px * pm.qty * self.taker_fee, kind="MARKET", tag=pm.reason))
        self.pending_market = still_pending

        # 2) stops (worst case first)
        stop_hit: set[str] = set()
        for oid, o in list(self.orders.items()):
            if o.kind != "STOP":
                continue
            trig = (o.side is Side.SELL and c.low <= o.price) or \
                   (o.side is Side.BUY and c.high >= o.price)
            if trig:
                px = o.price * (1 - slip) if o.side is Side.SELL else o.price * (1 + slip)
                fills.append(Fill(ts=c.close_ts, order_id=oid, strategy=o.strategy,
                                  side=o.side, price=px, qty=o.qty,
                                  fee=px * o.qty * self.taker_fee, kind="STOP", tag=o.tag))
                stop_hit.add(o.strategy)
                del self.orders[oid]

        # 3) limit entries: strict through-price
        for oid, o in list(self.orders.items()):
            if o.kind != "LIMIT":
                continue
            through = (o.side is Side.BUY and c.low < o.price) or \
                      (o.side is Side.SELL and c.high > o.price)
            if through:
                fills.append(Fill(ts=c.close_ts, order_id=oid, strategy=o.strategy,
                                  side=o.side, price=o.price, qty=o.qty,
                                  fee=o.price * o.qty * self.maker_fee, kind="LIMIT", tag=o.tag))
                del self.orders[oid]

        # 4) TPs — skipped for any strategy whose stop already fired this candle
        for oid, o in list(self.orders.items()):
            if o.kind != "TP" or o.strategy in stop_hit:
                continue
            through = (o.side is Side.SELL and c.high > o.price) or \
                      (o.side is Side.BUY and c.low < o.price)
            if through:
                fills.append(Fill(ts=c.close_ts, order_id=oid, strategy=o.strategy,
                                  side=o.side, price=o.price, qty=o.qty,
                                  fee=o.price * o.qty * self.maker_fee, kind="TP", tag=o.tag))
                del self.orders[oid]
        return fills

