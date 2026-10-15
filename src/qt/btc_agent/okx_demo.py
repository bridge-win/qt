"""BTC/USDT cash spot execution on OKX demo, with no production fallback."""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.parse import urlsplit

import ccxt

from qt.btc_agent.broker import (
    BrokerError,
    Fill,
    Quote,
    Side,
    _mapping,
    _number,
    _OrderMissingError,
    _rows,
    _SpotRules,
)

_ORIGIN = "https://openapi.okx.com"
_SYMBOL = "BTC-USDT"
_ROUTES = frozenset({
    ("GET", "/api/v5/public/instruments"),
    ("GET", "/api/v5/market/books"),
    ("GET", "/api/v5/account/balance"),
    ("GET", "/api/v5/trade/order"),
    ("GET", "/api/v5/trade/fills"),
    ("POST", "/api/v5/trade/order"),
})
_STATUSES = {
    "live": "NEW", "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED", "canceled": "CANCELED",
}


class DemoOrderNotVisibleError(BrokerError):
    """Accepted intent is not visible yet; preserve it and reconcile later."""


class DemoFillsIncompleteError(BrokerError):
    """Order and the available fill history do not yet fully reconcile."""


class DemoFeeAccountingError(BrokerError):
    """An executed order has fees/rebates this ledger cannot safely represent."""


def _client_id(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", value):
        raise BrokerError("OKX demo client order ID must be 1-32 letters or digits")


class _DemoExchange(ccxt.okx):  # type: ignore[misc]  # ccxt ships no typed class export.
    """Require the demo header on the final signed request to the shared API host."""

    def sign(
        self, path: str, api: str = "public", method: str = "GET",
        params: dict[str, object] | None = None, headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> dict[str, object]:
        signed = _mapping(super().sign(path, api, method, params or {}, headers, body))
        # ccxt normally merges self.headers inside fetch, after our boundary check.
        # Merge here too so the exact final demo header is validated before I/O.
        signed["headers"] = {**self.headers, **_mapping(signed.get("headers") or {})}
        return signed

    def fetch(
        self, url: str, method: str = "GET", headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> object:
        parsed = urlsplit(url)
        demo = [value for name, value in (headers or {}).items() if name.lower() == "x-simulated-trading"]
        if (
            parsed.scheme != "https" or parsed.netloc != "openapi.okx.com"
            or (method, parsed.path) not in _ROUTES or parsed.fragment or demo != ["1"]
        ):
            raise BrokerError("Blocked endpoint or missing OKX demo header outside allowlist")
        return cast(object, super().fetch(url, method, headers, body))


class OKXDemoBroker(_SpotRules):
    """Only cash BTC/USDT IOC limits; acknowledgements are reconciled, never fills.

    Demo and production share a host. The final HTTP boundary requires the
    simulated-trading header, while OKX rejects real keys in that environment.
    No method retries a submission or changes environment after an error.
    """

    def __init__(self, api_key: str, api_secret: str, passphrase: str) -> None:
        values = (api_key, api_secret, passphrase)
        if any(values) and not all(values):
            raise BrokerError("All three dedicated OKX demo credentials are required")
        if any(value != value.strip() or any(ord(c) < 32 for c in value) for value in values):
            raise BrokerError("Invalid OKX demo credential format")
        self._client = _DemoExchange({
            "apiKey": api_key, "secret": api_secret, "password": passphrase,
            "enableRateLimit": True, "timeout": 15000, "maxRetriesOnFailure": 0,
            "options": {"defaultType": "spot", "maxRetriesOnFailure": 0},
        })
        # First exchange method: the header is also checked at final transport.
        self._client.set_sandbox_mode(True)
        self._client.urls["api"] = {"rest": _ORIGIN}
        self._client.session.max_redirects = 0
        self._client.session.trust_env = False
        self._client.logger = logging.Logger("qt.btc_agent.okx_demo_transport")
        self._client.logger.disabled = True
        self._filters: dict[str, dict[str, object]] = {}
        self._loaded_at = 0.0

    def _request(
        self, path: str, *, private: bool = False, method: str = "GET",
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        self._client.verbose = False
        self._client.options["maxRetriesOnFailure"] = 0
        try:
            response = _mapping(self._client.request(
                path, "private" if private else "public", method, params or {},
            ))
        except ccxt.OrderNotFound:
            raise _OrderMissingError("OKX demo order not found") from None
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"OKX demo request failed ({type(exc).__name__})") from None
        # Never trust HTTP success alone, including environment mismatch 50101.
        if response.get("code") != "0":
            raise BrokerError("OKX demo returned an unsuccessful response")
        rows = _rows(response.get("data"))
        if any("sCode" in row and row["sCode"] != "0" for row in rows):
            raise BrokerError("OKX demo returned an unsuccessful item; reconcile intent")
        return rows

    @staticmethod
    def _one(rows: list[dict[str, object]]) -> dict[str, object]:
        if len(rows) != 1:
            raise BrokerError("OKX demo expected exactly one response item")
        return rows[0]

    def quote(self) -> Quote:
        row = self._one(self._request("market/books", params={"instId": _SYMBOL, "sz": 1}))
        # Order-book API identifies the requested instrument in the query, not in data.
        bids, asks = row.get("bids"), row.get("asks")
        if (
            not isinstance(bids, list) or not bids or not isinstance(bids[0], list)
            or not bids[0] or not isinstance(asks, list) or not asks
            or not isinstance(asks[0], list) or not asks[0]
        ):
            raise BrokerError("Malformed OKX demo order book")
        bid, ask = _number(bids[0][0], positive=True), _number(asks[0][0], positive=True)
        timestamp = _number(row.get("ts"), positive=True)
        if ask < bid or timestamp != timestamp.to_integral_value():
            raise BrokerError("Crossed OKX demo quote or invalid timestamp")
        return Quote(float(bid), float(ask), int(timestamp))

    def balances(self) -> dict[str, float]:
        row = self._one(self._request("account/balance", private=True, params={"ccy": "BTC,USDT"}))
        result: dict[str, float] = {}
        for entry in _rows(row.get("details")):
            asset = entry.get("ccy")
            if asset in ("BTC", "USDT"):
                if str(asset) in result:
                    raise BrokerError("Duplicate OKX demo balance")
                result[str(asset)] = float(_number(entry.get("availBal")))
        if set(result) != {"BTC", "USDT"}:
            raise BrokerError("OKX demo account is missing BTC or USDT cash balances")
        return result

    def _load_filters(self) -> None:
        if self._filters and time.monotonic() - self._loaded_at < 3600:
            return
        row = self._one(self._request("public/instruments", params={
            "instType": "SPOT", "instId": _SYMBOL,
        }))
        if (
            row.get("instId") != _SYMBOL or row.get("instType") != "SPOT"
            or row.get("baseCcy") != "BTC" or row.get("quoteCcy") != "USDT"
            or row.get("state") != "live"
        ):
            raise BrokerError("OKX demo BTC/USDT cash spot market is unavailable")
        # OKX SPOT provides a minimum base size, not Binance-style min notional.
        tick = _number(row.get("tickSz"), positive=True)
        step = _number(row.get("lotSz"), positive=True)
        minimum = _number(row.get("minSz"), positive=True)
        maximum = _number(row.get("maxLmtSz"), positive=True)
        # maxLmtAmt is USD, not USDT. Validate its shape but do not silently
        # reinterpret it as a quote-currency limit; OKX enforces that USD cap.
        _number(row.get("maxLmtAmt"), positive=True)
        self._filters = {
            "PRICE_FILTER": {"tickSize": str(tick), "minPrice": str(tick), "maxPrice": 0},
            "LOT_SIZE": {"stepSize": str(step), "minQty": str(minimum), "maxQty": str(maximum)},
        }
        self._loaded_at = time.monotonic()

    def submit(self, client_id: str, side: Side, qty: float, limit_price: float) -> Fill:
        _client_id(client_id)
        price = self.prepare_price(limit_price, side)
        amount = self.prepare_quantity(qty, price)
        # ccxt maps duplicate IDs and definite rejections to the same InvalidOrder
        # type. Never infer a terminal rejection from that class or message text.
        ack = self._one(self._request("trade/order", private=True, method="POST", params={
            "instId": _SYMBOL, "tdMode": "cash", "side": side, "ordType": "ioc",
            "sz": format(Decimal(str(amount)), "f"),
            "px": format(Decimal(str(price)), "f"), "clOrdId": client_id,
        }))
        order_id = str(ack.get("ordId", ""))
        if ack.get("clOrdId") != client_id or ack.get("sCode") != "0" or not order_id.isdigit():
            raise BrokerError("OKX demo acknowledgement does not match persisted intent")
        fill = self.lookup(client_id)
        if fill is None:
            raise DemoOrderNotVisibleError("OKX demo acknowledgement has no visible order; reconcile later")
        if fill.exchange_id != order_id or fill.side != side or fill.filled_qty > amount:
            raise BrokerError("OKX demo fill differs from submitted order")
        bound = Decimal(str(fill.filled_qty)) * Decimal(str(price))
        if (side == "buy" and Decimal(str(fill.quote_qty)) > bound) or (
            side == "sell" and Decimal(str(fill.quote_qty)) < bound
        ):
            raise BrokerError("OKX demo execution violated submitted limit")
        return fill

    def lookup(self, client_id: str) -> Fill | None:
        _client_id(client_id)
        try:
            row = self._one(self._request("trade/order", private=True, params={
                "instId": _SYMBOL, "clOrdId": client_id,
            }))
        except _OrderMissingError:
            return None
        if (
            row.get("clOrdId") != client_id or row.get("instId") != _SYMBOL
            or row.get("instType") != "SPOT" or row.get("tdMode") != "cash"
            or row.get("ordType") != "ioc" or row.get("side") not in ("buy", "sell")
            or row.get("state") not in _STATUSES
        ):
            raise BrokerError("OKX demo order identity, type, or state is invalid")
        order_id = str(row.get("ordId", ""))
        if not order_id.isdigit():
            raise BrokerError("Missing OKX demo order ID")
        qty = _number(row.get("accFillSz"))
        total = _number(row.get("sz"), positive=True)
        _number(row.get("px"), positive=True)
        if qty > total or (row["state"] == "filled" and qty != total):
            raise BrokerError("Inconsistent OKX demo executed quantity")
        cost, fee_btc, fee_quote = Decimal(0), Decimal(0), Decimal(0)
        if qty > 0:
            fills = self._request("trade/fills", private=True, params={
                "instType": "SPOT", "instId": _SYMBOL, "ordId": order_id, "limit": 100,
            })
            seen: set[str] = set()
            executed = Decimal(0)
            for trade in fills:
                trade_id = str(trade.get("tradeId", ""))
                if (
                    str(trade.get("ordId")) != order_id or trade.get("instId") != _SYMBOL
                    or trade.get("instType") != "SPOT"
                    or ("clOrdId" in trade and trade["clOrdId"] != client_id)
                    or trade.get("side") != row["side"] or not trade_id.isdigit()
                    or trade_id in seen
                ):
                    raise BrokerError("OKX demo trade identity is invalid or duplicated")
                seen.add(trade_id)
                amount = _number(trade.get("fillSz"), positive=True)
                cost += amount * _number(trade.get("fillPx"), positive=True)
                executed += amount
                fee = self._fee(trade.get("fee"))
                if trade.get("feeCcy") == "BTC":
                    fee_btc += fee
                elif trade.get("feeCcy") == "USDT":
                    fee_quote += fee
                elif fee > 0:
                    raise DemoFeeAccountingError("Unsupported OKX demo fee currency")
            if executed != qty:
                raise DemoFillsIncompleteError("OKX demo fills incomplete; reconcile later, never resubmit")
        return Fill(
            client_id, str(row["side"]), _STATUSES[str(row["state"])], float(qty),
            float(cost), float(fee_btc), float(fee_quote), order_id,
        )

    @staticmethod
    def _fee(value: object) -> Decimal:
        # OKX charges are negative; rebates are not representable by this ledger.
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise BrokerError("Missing OKX demo fee")
        try:
            fee = Decimal(str(value))
        except InvalidOperation:
            raise BrokerError("Invalid OKX demo fee") from None
        if not fee.is_finite() or fee > 0:
            raise DemoFeeAccountingError("Invalid fee or unsupported OKX demo rebate")
        return -fee
