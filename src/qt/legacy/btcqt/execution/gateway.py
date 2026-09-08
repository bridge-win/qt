# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Live broker for Binance USDT-M via ccxt (Phase 4 only).

Safety properties (v2 plan §5/§9):
- refuses to construct unless LIVE_TRADING=yes AND the CLI ack flag was passed
- post-only (GTX) ladder orders: never pays taker on entries
- STOP_MARKET is placed ON THE EXCHANGE (reduce-only, mark-price trigger):
  a dead VPS never leaves a naked position
- idempotent clientOrderId prefix `qtS1-`/`qtS2-`; startup reconcile adopts
  exchange state as truth and cancels orphans
- private user-data WebSocket drives fills/partial fills; a low-rate REST poll
  reconciles gaps after disconnects without double-counting
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time

import aiohttp
import ccxt.async_support as ccxt
try:
    import websockets
except ModuleNotFoundError:  # optional live-feed dependency
    websockets = None  # type: ignore[assignment]

from ..models import Fill, Intent, Side

log = logging.getLogger(__name__)


def _truthy(value) -> bool:
    return value is True or str(value).lower() in {"true", "1", "yes"}


class OrderStateUnknown(RuntimeError):
    pass


class LiveBroker:
    def __init__(self, api_key: str, secret: str, symbol: str, ack_live: bool,
                 leverage: int = 1):
        if not ack_live:
            raise RuntimeError("live trading requires the explicit acknowledgement flag")
        self.symbol_ccxt = symbol.replace("USDT", "/USDT:USDT")   # BTCUSDT -> BTC/USDT:USDT
        self.x = ccxt.binanceusdm({
            "apiKey": api_key, "secret": secret,
            "options": {"defaultType": "future"},
            "enableRateLimit": True,
        })
        self._tracked: dict[str, dict] = {}      # clientOrderId -> {strategy, kind, side, tag}
        self._seq = int(time.time())
        self.leverage = max(1, min(int(leverage), 2))
        self.api_key = api_key
        self.private_stream_connected = False

    # ------------------------------------------------------------ helpers

    def _cid(self, strategy: str, kind: str) -> str:
        self._seq += 1
        return f"qt{strategy}-{kind}-{self._seq}"

    async def _create(self, kind: str, strategy: str, side: Side, qty: float,
                      price: float | None = None, reduce_only: bool = False,
                      stop_price: float | None = None, tag: str = ""):
        cid = self._cid(strategy, kind)
        params: dict = {"newClientOrderId": cid}
        if reduce_only:
            params["reduceOnly"] = True
        self._tracked[cid] = {"strategy": strategy, "kind": kind, "side": side,
                              "tag": tag, "filled": 0.0, "fee_seen": 0.0,
                              "created_monotonic": time.monotonic()}
        try:
            if kind == "LIMIT" or kind == "TP":
                params["timeInForce"] = "GTX"          # post-only; rejected if it would take
                created = await self.x.create_order(self.symbol_ccxt, "limit", side.value.lower(),
                                                    qty, price, params)
            elif kind == "IOC":
                params["timeInForce"] = "IOC"
                created = await self.x.create_order(self.symbol_ccxt, "limit", side.value.lower(),
                                                    qty, price, params)
            elif kind == "STOP":
                params.update({"stopPrice": stop_price, "workingType": "MARK_PRICE"})
                created = await self.x.create_order(self.symbol_ccxt, "STOP_MARKET",
                                                    side.value.lower(), qty, None, params)
            elif kind == "MARKET":
                created = await self.x.create_order(self.symbol_ccxt, "market", side.value.lower(),
                                                    qty, None, params)
            return created
        except ccxt.OrderImmediatelyFillable:
            self._tracked.pop(cid, None)
            if kind == "TP":
                log.warning("post-only TP already crossed; taking reduce-only market exit")
                return await self._create("MARKET", strategy, side, qty,
                                          reduce_only=True, tag="tp_crossed")
            log.info("post-only entry rejected by exchange (would take) — intended behaviour")
        except Exception:
            self._tracked.pop(cid, None)
            log.exception("order create failed (%s %s)", kind, strategy)
            if kind in ("STOP", "MARKET", "IOC"):
                raise

    async def _cancel_where(self, pred) -> None:
        for cid in [c for c, meta in self._tracked.items() if pred(meta)]:
            try:
                await self.x.cancel_order(None, self.symbol_ccxt,
                                          {"origClientOrderId": cid})
            except ccxt.OrderNotFound:
                pass
            except Exception:
                log.exception("cancel failed %s", cid)
                raise
            self._tracked.pop(cid, None)

    # ------------------------------------------------------------ BrokerProtocol

    @property
    def needs_fast_poll(self) -> bool:
        return any(meta.get("kind") in {"LIMIT", "IOC", "MARKET"}
                   for meta in self._tracked.values())

    async def apply(self, it: Intent) -> None:
        if it.action == "REPLACE_LADDER":
            await self._cancel_where(lambda m: m["strategy"] == it.strategy
                                     and m["kind"] == "LIMIT" and m["side"] is it.side)
            for p, q in zip(it.prices, it.qtys):
                if q > 0:
                    await self._create("LIMIT", it.strategy, it.side, q, price=p)
        elif it.action == "CANCEL_LADDER":
            await self._cancel_where(lambda m: m["strategy"] == it.strategy
                                     and m["kind"] == "LIMIT"
                                     and (it.side is None or m["side"] is it.side))
        elif it.action == "CANCEL_ALL":
            await self._cancel_where(lambda m: m["strategy"] == it.strategy)
        elif it.action == "PLACE_TP":
            await self._cancel_where(lambda m: m["strategy"] == it.strategy and m["kind"] == "TP")
            await self._create("TP", it.strategy, it.side, it.qty, price=it.price,
                               reduce_only=True)
        elif it.action == "PLACE_STOP":
            await self._cancel_where(lambda m: m["strategy"] == it.strategy and m["kind"] == "STOP")
            await self._create("STOP", it.strategy, it.side, it.qty, stop_price=it.price,
                               reduce_only=True)
        elif it.action in ("MARKET_EXIT", "MARKET_ENTER"):
            await self._create("MARKET", it.strategy, it.side, it.qty,
                               reduce_only=(it.action == "MARKET_EXIT"), tag=it.reason)
        elif it.action == "LIMIT_IOC_ENTER":
            await self._create("IOC", it.strategy, it.side, it.qty, price=it.price,
                               tag=it.reason)
        elif it.action == "CANCEL_ENTRIES":
            await self._cancel_where(lambda m: m["strategy"] == it.strategy
                                     and m["kind"] in ("LIMIT", "IOC"))

    async def poll_fills(self) -> list[Fill]:
        """Diff tracked orders against exchange state; emit fills for closed ones."""
        fills: list[Fill] = []
        for cid, meta in list(self._tracked.items()):
            try:
                o = await self.x.fetch_order(None, self.symbol_ccxt,
                                             {"origClientOrderId": cid})
            except ccxt.OrderNotFound:
                if time.monotonic() - meta.get("created_monotonic", 0) > 30:
                    raise OrderStateUnknown(f"tracked order unresolved for 30 seconds: {cid}")
                continue
            except Exception:
                log.exception("fetch_order failed %s", cid)
                continue
            status = (o.get("status") or "").lower()
            filled = float(o.get("filled") or 0)
            previous = float(meta.get("filled") or 0)
            delta = max(0.0, filled - previous)
            if delta > 0:
                px = float(o.get("average") or o.get("price") or 0)
                if px <= 0:
                    raise OrderStateUnknown(f"filled order has no executable price: {cid}")
                fee_cost = sum(float(f.get("cost", 0)) for f in (o.get("fees") or []))
                if fee_cost == 0 and o.get("fee"):
                    fee_cost = float((o.get("fee") or {}).get("cost") or 0)
                fee_delta = max(0.0, fee_cost - float(meta.get("fee_seen") or 0))
                fills.append(Fill(ts=int(o.get("lastTradeTimestamp") or o.get("timestamp")
                                         or time.time() * 1000),
                                  order_id=cid, strategy=meta["strategy"],
                                  side=meta["side"], price=px, qty=delta,
                                  fee=fee_delta,
                                  kind="MARKET" if meta["kind"] == "IOC" else meta["kind"],
                                  tag=meta["tag"]))
                meta["filled"] = filled
                meta["fee_seen"] = fee_cost
            if status in ("closed",):
                self._tracked.pop(cid, None)
            elif status in ("canceled", "expired", "rejected"):
                self._tracked.pop(cid, None)
        return fills

    async def stream_fills(self):
        """Yield private order-trade updates; reconnect and REST polling cover gaps."""
        backoff = 1.0
        while True:
            renew_task = None
            try:
                async with aiohttp.ClientSession(
                        headers={"X-MBX-APIKEY": self.api_key}) as session:
                    async with session.post(
                            "https://fapi.binance.com/fapi/v1/listenKey") as response:
                        response.raise_for_status()
                        listen_key = (await response.json())["listenKey"]
                    renew_task = asyncio.create_task(
                        self._renew_listen_key(session, listen_key), name="listenkey-renew")
                    url = f"wss://fstream.binance.com/ws/{listen_key}"
                    async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                                  max_queue=4096) as ws:
                        self.private_stream_connected = True
                        backoff = 1.0
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("e") != "ORDER_TRADE_UPDATE":
                                continue
                            order = msg.get("o") or {}
                            cid = str(order.get("c") or "")
                            meta = self._tracked.get(cid)
                            if meta is None:
                                continue
                            if order.get("x") == "TRADE" and float(order.get("l") or 0) > 0:
                                qty = float(order["l"])
                                fee = float(order.get("n") or 0)
                                price = float(order.get("L") or order.get("ap") or 0)
                                if price <= 0:
                                    raise OrderStateUnknown(
                                        f"private fill has no executable price: {cid}")
                                meta["filled"] = float(meta.get("filled") or 0) + qty
                                meta["fee_seen"] = float(meta.get("fee_seen") or 0) + fee
                                yield Fill(
                                    ts=int(order.get("T") or msg.get("E") or time.time() * 1000),
                                    order_id=cid, strategy=meta["strategy"], side=meta["side"],
                                    price=price, qty=qty,
                                    fee=fee,
                                    kind="MARKET" if meta["kind"] == "IOC" else meta["kind"],
                                    tag=meta["tag"])
                            if str(order.get("X") or "").upper() in {
                                    "FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                                self._tracked.pop(cid, None)
            except asyncio.CancelledError:
                raise
            except OrderStateUnknown:
                raise
            except Exception as exc:
                log.error("private fill stream reconnecting: %s", exc)
                await asyncio.sleep(backoff + random.random())
                backoff = min(30.0, backoff * 2)
            finally:
                self.private_stream_connected = False
                if renew_task:
                    renew_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await renew_task

    async def _renew_listen_key(self, session: aiohttp.ClientSession,
                                listen_key: str) -> None:
        while True:
            await asyncio.sleep(30 * 60)
            async with session.put("https://fapi.binance.com/fapi/v1/listenKey",
                                   params={"listenKey": listen_key}) as response:
                response.raise_for_status()

    async def reconcile(self, strategies) -> None:
        """Reconcile safely: cancel entry orders, preserve protective reduce-only orders."""
        try:
            await self.x.load_markets()
            try:
                await self.x.set_position_mode(False)
            except Exception as exc:
                if "No need to change" not in str(exc):
                    raise RuntimeError("cannot confirm one-way position mode") from exc
            try:
                await self.x.set_margin_mode("isolated", self.symbol_ccxt)
            except Exception as exc:
                if "No need to change" not in str(exc):
                    raise RuntimeError("cannot confirm isolated margin mode") from exc
            await self.x.set_leverage(self.leverage, self.symbol_ccxt)
            open_orders = await self.x.fetch_open_orders()
            protection_by_strategy: dict[str, list[dict]] = {}
            for o in open_orders:
                if o.get("symbol") != self.symbol_ccxt:
                    raise RuntimeError("dedicated live account has open orders on another symbol")
                cid = (o.get("clientOrderId") or "")
                if cid.startswith("qt"):
                    parts = cid.split("-")
                    strategy = parts[0][2:] if parts else ""
                    reduce_only = (_truthy(o.get("reduceOnly"))
                                   or _truthy((o.get("info") or {}).get("reduceOnly")))
                    if reduce_only or "STOP" in cid or "TP" in cid:
                        protection_by_strategy.setdefault(strategy, []).append(o)
                        kind = "STOP" if "STOP" in cid else "TP"
                        side = Side.BUY if str(o.get("side", "")).lower() == "buy" else Side.SELL
                        self._tracked[cid] = {"strategy": strategy, "kind": kind,
                                              "side": side, "tag": "reconciled",
                                              "filled": float(o.get("filled") or 0),
                                              "fee_seen": 0.0,
                                              "created_monotonic": time.monotonic()}
                    else:
                        log.warning("reconcile: cancelling stale entry %s", cid)
                        await self.x.cancel_order(o["id"], self.symbol_ccxt)
            positions = await self.x.fetch_positions()
            adopted: set[str] = set()
            for p in positions:
                amt = float(p.get("contracts") or 0)
                if abs(amt) > 0 and p.get("symbol") != self.symbol_ccxt:
                    raise RuntimeError("dedicated live account has a position on another symbol")
                if abs(amt) > 0:
                    candidates = [name for name, orders in protection_by_strategy.items()
                                  if any("STOP" in (o.get("clientOrderId") or "") for o in orders)]
                    if len(candidates) != 1:
                        raise RuntimeError("existing position has no unambiguous protective stop; "
                                           "refusing live startup")
                    strat = next((s for s in strategies if s.name == candidates[0]), None)
                    if strat is None:
                        raise RuntimeError("existing position belongs to disabled strategy")
                    strat.pos.side = Side.BUY if p.get("side") == "long" else Side.SELL
                    strat.pos.qty = abs(amt)
                    strat.pos.avg_price = float(p.get("entryPrice") or 0)
                    adopted.add(strat.name)
                    log.warning("reconcile: adopted protected %s position", strat.name)
            for strategy, orders in protection_by_strategy.items():
                if strategy not in adopted:
                    for order in orders:
                        log.warning("reconcile: cancelling orphan protection %s",
                                    order.get("clientOrderId"))
                        await self.x.cancel_order(order["id"], self.symbol_ccxt)
                        self._tracked.pop(order.get("clientOrderId") or "", None)
        except Exception:
            log.exception("reconcile failed — refusing to trade")
            raise

    async def account_equity(self) -> float:
        balance = await self.x.fetch_balance({"type": "future"})
        info = balance.get("info") or {}
        value = info.get("totalWalletBalance")
        if value is None:
            value = ((balance.get("USDT") or {}).get("total"))
        equity = float(value or 0)
        if equity <= 0:
            raise RuntimeError("cannot read a positive USDT futures wallet balance")
        return equity

    async def cancel_everything(self) -> None:
        await self._cancel_where(lambda m: True)

    async def cancel_entries(self) -> None:
        await self._cancel_where(lambda m: m["kind"] in ("LIMIT", "IOC"))

    async def flatten_all(self, strategies) -> None:
        # Preserve exchange-side stops until the reduce-only exits are confirmed.
        # The fill callback cancels remaining protection after each position is flat.
        await self.cancel_entries()
        for s in strategies:
            if not s.pos.is_flat():
                await self._create("MARKET", s.name, s.pos.side.opposite, s.pos.qty,
                                   reduce_only=True, tag="flatten")

    async def emergency_flatten_exchange(self) -> None:
        """Flatten exchange truth when local strategy/order state is no longer trustworthy."""
        await self.cancel_entries()
        for position in await self.x.fetch_positions([self.symbol_ccxt]):
            qty = abs(float(position.get("contracts") or 0))
            if qty <= 0:
                continue
            side = Side.SELL if position.get("side") == "long" else Side.BUY
            await self._create("MARKET", "risk", side, qty, reduce_only=True,
                               tag="state_unknown")

    async def close(self) -> None:
        await self.x.close()
