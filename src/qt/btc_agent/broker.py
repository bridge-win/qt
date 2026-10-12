"""BTC/USDT execution restricted to Binance Spot Testnet at the transport boundary."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Literal, cast
from urllib.parse import urlsplit

import ccxt

Side = Literal["buy", "sell"]
_SYMBOL = "BTCUSDT"
_ORIGIN = "https://testnet.binance.vision"
_ROUTES = frozenset(
    {
        ("GET", "/api/v3/exchangeInfo"),
        ("GET", "/api/v3/ticker/bookTicker"),
        ("GET", "/api/v3/time"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/order"),
        ("GET", "/api/v3/myTrades"),
        ("POST", "/api/v3/order"),
    }
)
_STATUSES = frozenset(
    {
        "NEW",
        "PENDING_NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "PENDING_CANCEL",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    }
)


class BrokerError(RuntimeError):
    """A sanitized failure; submission outcome may require reconciliation."""


class _OrderMissingError(BrokerError):
    pass


class _OrderRejectedError(BrokerError):
    pass


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp_ms: int


@dataclass(frozen=True)
class Fill:
    client_id: str
    side: str
    status: str
    filled_qty: float
    quote_qty: float
    fee_btc: float = 0.0
    fee_quote: float = 0.0
    exchange_id: str = ""


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BrokerError("Malformed testnet response")
    return cast(dict[str, object], value)


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise BrokerError("Malformed testnet response list")
    return [_mapping(row) for row in value]


def _number(value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, float, int)) or isinstance(value, bool):
        raise BrokerError("Missing numeric testnet value")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise BrokerError("Invalid numeric testnet value") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise BrokerError("Testnet value must be finite and nonnegative (positive for orders)")
    return result


def _client_id(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", value):
        raise BrokerError(
            "Client order ID must contain 1-36 letters, digits, underscores or hyphens"
        )


class _TestnetExchange(ccxt.binance):  # type: ignore[misc]  # ccxt ships no typed class export.
    """Check the final signed URL immediately before the HTTP request."""

    def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> object:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "testnet.binance.vision"
            or (method, parsed.path) not in _ROUTES
            or parsed.fragment
        ):
            raise BrokerError("Blocked endpoint outside the Binance Spot Testnet allowlist")
        return cast(object, super().fetch(url, method, headers, body))


class BinanceTestnetBroker:
    """No live mode, configurable API origin, withdrawal endpoint, or submit retries.

    Persist the client ID and limit before calling submit. A timeout must be
    reconciled using lookup, never by blindly resubmitting the order.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        if bool(api_key) != bool(api_secret):
            raise BrokerError("Both testnet API key and secret are required for private operations")
        self._client = _TestnetExchange(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "timeout": 15000,
                "maxRetriesOnFailure": 0,
                "options": {"defaultType": "spot", "maxRetriesOnFailure": 0},
            }
        )
        # Must precede every other exchange method, including market loading.
        self._client.set_sandbox_mode(True)
        self._client.urls["api"] = {
            "public": f"{_ORIGIN}/api/v3",
            "private": f"{_ORIGIN}/api/v3",
        }
        self._client.session.max_redirects = 0
        self._client.session.trust_env = False
        # ccxt's debug logger includes signed URLs and headers. Never emit them.
        self._client.logger = logging.Logger("qt.btc_agent.testnet_transport")
        self._client.logger.disabled = True
        self._filters: dict[str, dict[str, object]] = {}
        self._loaded_at = 0.0

    def _request(
        self,
        path: str,
        *,
        private: bool = False,
        method: str = "GET",
        params: dict[str, object] | None = None,
    ) -> object:
        # Defense in depth if ccxt defaults or internal configuration change.
        self._client.verbose = False
        self._client.options["maxRetriesOnFailure"] = 0
        try:
            return cast(
                object,
                self._client.request(
                    path,
                    "private" if private else "public",
                    method,
                    params or {},
                ),
            )
        except ccxt.OrderNotFound:
            raise _OrderMissingError("Testnet order not found") from None
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds) as exc:
            if isinstance(exc, ccxt.DuplicateOrderId) or "duplicate" in str(exc).lower():
                raise BrokerError(
                    "Duplicate testnet client ID; reconcile the original order"
                ) from None
            raise _OrderRejectedError("Testnet request rejected") from None
        except BrokerError:
            raise
        except Exception as exc:
            # Exception strings and chaining can contain signatures or credentials.
            raise BrokerError(f"Testnet request failed ({type(exc).__name__})") from None

    def quote(self) -> Quote:
        started_ms = time.time_ns() // 1_000_000
        response = _mapping(self._request("ticker/bookTicker", params={"symbol": _SYMBOL}))
        if response.get("symbol") != _SYMBOL:
            raise BrokerError("Unexpected testnet quote symbol")
        bid = _number(response.get("bidPrice"), positive=True)
        ask = _number(response.get("askPrice"), positive=True)
        if ask < bid:
            raise BrokerError("Crossed testnet quote")
        return Quote(float(bid), float(ask), started_ms)

    def balances(self) -> dict[str, float]:
        response = _mapping(self._request("account", private=True))
        result: dict[str, float] = {}
        for entry in _rows(response.get("balances")):
            asset = entry.get("asset")
            if asset in ("BTC", "USDT"):
                result[str(asset)] = float(_number(entry.get("free")))
        if set(result) != {"BTC", "USDT"}:
            raise BrokerError("Testnet account is missing BTC or USDT balances")
        return result

    def _load_filters(self) -> None:
        if self._filters and time.monotonic() - self._loaded_at < 3600:
            return
        response = _mapping(self._request("exchangeInfo", params={"symbol": _SYMBOL}))
        symbols = _rows(response.get("symbols"))
        if len(symbols) != 1:
            raise BrokerError("Testnet BTC/USDT market metadata is missing")
        market = symbols[0]
        if (
            market.get("symbol") != _SYMBOL
            or market.get("status") != "TRADING"
            or market.get("baseAsset") != "BTC"
            or market.get("quoteAsset") != "USDT"
            or market.get("isSpotTradingAllowed") is not True
        ):
            raise BrokerError("Testnet BTC/USDT spot market is unavailable")
        filters = {str(row.get("filterType")): row for row in _rows(market.get("filters"))}
        if not {"LOT_SIZE", "PRICE_FILTER"}.issubset(filters):
            raise BrokerError("Testnet lot or price filter is missing")
        if "MIN_NOTIONAL" not in filters and "NOTIONAL" not in filters:
            raise BrokerError("Testnet notional filter is missing")
        self._filters = filters
        self._loaded_at = time.monotonic()

    @staticmethod
    def _bound(value: Decimal, rule: dict[str, object], minimum: str, maximum: str) -> None:
        low = _number(rule.get(minimum, 0))
        high = _number(rule.get(maximum, 0))
        if value < low or (high > 0 and value > high):
            raise BrokerError(f"Order violates testnet {minimum}/{maximum} filter")

    def prepare_price(self, price: float, side: Side) -> float:
        if side not in ("buy", "sell"):
            raise BrokerError("Only buy and sell sides are supported")
        self._load_filters()
        rule = self._filters["PRICE_FILTER"]
        tick = _number(rule.get("tickSize"), positive=True)
        value = _number(price, positive=True)
        rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
        rounded = (value / tick).to_integral_value(rounding=rounding) * tick
        self._bound(rounded, rule, "minPrice", "maxPrice")
        if rounded <= 0:
            raise BrokerError("Rounded order price is zero")
        return float(rounded)

    def prepare_quantity(self, qty: float, price: float) -> float:
        self._load_filters()
        rule = self._filters["LOT_SIZE"]
        step = _number(rule.get("stepSize"), positive=True)
        value = _number(qty, positive=True)
        rounded = (value / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if rounded <= 0:
            raise BrokerError("Rounded order quantity is zero")
        self._bound(rounded, rule, "minQty", "maxQty")
        notional = rounded * _number(price, positive=True)
        for name in ("NOTIONAL", "MIN_NOTIONAL"):
            if name in self._filters:
                self._bound(notional, self._filters[name], "minNotional", "maxNotional")
        return float(rounded)

    def is_dust(self, qty: float, price: float) -> bool:
        """Whether this balance rounds below the current minimum spot sell size.

        Invalid numbers, unavailable metadata, and request failures still raise;
        only confirmed exchange minimum constraints classify inventory as dust.
        """
        self._load_filters()
        amount = _number(qty)
        mark = _number(price, positive=True)
        self._bound(mark, self._filters["PRICE_FILTER"], "minPrice", "maxPrice")
        lot = self._filters["LOT_SIZE"]
        step = _number(lot.get("stepSize"), positive=True)
        rounded = (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if rounded == 0 or rounded < _number(lot.get("minQty")):
            return True
        return any(
            rounded * mark < _number(self._filters[name].get("minNotional"))
            for name in ("NOTIONAL", "MIN_NOTIONAL")
            if name in self._filters
        )

    def submit(self, client_id: str, side: Side, qty: float, limit_price: float) -> Fill:
        _client_id(client_id)
        price = self.prepare_price(limit_price, side)
        amount = self.prepare_quantity(qty, price)
        try:
            response = self._request(
                "order",
                private=True,
                method="POST",
                params={
                    "symbol": _SYMBOL,
                    "side": side.upper(),
                    "type": "LIMIT",
                    "timeInForce": "IOC",
                    "quantity": format(Decimal(str(amount)), "f"),
                    "price": format(Decimal(str(price)), "f"),
                    "newClientOrderId": client_id,
                    "newOrderRespType": "FULL",
                },
            )
        except _OrderRejectedError:
            return Fill(client_id, side, "REJECTED", 0.0, 0.0)
        fill = self._fill(_mapping(response), client_id)
        if fill.side != side or Decimal(str(fill.filled_qty)) > Decimal(str(amount)):
            raise BrokerError("Testnet execution does not match the submitted side or quantity")
        executed_notional = Decimal(str(fill.filled_qty)) * Decimal(str(price))
        if side == "buy" and Decimal(str(fill.quote_qty)) > executed_notional:
            raise BrokerError("Testnet buy execution exceeded its submitted limit")
        if side == "sell" and Decimal(str(fill.quote_qty)) < executed_notional:
            raise BrokerError("Testnet sell execution was below its submitted limit")
        return fill

    def lookup(self, client_id: str) -> Fill | None:
        _client_id(client_id)
        try:
            response = self._request(
                "order",
                private=True,
                params={
                    "symbol": _SYMBOL,
                    "origClientOrderId": client_id,
                },
            )
        except _OrderMissingError:
            return None
        return self._fill(_mapping(response), client_id)

    def _fill(self, response: dict[str, object], client_id: str) -> Fill:
        if response.get("clientOrderId") != client_id or response.get("symbol") != _SYMBOL:
            raise BrokerError("Testnet order identity does not match the persisted intent")
        status = str(response.get("status"))
        side = str(response.get("side")).lower()
        if status not in _STATUSES or side not in ("buy", "sell"):
            raise BrokerError("Unknown testnet order status or side")
        qty = _number(response.get("executedQty"))
        cost = _number(response.get("cummulativeQuoteQty"))
        if (qty == 0) != (cost == 0) or (status == "FILLED" and qty == 0):
            raise BrokerError("Inconsistent testnet executed quantities")
        exchange_id = str(response.get("orderId", ""))
        if not exchange_id.isdigit():
            raise BrokerError("Missing testnet order ID")
        fee_btc = Decimal(0)
        fee_quote = Decimal(0)
        if qty > 0:
            fills = _rows(response.get("fills", []))
            if not fills:
                trades = self._request(
                    "myTrades",
                    private=True,
                    params={
                        "symbol": _SYMBOL,
                        "orderId": exchange_id,
                        "limit": 1000,
                    },
                )
                fills = _rows(trades)
                if any(str(row.get("orderId")) != exchange_id for row in fills):
                    raise BrokerError("Testnet fee lookup returned a different order")
            if sum((_number(row.get("qty")) for row in fills), Decimal(0)) != qty:
                raise BrokerError("Testnet fills are incomplete; reconciliation must be retried")
            for row in fills:
                commission = _number(row.get("commission"))
                asset = row.get("commissionAsset")
                if asset == "BTC":
                    fee_btc += commission
                elif asset == "USDT":
                    fee_quote += commission
                elif commission > 0:
                    raise BrokerError(
                        "Unsupported fee asset; disable testnet third-asset fee discounts"
                    )
        return Fill(
            client_id,
            side,
            status,
            float(qty),
            float(cost),
            float(fee_btc),
            float(fee_quote),
            exchange_id,
        )
