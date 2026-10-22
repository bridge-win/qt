"""Tenant-scoped trading ledger; public prices never imply live trading readiness.

Paper fills consume displayed top-of-book liquidity and charge explicit estimated
fees. They do not model queue position, settlement, redemption or a real account.
Only server-side ``trading_from_env`` / ``bind_execution`` can attach execution;
there is deliberately no API for enabling it or receiving wallet secrets.

Protocol references, checked 2026-10-21:
https://docs.polymarket.com/getting-started/python (polymarket-client 0.13.0)
https://docs.polymarket.com/v2-migration (EIP-712 Exchange domain v2, pUSD)
https://docs.polymarket.com/api-reference/geoblock (SG is close-only)
https://developers.binance.com/en/docs/products/spot/rest-api
Binance -1007/5xx outcomes are UNKNOWN: query, never blindly send again.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit

import httpx

from qt.prediction_markets.polymarket import (
    CLOB_URL,
    GAMMA_URL,
    PublicDataError,
    parse_book,
    parse_market,
    validate_slug,
)

POLYMARKET_SDK = "polymarket-client==0.13.0"
MAX_QUOTE_AGE = 15.0
MAX_DEVIATION = Decimal("0.03")
MAX_SPREAD = Decimal("0.15")
_ZERO = Decimal(0)
_OPEN = {"PREPARING", "SUBMITTING", "UNKNOWN", "NEW", "PARTIALLY_FILLED", "SETTLEMENT_PENDING"}
_TERMINAL = {"FILLED", "EXPIRED", "CANCELED", "REJECTED"}
_TOKEN = re.compile(r"[0-9]{1,78}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_:.@-]{1,128}")
_CURRENCIES = {"binance": "USDT", "polymarket": "pUSD"}
_RISK_MODES = {"active": 0, "frozen": 1, "reduce_only": 2}
_INTENT_FIELDS = frozenset(
    {
        "idempotency_key",
        "venue",
        "instrument",
        "side",
        "quantity",
        "limit_price",
        "quote_timestamp",
        "expected_probability",
    }
)
_EFFECT_NS = uuid.UUID("6ba7b814-9dad-11d1-80b4-00c04fd430c8")


class TradingError(ValueError):
    """Stable, sanitized code suitable for an API response."""


class LeaseLostError(TradingError):
    """The worker lost its execution lease before sending an order."""


class _SendBlockedError(TradingError):
    """A server-owned risk control changed before the exchange send."""


def _assert_lease_guard(guard: Callable[[], bool] | None) -> None:
    if guard is not None:
        try:
            valid = guard() is True
        except _SendBlockedError:
            raise
        except Exception:
            valid = False
        if not valid:
            raise LeaseLostError("execution_lease_lost_no_order_sent") from None


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise TradingError("invalid_number")
    if isinstance(value, str) and len(value) > 100:
        raise TradingError("number_out_of_range")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradingError("invalid_number") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise TradingError("finite_positive_number_required" if positive else "invalid_number")
    if number > Decimal("1e18") or (number and number < Decimal("1e-18")):
        raise TradingError("number_out_of_range")
    return number


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TradingError("invalid_identifier")
    return value


def _instrument(venue: str, value: Any) -> str:
    if venue not in _CURRENCIES:
        raise TradingError("unsupported_venue")
    if not isinstance(value, str):
        raise TradingError("invalid_instrument")
    if venue == "binance":
        if value != "BTCUSDT":
            raise TradingError("only_BTCUSDT_supported")
    elif not _TOKEN.fullmatch(value) or int(value) >= 2**256:
        raise TradingError("explicit_polymarket_token_required")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_send_age(intent: dict[str, Any]) -> None:
    for key in ("quote_timestamp", "server_quote_timestamp"):
        if (
            key in intent
            and not -2 <= time.time() - float(_decimal(intent[key], positive=True)) <= MAX_QUOTE_AGE
        ):
            raise TradingError("quote_expired_before_exchange_send")


def _get_json(url: str, params: dict[str, str] | None = None) -> Any:
    """No redirects, arbitrary endpoints, retrying signed writes, or response logging."""
    allowed = (
        "https://data-api.binance.vision/api/v3/ticker/bookTicker",
        "https://testnet.binance.vision/api/v3/ticker/bookTicker",
        f"{GAMMA_URL}/markets/slug/",
        f"{GAMMA_URL}/markets",
        f"{GAMMA_URL}/public-search",
        f"{CLOB_URL}/book",
        f"{CLOB_URL}/markets-by-token/",
        "https://polymarket.com/api/geoblock",
    )
    if not any(url == item or (item.endswith("/") and url.startswith(item)) for item in allowed):
        raise TradingError("blocked_public_endpoint")
    try:
        with (
            httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client,
            client.stream("GET", url, params=params) as response,
        ):
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 1_048_576:
                    raise TradingError("public_response_too_large")
        return json.loads(raw)
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
        raise TradingError("public_market_data_unavailable") from None


def _market_for_token(token: str) -> Any:
    parent = _get_json(f"{CLOB_URL}/markets-by-token/{token}")
    if not isinstance(parent, dict) or token not in [
        parent.get("primary_token_id"),
        parent.get("secondary_token_id"),
    ]:
        raise TradingError("polymarket_token_mapping_mismatch")
    condition = parent.get("condition_id")
    if not isinstance(condition, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
        raise TradingError("invalid_condition_id")
    markets = _get_json(
        f"{GAMMA_URL}/markets", {"condition_ids": condition, "limit": "2", "closed": "any"}
    )
    if not isinstance(markets, list) or len(markets) != 1 or not isinstance(markets[0], dict):
        raise TradingError("polymarket_market_metadata_unavailable")
    try:
        market = parse_market(markets[0], cast(str, markets[0].get("slug")), time.time())
    except PublicDataError:
        raise TradingError("polymarket_market_unavailable") from None
    if market.condition_id != condition or token not in {row.token_id for row in market.outcomes}:
        raise TradingError("polymarket_token_mapping_mismatch")
    return market


class ExecutionAdapter(Protocol):
    """All methods are server-owned; credentials never enter the request intent."""

    venue: str
    environment: str

    @property
    def account_id(self) -> str: ...

    def balances(self, instrument: str) -> dict[str, Any]: ...
    def submit(self, client_id: str, intent: dict[str, Any]) -> dict[str, Any]: ...
    def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any] | None: ...


class _OrderNotFoundError(TradingError):
    pass


class _OrderRejectedError(TradingError):
    pass


class _SignedRequestLogFilter(logging.Filter):
    """httpx normally logs a full GET URL, including the private signature."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                urlsplit(str(value))._replace(query="<redacted>").geturl()
                if "signature=" in str(value)
                and urlsplit(str(value)).hostname in {"api.binance.com", "testnet.binance.vision"}
                else value
                for value in record.args
            )
        return True


class BinanceSpotAdapter:
    """Production Spot only: fixed origin, LIMIT IOC, HMAC, no transport retries.

    This is independent of the existing testnet broker and cannot change its
    endpoint safeguards. Only explicitly bound dedicated accounts may execute.
    """

    venue = "binance"
    environment = "live"
    _ORIGIN = "https://api.binance.com"
    _ROUTES = frozenset(
        {
            ("GET", "time"),
            ("GET", "exchangeInfo"),
            ("GET", "account"),
            ("GET", "order"),
            ("GET", "myTrades"),
            ("POST", "order"),
        }
    )

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise TradingError("missing_BINANCE_API_KEY_or_BINANCE_API_SECRET")
        self._api_key, self._secret = api_key, api_secret
        self._offset_ms = 0
        self._clock_loaded = 0.0
        self._filters: dict[str, dict[str, Any]] = {}
        self._filters_loaded = 0.0
        self._account_id = ""
        logger = logging.getLogger("httpx")
        if not any(isinstance(item, _SignedRequestLogFilter) for item in logger.filters):
            logger.addFilter(_SignedRequestLogFilter())

    @property
    def account_id(self) -> str:
        # Different keys for the same Binance UID must not bypass tenant binding.
        if not self._account_id:
            account = self._request("account", private=True)
            uid = account.get("uid") if isinstance(account, dict) else None
            if not isinstance(uid, (int, str)) or isinstance(uid, bool) or not str(uid).isdigit():
                raise TradingError("binance_account_uid_unavailable")
            self._account_id = hashlib.sha256(
                f"binance-{self.environment}-uid:{uid}".encode()
            ).hexdigest()
        return self._account_id

    def _request(
        self,
        route: str,
        *,
        method: str = "GET",
        private: bool = False,
        params: dict[str, Any] | None = None,
    ) -> Any:
        expected_origin = {
            "live": "https://api.binance.com",
            "testnet": "https://testnet.binance.vision",
        }.get(self.environment)
        if expected_origin is None or expected_origin != self._ORIGIN:
            raise TradingError("blocked_binance_environment_origin")
        if (method, route) not in self._ROUTES:
            raise TradingError("blocked_binance_production_route")
        fields = dict(params or {})
        headers = {}
        if private:
            self._sync_clock()
            fields.update(timestamp=int(time.time() * 1000) + self._offset_ms, recvWindow=5000)
            query = urlencode(fields)
            fields["signature"] = hmac.new(
                self._secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self._api_key
        url = f"{self._ORIGIN}/api/v3/{route}"
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = client.request(method, url, params=fields, headers=headers)
            if len(response.content) > 1_048_576:
                raise TradingError("binance_response_too_large")
            payload = response.json()
            if not response.is_success:
                code = payload.get("code") if isinstance(payload, dict) else None
                if method == "GET" and route == "order" and code == -2013:
                    raise _OrderNotFoundError("binance_order_not_found")
                # Duplicate client IDs and -1007/5xx remain ambiguous; never
                # classify them as proof that the original order did not fill.
                message = payload.get("msg", "") if isinstance(payload, dict) else ""
                if (
                    400 <= response.status_code < 500
                    and code != -1007
                    and "duplicate" not in str(message).lower()
                ):
                    raise _OrderRejectedError("binance_request_rejected")
                raise TradingError("binance_execution_status_unknown")
            return payload
        except (httpx.HTTPError, ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, TradingError):
                raise
            raise TradingError("binance_transport_failed_outcome_unknown") from None

    def _sync_clock(self) -> None:
        if time.monotonic() - self._clock_loaded < 60 and self._clock_loaded:
            return
        start = time.time() * 1000
        response = self._request("time")
        end = time.time() * 1000
        server = _decimal(response.get("serverTime"), positive=True)
        if end - start > 5000:
            raise TradingError("binance_clock_probe_too_slow")
        self._offset_ms = int(server - Decimal(str((start + end) / 2)))
        if abs(self._offset_ms) > 60_000:
            raise TradingError("binance_local_clock_drift_exceeds_60_seconds")
        self._clock_loaded = time.monotonic()

    def balances(self, instrument: str) -> dict[str, Any]:
        _instrument("binance", instrument)
        account = self._request("account", private=True)
        if not isinstance(account, dict) or account.get("canTrade") is not True:
            raise TradingError("binance_spot_trading_permission_required")
        rows = account.get("balances")
        if not isinstance(rows, list):
            raise TradingError("invalid_binance_balance_response")
        balances = {
            row.get("asset"): _decimal(row.get("free"))
            for row in rows
            if isinstance(row, dict) and row.get("asset") in {"BTC", "USDT"}
        }
        if set(balances) != {"BTC", "USDT"}:
            raise TradingError("missing_binance_spot_balances")
        return {"cash": str(balances["USDT"]), "quantity": str(balances["BTC"])}

    def _prepare(self, intent: dict[str, Any]) -> tuple[Decimal, Decimal]:
        if not self._filters or time.monotonic() - self._filters_loaded > 3600:
            payload = self._request("exchangeInfo", params={"symbol": "BTCUSDT"})
            symbols = payload.get("symbols") if isinstance(payload, dict) else None
            if not isinstance(symbols, list) or len(symbols) != 1:
                raise TradingError("invalid_binance_market_metadata")
            symbol = symbols[0]
            if (
                symbol.get("symbol") != "BTCUSDT"
                or symbol.get("status") != "TRADING"
                or symbol.get("baseAsset") != "BTC"
                or symbol.get("quoteAsset") != "USDT"
                or symbol.get("isSpotTradingAllowed") is not True
            ):
                raise TradingError("binance_spot_market_unavailable")
            filters = symbol.get("filters")
            if not isinstance(filters, list):
                raise TradingError("missing_binance_filters")
            self._filters = {row["filterType"]: row for row in filters if isinstance(row, dict)}
            if not {"PRICE_FILTER", "LOT_SIZE"}.issubset(self._filters) or not (
                {"NOTIONAL", "MIN_NOTIONAL"} & self._filters.keys()
            ):
                raise TradingError("missing_binance_filters")
            self._filters_loaded = time.monotonic()
        tick, lot = self._filters["PRICE_FILTER"], self._filters["LOT_SIZE"]
        tick_size, step = (
            _decimal(tick["tickSize"], positive=True),
            _decimal(lot["stepSize"], positive=True),
        )
        rounding = ROUND_FLOOR if intent["side"] == "BUY" else ROUND_CEILING
        price = (_decimal(intent["limit_price"], positive=True) / tick_size).to_integral_value(
            rounding=rounding
        ) * tick_size
        quantity = (_decimal(intent["quantity"], positive=True) / step).to_integral_value(
            rounding=ROUND_FLOOR
        ) * step
        for amount, rule, low, high in (
            (price, tick, "minPrice", "maxPrice"),
            (quantity, lot, "minQty", "maxQty"),
        ):
            if (
                amount <= 0
                or amount < _decimal(rule.get(low, 0))
                or (_decimal(rule.get(high, 0)) > 0 and amount > _decimal(rule[high]))
            ):
                raise TradingError("binance_order_violates_price_or_lot_filter")
        for name in ("NOTIONAL", "MIN_NOTIONAL"):
            if name in self._filters:
                rule = self._filters[name]
                notional = price * quantity
                if notional < _decimal(rule.get("minNotional", 0)) or (
                    _decimal(rule.get("maxNotional", 0)) > 0
                    and notional > _decimal(rule["maxNotional"])
                ):
                    raise TradingError("binance_order_violates_notional_filter")
        return quantity, price

    def submit(self, client_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        return self.submit_guarded(client_id, intent, None)

    def submit_guarded(
        self, client_id: str, intent: dict[str, Any], guard: Callable[[], bool] | None
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", client_id):
            raise TradingError("invalid_binance_client_order_id")
        quantity, price = self._prepare(intent)
        self._sync_clock()
        _check_send_age(intent)
        _assert_lease_guard(guard)
        try:
            payload = self._request(
                "order",
                method="POST",
                private=True,
                params={
                    "symbol": "BTCUSDT",
                    "side": intent["side"],
                    "type": "LIMIT",
                    "timeInForce": "IOC",
                    "quantity": format(quantity, "f"),
                    "price": format(price, "f"),
                    "newClientOrderId": client_id,
                    "newOrderRespType": "FULL",
                },
            )
        except _OrderRejectedError:
            return {"status": "REJECTED", "filled_qty": 0, "quote_qty": 0}
        return self._fill(payload, client_id)

    def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
        try:
            payload = self._request(
                "order", private=True, params={"symbol": "BTCUSDT", "origClientOrderId": client_id}
            )
        except _OrderNotFoundError:
            return None
        return self._fill(payload, client_id)

    def _fill(self, payload: Any, client_id: str) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or payload.get("clientOrderId") != client_id
            or payload.get("symbol") != "BTCUSDT"
            or payload.get("side") not in {"BUY", "SELL"}
        ):
            raise TradingError("binance_execution_identity_mismatch")
        status = payload.get("status")
        status = "NEW" if status in {"PENDING_NEW", "PENDING_CANCEL"} else status
        status = "EXPIRED" if status == "EXPIRED_IN_MATCH" else status
        if status not in _TERMINAL | {"NEW", "PARTIALLY_FILLED"}:
            raise TradingError("binance_unrecognized_order_status")
        qty, gross = (
            _decimal(payload.get("executedQty")),
            _decimal(payload.get("cummulativeQuoteQty")),
        )
        order_id = str(payload.get("orderId", ""))
        if not order_id.isdigit():
            raise TradingError("missing_binance_exchange_order_id")
        fee_base = fee_quote = _ZERO
        if qty:
            fills = payload.get("fills")
            if not fills:
                fills = self._request(
                    "myTrades",
                    private=True,
                    params={"symbol": "BTCUSDT", "orderId": order_id, "limit": 1000},
                )
            if not isinstance(fills, list) or not all(isinstance(row, dict) for row in fills):
                raise TradingError("invalid_binance_fills")
            if any("orderId" in row and str(row["orderId"]) != order_id for row in fills):
                raise TradingError("binance_trade_identity_mismatch")
            if sum((_decimal(row.get("qty")) for row in fills), _ZERO) != qty:
                raise TradingError("binance_trade_pagination_or_incomplete_fees_reconcile_required")
            for row in fills:
                commission = _decimal(row.get("commission"))
                if row.get("commissionAsset") == "BTC":
                    fee_base += commission
                elif row.get("commissionAsset") == "USDT":
                    fee_quote += commission
                elif commission:
                    raise TradingError("binance_third_asset_fee_reconcile_required")
        return {
            "status": status,
            "side": payload["side"],
            "filled_qty": str(qty),
            "quote_qty": str(gross),
            "fee_btc": str(fee_base),
            "fee_quote": str(fee_quote),
            "exchange_id": order_id,
        }


class BinanceTestnetAdapter(BinanceSpotAdapter):
    """The same bounded Spot protocol, pinned to the official testnet origin.

    This transport has no ccxt dependency and accepts no endpoint override.
    Testnet uses virtual balances and is not production trading acceptance.
    """

    environment = "testnet"
    _ORIGIN = "https://testnet.binance.vision"


class PolymarketAdapter:
    """Official unified SDK, existing wallet only, no approvals/funding/withdrawals.

    An interrupted POST without an order ID stays UNKNOWN and requires operator
    account investigation. We never create a second signed order to recover it.
    Settlement accounting uses confirmed trades and actual balance deltas from a
    dedicated wallet. Concurrent manual activity causes fail-closed reconciliation.
    """

    venue = "polymarket"
    environment = "live"

    def __init__(
        self,
        private_key: str,
        wallet: str,
        key: str,
        secret: str,
        passphrase: str,
        *,
        user_country: str,
    ) -> None:
        if not re.fullmatch(r"[A-Z]{2}", user_country):
            raise TradingError("invalid_POLYMARKET_USER_COUNTRY")
        try:
            if version("polymarket-client") != "0.13.0":
                raise TradingError("polymarket_SDK_version_mismatch")
            sdk = importlib.import_module("polymarket")
        except (ImportError, PackageNotFoundError):
            raise TradingError("missing_optional_dependency_polymarket-client==0.13.0") from None
        self.account_id = hashlib.sha256(wallet.lower().encode()).hexdigest()
        self.user_country = user_country
        self._private_key, self._wallet = private_key, wallet
        self._creds = sdk.ApiKeyCreds(key=key, secret=secret, passphrase=passphrase)
        self._sdk = sdk
        self._client: Any = None

    def _connected(self) -> Any:
        if self._client is None:
            logger = logging.Logger("qt.agent_hub.polymarket")
            logger.disabled = True
            self._client = self._sdk.SecureClient.create(
                private_key=self._private_key,
                wallet=self._wallet,
                credentials=self._creds,
                logger=logger,
            )
        return self._client

    def _eligibility(self) -> None:
        # The server's egress location is insufficient evidence of user eligibility.
        # Conservatively reject all restricted and close-only countries for this
        # opening-position integration, including SG. No proxy/region fallback.
        restricted = {
            "AU",
            "BY",
            "BE",
            "BI",
            "BR",
            "CF",
            "CD",
            "ET",
            "FR",
            "DE",
            "IQ",
            "IT",
            "LB",
            "LY",
            "MM",
            "NZ",
            "NI",
            "KP",
            "PL",
            "RU",
            "SG",
            "SO",
            "SK",
            "SS",
            "SD",
            "TW",
            "TH",
            "GB",
            "US",
            "UM",
            "VE",
            "YE",
            "ZW",
            "IR",
            "SY",
            "CU",
            "CA",
            "UA",
            "IE",
            "JP",
            "MT",
            "NL",
            "KR",
        }
        if self.user_country in restricted:
            raise TradingError("polymarket_user_location_restricted_or_close_only")
        geo = _get_json("https://polymarket.com/api/geoblock")
        if not isinstance(geo, dict) or geo.get("blocked") is not False:
            raise TradingError("polymarket_geoblock_or_unverified")
        if geo.get("country") != self.user_country:
            raise TradingError("polymarket_user_and_execution_country_must_match")

    def balances(self, instrument: str) -> dict[str, Any]:
        self._eligibility()
        client = self._connected()
        cash = client.get_balance_allowance(asset_type="COLLATERAL")
        market = _market_for_token(instrument)
        shares = client.get_balance_allowance(
            asset_type="CONDITIONAL-V2" if market.version == "v2" else "CONDITIONAL",
            asset_id=instrument,
        )
        return {
            "cash": str(Decimal(cash.balance) / 1_000_000),
            "quantity": str(Decimal(shares.balance) / 1_000_000),
        }

    def submit(self, client_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        return self.submit_guarded(client_id, intent, None)

    def submit_guarded(
        self, client_id: str, intent: dict[str, Any], guard: Callable[[], bool] | None
    ) -> dict[str, Any]:
        self._eligibility()
        client = self._connected()
        signed = client.create_limit_order(
            asset_id=intent["instrument"],
            side=intent["side"],
            price=intent["limit_price"],
            size=intent["quantity"],
        )
        _check_send_age(intent)
        _assert_lease_guard(guard)
        # Order type is a transport instruction, outside the EIP-712 signed order.
        # FAK is IOC: never leave an unmanaged resting GTC order behind.
        response = client.post_order(replace(signed, order_type="FAK"))
        if not response.ok:
            return {
                "status": "REJECTED",
                "filled_qty": 0,
                "quote_qty": 0,
                "fee_quote": 0,
                "fee_btc": 0,
            }
        return {
            "status": "SETTLEMENT_PENDING",
            "exchange_id": response.order_id,
            "filled_qty": 0,
            "quote_qty": 0,
            "fee_quote": 0,
            "fee_btc": 0,
        }

    def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
        external = record.get("exchange_id")
        if not external:
            return None
        client = self._connected()
        order = client.get_order(order_id=external)
        if order.asset_id != record["instrument"] or order.side != record["side"]:
            raise TradingError("polymarket_reconcile_identity_mismatch")
        if order.status.upper() in {"LIVE", "OPEN", "DELAYED"}:
            return None
        matched = _decimal(order.size_matched)
        if matched == 0 and order.status.upper() in {"CANCELED", "CANCELLED", "UNMATCHED"}:
            return {
                "status": "EXPIRED",
                "filled_qty": 0,
                "quote_qty": 0,
                "fee_quote": 0,
                "fee_btc": 0,
                "exchange_id": external,
            }
        if matched == 0:
            return None
        trades = []
        for trade_id in order.associate_trades:
            page = client.list_account_trades(id=trade_id).first_page()
            if page.next_cursor:
                return None
            trades.extend(page.items)
        selected = {row.id: row for row in trades if row.taker_order_id == external}
        if (
            not selected
            or any(row.status != "CONFIRMED" for row in selected.values())
            or sum((_decimal(row.size) for row in selected.values()), _ZERO) != matched
        ):
            return None
        gross = sum((_decimal(row.size) * _decimal(row.price) for row in selected.values()), _ZERO)
        before = record["pre_balances"]
        after = self.balances(record["instrument"])
        sign = Decimal(1) if record["side"] == "BUY" else Decimal(-1)
        net_quantity = sign * (_decimal(after["quantity"]) - _decimal(before["quantity"]))
        net_cash = sign * (_decimal(before["cash"]) - _decimal(after["cash"]))
        fee_base = sign * (matched - net_quantity)
        fee_quote = sign * (net_cash - gross)
        if fee_base < 0 or fee_quote < 0:
            raise TradingError("polymarket_external_balance_drift")
        return {
            "status": "FILLED" if matched == _decimal(record["quantity"]) else "EXPIRED",
            "filled_qty": str(matched),
            "quote_qty": str(gross),
            "fee_btc": str(fee_base),
            "fee_quote": str(fee_quote),
            "exchange_id": external,
        }


class TradingService:
    def __init__(
        self,
        path: Path,
        *,
        mode: str = "paper",
        max_order_quote: float = 25,
        max_position_quote: float = 100,
        max_daily_quote: float = 100,
        frozen_allows_protective_exit: bool = False,
    ) -> None:
        if mode not in {"paper", "testnet", "live"}:
            raise TradingError("unsupported_trading_mode")
        self.path, self.mode = Path(path), mode
        self.max_order_quote = _decimal(max_order_quote, positive=True)
        self.max_position_quote = _decimal(max_position_quote, positive=True)
        self.max_daily_quote = _decimal(max_daily_quote, positive=True)
        self.frozen_allows_protective_exit = bool(frozen_allows_protective_exit)
        self._adapters: dict[tuple[str, str], ExecutionAdapter] = {}
        self._readiness: dict[tuple[str, str], list[str]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS hub_trade_accounts(
                    tenant TEXT, mode TEXT, venue TEXT, cash TEXT NOT NULL,
                    PRIMARY KEY(tenant,mode,venue));
                CREATE TABLE IF NOT EXISTS hub_trade_positions(
                    tenant TEXT, mode TEXT, venue TEXT, instrument TEXT, quantity TEXT,
                    cost TEXT, realized TEXT, PRIMARY KEY(tenant,mode,venue,instrument));
                CREATE TABLE IF NOT EXISTS hub_trade_orders(
                    tenant TEXT, mode TEXT, idem TEXT, fingerprint TEXT, status TEXT,
                    created REAL, day TEXT, reserved TEXT, data TEXT,
                    PRIMARY KEY(tenant,mode,idem));
                CREATE TABLE IF NOT EXISTS hub_trade_kill(tenant TEXT PRIMARY KEY, enabled INTEGER);
                CREATE TABLE IF NOT EXISTS hub_trade_bindings(
                    tenant TEXT, mode TEXT, venue TEXT, account TEXT,
                    PRIMARY KEY(tenant,mode,venue), UNIQUE(mode,venue,account));
                CREATE TABLE IF NOT EXISTS hub_execution_intents(
                    tenant TEXT, mode TEXT, decision_id TEXT, effect_id TEXT NOT NULL,
                    intent_json TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(tenant,mode,decision_id));
                CREATE TABLE IF NOT EXISTS hub_effects(
                    effect_id TEXT PRIMARY KEY, tenant TEXT, mode TEXT, decision_id TEXT,
                    tool TEXT NOT NULL, args_hash TEXT NOT NULL, status TEXT NOT NULL,
                    external_id TEXT, result_json TEXT, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL);
            """)
        if new:
            self.path.chmod(0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def bind_execution(
        self,
        tenant_id: str,
        venue: str,
        adapter: ExecutionAdapter,
        *,
        acknowledge: bool = False,
        funding_quote: float = 100,
    ) -> None:
        """Server startup only. Allocate a budget in a dedicated venue account.

        A fresh live ledger starts with no positions; existing wallet inventory is
        not allocated to the agent. Never expose this method as an HTTP endpoint.
        """
        tenant = _identifier(tenant_id)
        if (
            acknowledge is not True
            or self.mode == "paper"
            or venue != adapter.venue
            or adapter.environment != self.mode
            or venue not in _CURRENCIES
        ):
            raise TradingError("explicit_matching_execution_configuration_required")
        funding = min(_decimal(funding_quote, positive=True), self.max_daily_quote)
        account = _identifier(adapter.account_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT account FROM hub_trade_bindings WHERE tenant=? AND mode=? AND venue=?",
                (tenant, self.mode, venue),
            ).fetchone()
            if old and old[0] != account:
                raise TradingError("execution_account_cannot_change_with_existing_ledger")
            try:
                db.execute(
                    "INSERT OR IGNORE INTO hub_trade_bindings VALUES(?,?,?,?)",
                    (tenant, self.mode, venue, account),
                )
                owner = db.execute(
                    "SELECT tenant FROM hub_trade_bindings WHERE mode=? AND venue=? AND account=?",
                    (self.mode, venue, account),
                ).fetchone()
                if not owner or owner[0] != tenant:
                    raise TradingError("execution_account_already_bound_to_another_tenant")
            except sqlite3.IntegrityError:
                raise TradingError("execution_account_already_bound_to_another_tenant") from None
            db.execute(
                "INSERT OR IGNORE INTO hub_trade_accounts VALUES(?,?,?,?)",
                (tenant, self.mode, venue, str(funding)),
            )
        self._adapters[(tenant, venue)] = adapter
        self._readiness.pop((tenant, venue), None)

    def quote(self, venue: str, instrument: str) -> dict[str, Any]:
        if venue == "binance":
            _instrument(venue, instrument)
            observed = time.time()
            source = (
                "https://testnet.binance.vision/api/v3/ticker/bookTicker"
                if self.mode == "testnet"
                else "https://data-api.binance.vision/api/v3/ticker/bookTicker"
            )
            body = _get_json(source, {"symbol": instrument})
            if not isinstance(body, dict) or body.get("symbol") != instrument:
                raise TradingError("quote_identity_mismatch")
            bid, ask = (
                _decimal(body.get("bidPrice"), positive=True),
                _decimal(body.get("askPrice"), positive=True),
            )
            if bid > ask:
                raise TradingError("crossed_orderbook")
            return {
                "venue": venue,
                "instrument": instrument,
                "bid": float(bid),
                "ask": float(ask),
                "bid_size": float(_decimal(body.get("bidQty"))),
                "ask_size": float(_decimal(body.get("askQty"))),
                "timestamp": observed,
                "timestamp_kind": "local_request_started_no_exchange_timestamp",
                "read_at": time.time(),
                "source": source,
                "environment": "testnet-public" if self.mode == "testnet" else "production-public",
                "ready": True,
                "fee_model": "paper_estimate_10bps_quote",
                "fee_rate": 0.001,
            }
        if venue != "polymarket":
            raise TradingError("unsupported_venue")
        if _TOKEN.fullmatch(instrument):
            _instrument(venue, instrument)
            market = _market_for_token(instrument)
            outcome = next(row for row in market.outcomes if row.token_id == instrument)
            result = self._poly_book(
                instrument, market.condition_id, ready=market.active, outcome=outcome.name
            )
            result.update(slug=market.slug, question=market.question, version=market.version)
            return result
        try:
            slug = validate_slug(instrument)
            market = parse_market(_get_json(f"{GAMMA_URL}/markets/slug/{slug}"), slug, time.time())
            outcomes = [
                self._poly_book(
                    row.token_id, market.condition_id, ready=market.active, outcome=row.name
                )
                for row in market.outcomes
            ]
        except PublicDataError:
            raise TradingError("polymarket_market_unavailable") from None
        return {
            "venue": venue,
            "instrument": slug,
            "slug": slug,
            "question": market.question,
            "condition_id": market.condition_id,
            "version": market.version,
            "timestamp": min(row["timestamp"] for row in outcomes),
            "read_at": time.time(),
            "source": f"{GAMMA_URL}/markets/slug/{slug}",
            "outcomes": outcomes,
            "ready": all(row["ready"] for row in outcomes),
            "environment": "production-public",
        }

    def discover_markets(self, query: str = "bitcoin", limit: int = 10) -> list[dict[str, Any]]:
        """Bounded public discovery; explicitly choose a returned outcome token."""
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 120
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 20
        ):
            raise TradingError("invalid_market_search")
        body = _get_json(
            f"{GAMMA_URL}/public-search",
            {
                "q": query.strip(),
                "limit_per_type": str(limit),
                "events_status": "active",
                "keep_closed_markets": "0",
                "search_profiles": "false",
                "search_tags": "false",
            },
        )
        if not isinstance(body, dict) or (
            body.get("events") is not None and not isinstance(body["events"], list)
        ):
            raise TradingError("invalid_market_search_response")
        found: dict[str, dict[str, Any]] = {}
        for event in (body.get("events") or [])[:40]:
            if not isinstance(event, dict) or not isinstance(event.get("markets", []), list):
                continue
            for raw in event.get("markets", [])[:100]:
                if not isinstance(raw, dict):
                    continue
                try:
                    market = parse_market(raw, cast(str, raw.get("slug")), time.time())
                except PublicDataError:
                    continue
                if not market.active:
                    continue
                found[market.slug] = {
                    "slug": market.slug,
                    "question": market.question,
                    "condition_id": market.condition_id,
                    "version": market.version,
                    "outcomes": [
                        {"name": row.name, "outcome": row.name, "token_id": row.token_id}
                        for row in market.outcomes
                    ],
                    "read_at": time.time(),
                    "source": f"{GAMMA_URL}/public-search",
                    "prices_loaded": False,
                }
                if len(found) == limit:
                    return list(found.values())
        return list(found.values())

    def _poly_book(
        self, token: str, condition: str, *, ready: bool, outcome: str
    ) -> dict[str, Any]:
        body = _get_json(f"{CLOB_URL}/book", {"token_id": token})
        # Reuse the strict identity, crossed-book and source-time checks.
        from qt.prediction_markets.polymarket import OutcomeQuote, PredictionSnapshot

        snapshot = PredictionSnapshot(
            slug="token",
            question="token",
            condition_id=condition,
            version="v1",
            read_at=time.time(),
            active=ready,
            outcomes=[],
        )
        try:
            parsed = parse_book(
                body, snapshot, OutcomeQuote(name=outcome or "outcome", token_id=token), time.time()
            )
        except PublicDataError:
            raise TradingError("polymarket_book_unavailable") from None

        def size(field: str, price: float | None) -> float:
            return (
                float(
                    sum(
                        (
                            _decimal(row["size"])
                            for row in body[field]
                            if _decimal(row["price"]) == Decimal(str(price))
                        ),
                        _ZERO,
                    )
                )
                if price is not None
                else 0.0
            )

        return {
            "venue": "polymarket",
            "instrument": token,
            "token_id": token,
            "outcome": outcome,
            "condition_id": condition,
            "bid": parsed.bid,
            "ask": parsed.ask,
            "bid_size": size("bids", parsed.bid),
            "ask_size": size("asks", parsed.ask),
            "timestamp": parsed.timestamp,
            "read_at": time.time(),
            "timestamp_kind": "exchange",
            "ready": ready and parsed.ready,
            "source": f"{CLOB_URL}/book",
            "environment": "production-public",
            "reasons": parsed.reasons,
            "fee_model": "paper_conservative_estimate_shares_rate_price_1_minus_price",
            "fee_rate": 0.07,
        }

    def _intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(intent, dict):
            raise TradingError("invalid_order_intent")
        if set(intent) - _INTENT_FIELDS:
            raise TradingError("unsupported_intent_fields")
        venue = intent.get("venue")
        if not isinstance(venue, str):
            raise TradingError("unsupported_venue")
        result: dict[str, Any] = {
            "idempotency_key": _identifier(intent.get("idempotency_key")),
            "venue": venue,
            "instrument": _instrument(venue, intent.get("instrument")),
            "side": intent.get("side"),
            "quantity": str(_decimal(intent.get("quantity"), positive=True)),
            "limit_price": str(_decimal(intent.get("limit_price"), positive=True)),
            "quote_timestamp": float(_decimal(intent.get("quote_timestamp"), positive=True)),
        }
        if not isinstance(result["side"], str) or result["side"] not in {"BUY", "SELL"}:
            raise TradingError("unsupported_side")
        if venue == "polymarket" and Decimal(result["limit_price"]) >= 1:
            raise TradingError("polymarket_price_must_be_between_zero_and_one")
        if "expected_probability" in intent:
            probability = _decimal(intent["expected_probability"])
            if probability > 1:
                raise TradingError("invalid_probability")
            result["expected_probability"] = str(probability)
        return result

    def _read_order(self, row: sqlite3.Row) -> dict[str, Any]:
        """Verify the immutable send intent, including pre-upgrade ledger rows.

        Recovery reads the original decision instead of manufacturing a new
        intent with a fresh quote. An integrity failure must never look like a
        missing order: the caller must not send again under that key.
        """
        try:
            record = json.loads(row["data"])
            normalized = self._intent({k: v for k, v in record.items() if k in _INTENT_FIELDS})
            fingerprint = hashlib.sha256(_json(normalized).encode()).hexdigest()
            if (
                fingerprint != row["fingerprint"]
                or record.get("intent_sha256", fingerprint) != fingerprint
                or record.get("execution_intent", normalized) != normalized
            ):
                raise TradingError("stored_execution_intent_integrity_error")
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise TradingError("stored_execution_intent_integrity_error") from None
        record["execution_intent"] = normalized
        record["intent_sha256"] = fingerprint
        record["risk_effect"] = "reduce_only" if normalized["side"] == "SELL" else "increase"
        return cast(dict[str, Any], record)

    def lookup_order(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """Recover this tenant/mode's persisted result without external calls.

        Includes the immutable execution intent and its SHA-256. PREPARING,
        SUBMITTING and UNKNOWN are recoverable states, never permission to send
        again. Call ``reconcile`` to query those states, then read this method.
        ``submit`` still rejects an explicitly different intent under this key.
        """
        tenant, key = _identifier(tenant_id), _identifier(idempotency_key)
        with self._db() as db:
            row = db.execute(
                "SELECT fingerprint,data FROM hub_trade_orders WHERE tenant=? AND mode=? AND idem=?",
                (tenant, self.mode, key),
            ).fetchone()
            return self._read_order(row) if row else None

    @staticmethod
    def effect_id(decision_id: str, step: str = "execute", seq: int = 0) -> str:
        return str(uuid.uuid5(_EFFECT_NS, f"{decision_id}:{step}:{seq}"))

    def get_execution_intent(self, tenant_id: str, decision_id: str) -> dict[str, Any] | None:
        tenant, key = _identifier(tenant_id), _identifier(decision_id)
        with self._db() as db:
            row = db.execute(
                "SELECT intent_json,fingerprint,effect_id FROM hub_execution_intents "
                "WHERE tenant=? AND mode=? AND decision_id=?",
                (tenant, self.mode, key),
            ).fetchone()
        if row is None:
            return None
        stored = json.loads(row["intent_json"])
        normalized = self._intent({k: v for k, v in stored.items() if k in _INTENT_FIELDS})
        fingerprint = hashlib.sha256(_json(normalized).encode()).hexdigest()
        if fingerprint != row["fingerprint"]:
            raise TradingError("stored_execution_intent_integrity_error")
        return {**normalized, "effect_id": row["effect_id"]}

    def persist_execution_intent(
        self, tenant_id: str, decision_id: str, intent: dict[str, Any]
    ) -> dict[str, Any]:
        """First write wins. Retries return the original quote and quantity."""
        existing = self.get_execution_intent(tenant_id, decision_id)
        if existing is not None:
            return existing
        tenant, key = _identifier(tenant_id), _identifier(decision_id)
        normalized = self._intent(intent)
        fingerprint = hashlib.sha256(_json(normalized).encode()).hexdigest()
        effect = self.effect_id(key)
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT intent_json,fingerprint,effect_id FROM hub_execution_intents "
                "WHERE tenant=? AND mode=? AND decision_id=?",
                (tenant, self.mode, key),
            ).fetchone()
            if row is not None:
                stored = json.loads(row["intent_json"])
                return {**self._intent({k: v for k, v in stored.items() if k in _INTENT_FIELDS}), "effect_id": row["effect_id"]}
            db.execute(
                "INSERT INTO hub_execution_intents VALUES(?,?,?,?,?,?,?)",
                (tenant, self.mode, key, effect, _json(normalized), fingerprint, now),
            )
            db.execute(
                "INSERT INTO hub_effects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    effect,
                    tenant,
                    self.mode,
                    key,
                    "orders.submit",
                    fingerprint,
                    "pending",
                    None,
                    None,
                    now,
                    now,
                ),
            )
        return {**normalized, "effect_id": effect}

    def record_effect(
        self,
        tenant_id: str,
        decision_id: str,
        *,
        status: str,
        external_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        tenant, key = _identifier(tenant_id), _identifier(decision_id)
        effect = self.effect_id(key)
        with self._db() as db:
            db.execute(
                "UPDATE hub_effects SET status=?, external_id=?, result_json=?, updated_at=? "
                "WHERE effect_id=? AND tenant=? AND mode=?",
                (
                    status,
                    external_id,
                    _json(result) if result is not None else None,
                    time.time(),
                    effect,
                    tenant,
                    self.mode,
                ),
            )

    def get_effect(self, tenant_id: str, decision_id: str) -> dict[str, Any] | None:
        tenant, key = _identifier(tenant_id), _identifier(decision_id)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM hub_effects WHERE tenant=? AND mode=? AND decision_id=?",
                (tenant, self.mode, key),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("result_json"):
            item["result"] = json.loads(item.pop("result_json"))
        return item

    def get_order(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        """Read an opaque public order ID inside the same tenant/mode boundary."""
        tenant = _identifier(tenant_id)
        if not isinstance(order_id, str) or not re.fullmatch(r"qth_[0-9a-f]{32}", order_id):
            raise TradingError("invalid_order_id")
        with self._db() as db:
            row = db.execute(
                "SELECT fingerprint,data FROM hub_trade_orders "
                "WHERE tenant=? AND mode=? AND json_extract(data,'$.order_id')=?",
                (tenant, self.mode, order_id),
            ).fetchone()
            return self._read_order(row) if row else None

    def _existing(
        self, db: sqlite3.Connection, tenant: str, intent: dict[str, Any], fingerprint: str
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT fingerprint,data FROM hub_trade_orders WHERE tenant=? AND mode=? AND idem=?",
            (tenant, self.mode, intent["idempotency_key"]),
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise TradingError("idempotency_key_conflict")
            return self._read_order(row)
        return None

    def submit(
        self,
        tenant_id: str,
        intent: dict[str, Any],
        *,
        lease_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        tenant = _identifier(tenant_id)
        _assert_lease_guard(lease_guard)
        normalized = self._intent(intent)
        fingerprint = hashlib.sha256(_json(normalized).encode()).hexdigest()
        with self._db() as db:
            old = self._existing(db, tenant, normalized, fingerprint)
        if old:
            return old
        adapter = self._adapters.get((tenant, normalized["venue"]))
        if self.mode != "paper" and adapter is None:
            raise TradingError("execution_not_configured")
        now = time.time()
        if not -2 <= now - normalized["quote_timestamp"] <= MAX_QUOTE_AGE:
            raise TradingError("stale_or_future_client_quote")
        quote = self.quote(normalized["venue"], normalized["instrument"])
        self._check_quote(quote, normalized, time.time())
        try:
            pre_balances = adapter.balances(normalized["instrument"]) if adapter else None
            if pre_balances is not None:
                pre_balances = {
                    "cash": str(_decimal(pre_balances["cash"])),
                    "quantity": str(_decimal(pre_balances["quantity"])),
                }
        except TradingError:
            raise
        except Exception:
            raise TradingError("execution_account_preflight_unavailable") from None
        _assert_lease_guard(lease_guard)
        now = time.time()
        self._check_quote(quote, normalized, now)
        quantity, price = Decimal(normalized["quantity"]), Decimal(normalized["limit_price"])
        reserved = quantity * price + self._fee(normalized["venue"], quantity, price)
        record = {
            **normalized,
            "execution_intent": normalized,
            "intent_sha256": fingerprint,
            "risk_effect": "reduce_only" if normalized["side"] == "SELL" else "increase",
            "mode": self.mode,
            "created_at": now,
            "updated_at": now,
            "order_id": "qth_"
            + hashlib.sha256(
                f"{tenant}\0{self.mode}\0{normalized['idempotency_key']}".encode()
            ).hexdigest()[:32],
            "status": "PREPARING",
            "filled_qty": "0",
            "quote_qty": "0",
            "fee_quote": "0",
            "fee_base": "0",
            "exchange_id": "",
            "quote": quote,
            "fee_model": quote.get("fee_model") if self.mode == "paper" else "venue_reconciled",
            "pre_balances": pre_balances,
        }
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._existing(db, tenant, normalized, fingerprint)
            if old:
                return old
            self._risk(db, tenant, normalized, reserved, pre_balances, now)
            _assert_lease_guard(lease_guard)
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            db.execute(
                "INSERT INTO hub_trade_orders VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    self.mode,
                    normalized["idempotency_key"],
                    fingerprint,
                    record["status"],
                    now,
                    day,
                    str(reserved),
                    _json(record),
                ),
            )
            if self.mode == "paper":
                execution = Decimal(str(quote["ask" if normalized["side"] == "BUY" else "bid"]))
                crossed = execution <= price if normalized["side"] == "BUY" else execution >= price
                available = _decimal(
                    quote["ask_size" if normalized["side"] == "BUY" else "bid_size"]
                )
                filled = min(quantity, available) if crossed else _ZERO
                result = {
                    "status": "FILLED" if filled == quantity else "EXPIRED",
                    "filled_qty": str(filled),
                    "quote_qty": str(filled * execution),
                    "fee_quote": str(self._fee(normalized["venue"], filled, execution)),
                    "fee_btc": "0",
                    "exchange_id": "paper-" + record["order_id"],
                }
                return self._finish(db, tenant, record, result)
        # Durable reservation precedes the only send. A process crash leaves a
        # PREPARING/SUBMITTING record; reconciliation never resubmits it.
        assert adapter is not None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._check_risk_mode(db, tenant, normalized["side"])
            except TradingError as exc:
                record["rejection_reason"] = str(exc)
                return self._finish(
                    db, tenant, record, {"status": "REJECTED", "filled_qty": 0, "quote_qty": 0}
                )
            record["status"] = "SUBMITTING"
            self._save(db, tenant, record)

        def send_guard() -> bool:
            _assert_lease_guard(lease_guard)
            try:
                with self._db() as db:
                    self._check_risk_mode(db, tenant, normalized["side"])
            except TradingError as exc:
                raise _SendBlockedError(str(exc)) from None
            return True

        try:
            _assert_lease_guard(send_guard)
            execution_intent = {**normalized, "server_quote_timestamp": quote["timestamp"]}
            guarded_submit = getattr(adapter, "submit_guarded", None)
            result = (
                guarded_submit(record["order_id"], execution_intent, send_guard)
                if callable(guarded_submit)
                else adapter.submit(record["order_id"], execution_intent)
            )
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                return self._finish(db, tenant, record, result)
        except (LeaseLostError, _SendBlockedError) as exc:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                saved = db.execute(
                    "SELECT data FROM hub_trade_orders WHERE tenant=? AND mode=? AND idem=?",
                    (tenant, self.mode, normalized["idempotency_key"]),
                ).fetchone()
                record = json.loads(saved[0])
                if record.get("accounted") or record["status"] in _TERMINAL:
                    return cast(dict[str, Any], record)
                record["rejection_reason"] = str(exc)
                return self._finish(
                    db, tenant, record, {"status": "REJECTED", "filled_qty": 0, "quote_qty": 0}
                )
        except Exception:
            # Never persist str(exc): SDK errors can contain auth headers/signatures.
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                saved = db.execute(
                    "SELECT data FROM hub_trade_orders WHERE tenant=? AND mode=? AND idem=?",
                    (tenant, self.mode, normalized["idempotency_key"]),
                ).fetchone()
                record = json.loads(saved[0])
                if record.get("accounted") or record["status"] in _TERMINAL:
                    return cast(dict[str, Any], record)
                record["status"], record["error"] = (
                    "UNKNOWN",
                    "execution_outcome_unknown_reconcile_required",
                )
                self._save(db, tenant, record)
            return cast(dict[str, Any], record)

    @staticmethod
    def _fee(venue: str, quantity: Decimal, price: Decimal) -> Decimal:
        return (
            quantity * price * Decimal("0.001")
            if venue == "binance"
            else quantity * Decimal("0.07") * price * (1 - price)
        )

    @staticmethod
    def _check_quote(quote: dict[str, Any], intent: dict[str, Any], now: float) -> None:
        if quote.get("venue") != intent["venue"] or quote.get("instrument") != intent["instrument"]:
            raise TradingError("quote_identity_mismatch")
        if quote.get("ready") is not True:
            raise TradingError("market_not_ready")
        stamp = float(_decimal(quote.get("timestamp"), positive=True))
        if not -2 <= now - stamp <= MAX_QUOTE_AGE:
            raise TradingError("stale_or_future_market_quote")
        bid, ask = (
            _decimal(quote.get("bid"), positive=True),
            _decimal(quote.get("ask"), positive=True),
        )
        if bid > ask or (ask - bid) / ((ask + bid) / 2) > MAX_SPREAD:
            raise TradingError("spread_limit_exceeded")
        execution = ask if intent["side"] == "BUY" else bid
        if abs(Decimal(intent["limit_price"]) - execution) / execution > MAX_DEVIATION:
            raise TradingError("price_deviation_limit_exceeded")

    def _risk(
        self,
        db: sqlite3.Connection,
        tenant: str,
        intent: dict[str, Any],
        reserved: Decimal,
        remote: dict[str, Any] | None,
        now: float,
    ) -> None:
        self._check_risk_mode(db, tenant, intent["side"])
        scope = (tenant, self.mode)
        rows = db.execute(
            "SELECT status,reserved,day,data FROM hub_trade_orders WHERE tenant=? AND mode=?", scope
        ).fetchall()
        if any(row["status"] in _OPEN for row in rows):
            raise TradingError("unresolved_order_reconcile_required")
        if reserved > self.max_order_quote:
            raise TradingError("max_order_quote_exceeded_including_fee_reserve")
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        # SELL is always a bounded spot inventory reduction. It does not spend
        # the new-risk budget, nor refund an earlier BUY's consumption of it.
        used = sum(
            (
                Decimal(row["reserved"])
                for row in rows
                if row["day"] == day and json.loads(row["data"])["side"] == "BUY"
            ),
            _ZERO,
        )
        if intent["side"] == "BUY" and used + reserved > self.max_daily_quote:
            raise TradingError("max_daily_quote_exceeded")
        venue = intent["venue"]
        if self.mode == "paper":
            db.execute(
                "INSERT OR IGNORE INTO hub_trade_accounts VALUES(?,?,?,?)",
                (tenant, self.mode, venue, "1000"),
            )
        account = db.execute(
            "SELECT cash FROM hub_trade_accounts WHERE tenant=? AND mode=? AND venue=?",
            (*scope, venue),
        ).fetchone()
        if not account:
            raise TradingError("execution_budget_not_allocated")
        position = db.execute(
            "SELECT quantity FROM hub_trade_positions WHERE tenant=? AND mode=? AND venue=? AND instrument=?",
            (*scope, venue, intent["instrument"]),
        ).fetchone()
        qty = Decimal(intent["quantity"])
        if intent["side"] == "BUY":
            if Decimal(account[0]) < reserved or (
                remote is not None and _decimal(remote["cash"]) < reserved
            ):
                raise TradingError("insufficient_cash")
            exposure = sum(
                (
                    max(
                        Decimal(row["cost"]),
                        Decimal(row["quantity"]) * Decimal(intent["limit_price"]),
                    )
                    if row["venue"] == venue and row["instrument"] == intent["instrument"]
                    else Decimal(row["cost"])
                    for row in db.execute(
                        "SELECT venue,instrument,quantity,cost FROM hub_trade_positions WHERE tenant=? AND mode=?",
                        scope,
                    )
                ),
                _ZERO,
            )
            if exposure + reserved > self.max_position_quote:
                raise TradingError("max_position_quote_exceeded")
        elif (
            not position
            or Decimal(position[0]) < qty
            or (remote is not None and _decimal(remote["quantity"]) < qty)
        ):
            raise TradingError("insufficient_position_no_short_selling")

    def _save(self, db: sqlite3.Connection, tenant: str, record: dict[str, Any]) -> None:
        record["updated_at"] = time.time()
        db.execute(
            "UPDATE hub_trade_orders SET status=?,data=? WHERE tenant=? AND mode=? AND idem=?",
            (record["status"], _json(record), tenant, self.mode, record["idempotency_key"]),
        )

    def _finish(
        self, db: sqlite3.Connection, tenant: str, record: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        status = result.get("status")
        if result.get("side") is not None and str(result["side"]).upper() != record["side"]:
            raise TradingError("execution_side_mismatch")
        if status not in _TERMINAL | _OPEN:
            raise TradingError("unknown_execution_status")
        qty, gross = _decimal(result.get("filled_qty", 0)), _decimal(result.get("quote_qty", 0))
        fee_base, fee_quote = (
            _decimal(result.get("fee_btc", result.get("fee_base", 0))),
            _decimal(result.get("fee_quote", 0)),
        )
        requested, limit = Decimal(record["quantity"]), Decimal(record["limit_price"])
        if qty > requested or ((qty == 0) != (gross == 0)) or (status == "FILLED" and qty == 0):
            raise TradingError("invalid_execution_quantities")
        if (status == "REJECTED" and qty) or (qty == 0 and (fee_base or fee_quote)):
            raise TradingError("invalid_execution_fee_or_rejection")
        if qty and (
            (record["side"] == "BUY" and gross > qty * limit)
            or (record["side"] == "SELL" and gross < qty * limit)
        ):
            raise TradingError("execution_violated_limit")
        current = db.execute(
            "SELECT data FROM hub_trade_orders WHERE tenant=? AND mode=? AND idem=?",
            (tenant, self.mode, record["idempotency_key"]),
        ).fetchone()
        if current and json.loads(current[0]).get("accounted"):
            return cast(dict[str, Any], json.loads(current[0]))
        # Partial fills stay reserved until a terminal result with complete fees.
        if status in _OPEN:
            record.update(status=status, exchange_id=str(result.get("exchange_id", "")))
            self._save(db, tenant, record)
            return record
        scope = (tenant, self.mode, record["venue"])
        cash = Decimal(
            db.execute(
                "SELECT cash FROM hub_trade_accounts WHERE tenant=? AND mode=? AND venue=?", scope
            ).fetchone()[0]
        )
        row = db.execute(
            "SELECT quantity,cost,realized FROM hub_trade_positions WHERE tenant=? AND mode=? AND venue=? AND instrument=?",
            (*scope, record["instrument"]),
        ).fetchone()
        held, cost, realized = tuple(Decimal(x) for x in row) if row else (_ZERO, _ZERO, _ZERO)
        if record["side"] == "BUY":
            cash -= gross + fee_quote
            held += qty - fee_base
            cost += gross + fee_quote
        else:
            removed = qty + fee_base
            if removed > held:
                raise TradingError("execution_position_drift")
            basis = cost * removed / held if held else _ZERO
            cash += gross - fee_quote
            held -= removed
            cost -= basis
            realized += gross - fee_quote - basis
        if cash < 0 or held < 0 or fee_base > qty:
            raise TradingError("execution_budget_or_fee_drift")
        db.execute(
            "UPDATE hub_trade_accounts SET cash=? WHERE tenant=? AND mode=? AND venue=?",
            (str(cash), *scope),
        )
        db.execute(
            "INSERT OR REPLACE INTO hub_trade_positions VALUES(?,?,?,?,?,?,?)",
            (*scope, record["instrument"], str(held), str(cost), str(realized)),
        )
        record.update(
            status=status,
            filled_qty=str(qty),
            quote_qty=str(gross),
            fee_base=str(fee_base),
            fee_quote=str(fee_quote),
            exchange_id=str(result.get("exchange_id", "")),
            accounted=True,
        )
        record.pop("error", None)
        self._save(db, tenant, record)
        db.execute(
            "UPDATE hub_trade_orders SET reserved=? WHERE tenant=? AND mode=? AND idem=?",
            (
                str(gross + fee_quote + fee_base * limit),
                tenant,
                self.mode,
                record["idempotency_key"],
            ),
        )
        return record

    def orders(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        tenant = _identifier(tenant_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise TradingError("invalid_order_limit")
        with self._db() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT data FROM hub_trade_orders WHERE tenant=? AND mode=? ORDER BY created DESC,idem LIMIT ?",
                    (tenant, self.mode, limit),
                )
            ]

    def portfolio(self, tenant_id: str) -> dict[str, Any]:
        tenant = _identifier(tenant_id)
        with self._db() as db:
            scope = (tenant, self.mode)
            cash = {
                row[0]: {"currency": _CURRENCIES[row[0]], "amount": row[1]}
                for row in db.execute(
                    "SELECT venue,cash FROM hub_trade_accounts WHERE tenant=? AND mode=?", scope
                )
            }
            positions = [
                dict(row)
                for row in db.execute(
                    "SELECT venue,instrument,quantity,cost,realized FROM hub_trade_positions WHERE tenant=? AND mode=? ORDER BY venue,instrument",
                    scope,
                )
            ]
            for position in positions:
                qty = Decimal(position["quantity"])
                position["average_price"] = str(Decimal(position["cost"]) / qty if qty else _ZERO)
        return {
            "mode": self.mode,
            "cash": cash,
            "positions": positions,
            "valuation": "historical_cost_separate_quote_currencies_not_current_equity",
            "paper_starting_cash_per_venue": "1000" if self.mode == "paper" else None,
        }

    def set_kill(self, tenant_id: str, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise TradingError("kill_requires_boolean")
        return self.set_risk_mode(tenant_id, "frozen" if enabled else "active")

    def set_risk_mode(self, tenant_id: str, mode: str) -> dict[str, Any]:
        """Persist active / inventory reductions only / all new sends frozen.

        Existing kill rows 0/1 remain active/frozen; value 2 adds reduce-only
        without migrating or weakening the former full-stop semantics.
        """
        tenant = _identifier(tenant_id)
        if not isinstance(mode, str) or mode not in _RISK_MODES:
            raise TradingError("unsupported_risk_mode")
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO hub_trade_kill VALUES(?,?)", (tenant, _RISK_MODES[mode])
            )
        return self.status(tenant)

    def _check_risk_mode(self, db: sqlite3.Connection, tenant: str, side: str) -> None:
        control = db.execute(
            "SELECT enabled FROM hub_trade_kill WHERE tenant=?", (tenant,)
        ).fetchone()
        value = control[0] if control else 0
        if value not in {0, 2}:
            if self.frozen_allows_protective_exit and side == "SELL":
                return
            raise TradingError("kill_switch_enabled")
        if value == 2 and side != "SELL":
            raise TradingError("reduce_only_mode_new_risk_blocked")

    def status(self, tenant_id: str) -> dict[str, Any]:
        tenant = _identifier(tenant_id)
        with self._db() as db:
            kill = db.execute(
                "SELECT enabled FROM hub_trade_kill WHERE tenant=?", (tenant,)
            ).fetchone()
            unresolved = db.execute(
                "SELECT COUNT(*) FROM hub_trade_orders WHERE tenant=? AND mode=? AND status IN ("
                + ",".join("?" for _ in _OPEN)
                + ")",
                (tenant, self.mode, *_OPEN),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT reserved,data FROM hub_trade_orders WHERE tenant=? AND mode=? AND day=?",
                (tenant, self.mode, time.strftime("%Y-%m-%d", time.gmtime())),
            ).fetchall()
        risk_mode = next(
            (name for name, value in _RISK_MODES.items() if value == (kill[0] if kill else 0)),
            "frozen",
        )
        increase_used = sum(
            (Decimal(row["reserved"]) for row in rows if json.loads(row["data"])["side"] == "BUY"),
            _ZERO,
        )
        turnover = sum((Decimal(row["reserved"]) for row in rows), _ZERO)
        return {
            "mode": self.mode,
            "kill_enabled": risk_mode == "frozen",
            "risk_mode": risk_mode,
            "unresolved_orders": unresolved,
            "readiness_kind": "configuration_only_not_live_acceptance",
            "execution_enabled": self.mode != "paper"
            and any(k[0] == tenant for k in self._adapters),
            "venues": {
                venue: {
                    "ready": self.mode == "paper" or (tenant, venue) in self._adapters,
                    "missing_settings": self._readiness.get(
                        (tenant, venue),
                        []
                        if self.mode == "paper" or (tenant, venue) in self._adapters
                        else ["server_execution_binding"],
                    ),
                }
                for venue in _CURRENCIES
            },
            "limits": {
                "max_order_quote": str(self.max_order_quote),
                "max_position_quote": str(self.max_position_quote),
                "max_daily_quote": str(self.max_daily_quote),
                "daily_limit_policy": "new_risk_buy_including_fees_sell_does_not_refund",
            },
            "daily_usage": {
                "timezone": "UTC",
                "new_risk_quote": str(increase_used),
                "new_risk_remaining_quote": str(max(_ZERO, self.max_daily_quote - increase_used)),
                "turnover_quote_including_fees_and_open_reservations": str(turnover),
                "sell_reduce_only_quote": str(turnover - increase_used),
            },
            "limitations": [
                "paper_is_not_live_acceptance",
                "no_margin_shorting_redemption_or_withdrawal",
                "dedicated_execution_account_required",
                "unknown_orders_are_never_resent",
                "daily_new_risk_budget_counts_buy_including_fees",
                "reduce_only_sell_still_requires_limits_inventory_market_and_no_unknown_orders",
            ],
        }

    def reconcile(self, tenant_id: str) -> dict[str, Any]:
        tenant = _identifier(tenant_id)
        results = []
        for record in self.orders(tenant, 1000):
            if record["status"] not in _OPEN:
                continue
            adapter = self._adapters.get((tenant, record["venue"]))
            result = None
            if adapter is not None:
                with suppress(Exception):
                    result = adapter.lookup(record["order_id"], record)
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT data FROM hub_trade_orders WHERE tenant=? AND mode=? AND idem=?",
                    (tenant, self.mode, record["idempotency_key"]),
                ).fetchone()
                record = json.loads(current[0])
                if record.get("accounted"):
                    continue
                if result is not None:
                    try:
                        record = self._finish(db, tenant, record, result)
                    except TradingError:
                        record["status"] = "UNKNOWN"
                        self._save(db, tenant, record)
                else:
                    record["status"] = "UNKNOWN"
                    record["error"] = "not_confirmed_no_resubmission_operator_review_required"
                    self._save(db, tenant, record)
            results.append({"order_id": record["order_id"], "status": record["status"]})
        return {
            "mode": self.mode,
            "orders": results,
            "resubmitted": 0,
            "unresolved_orders": self.status(tenant)["unresolved_orders"],
        }


def trading_from_env(path: Path, *, environ: Mapping[str, str] | None = None) -> TradingService:
    """Server startup factory; never binds request-supplied tenants or credentials.

    Environment values may instead live in a 0600 JSON secret file selected by
    QT_AGENT_HUB_TRADING_SECRETS_FILE. Do not commit that file. Missing/bad config
    is reported only using setting names. No order is sent during construction.
    """
    env = dict(os.environ if environ is None else environ)
    secret_file = env.get("QT_AGENT_HUB_TRADING_SECRETS_FILE")
    if secret_file:
        try:
            location = Path(secret_file).expanduser()
            if location.stat().st_mode & 0o077 or location.stat().st_size > 65536:
                raise TradingError("private_secret_file_required")
            values = json.loads(location.read_text())
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise TradingError("invalid_secret_file")
            env = {**values, **env}
        except (OSError, ValueError, RecursionError):
            raise TradingError("invalid_QT_AGENT_HUB_TRADING_SECRETS_FILE") from None
    service = TradingService(path, mode=env.get("QT_AGENT_HUB_TRADING_MODE", "paper"))
    if service.mode == "paper":
        return service
    tenant = env.get("QT_AGENT_HUB_EXECUTION_TENANT", "local")
    _identifier(tenant)
    common = [
        name
        for name in ("QT_AGENT_HUB_EXECUTION_TENANT", "QT_AGENT_HUB_EXECUTION_ENABLED")
        if not env.get(name) or (name.endswith("ENABLED") and env[name] != "true")
    ]
    requirements = (
        {"binance": ["BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"]}
        if service.mode == "testnet"
        else {
            "binance": ["BINANCE_API_KEY", "BINANCE_API_SECRET"],
            "polymarket": [
                "POLYMARKET_PRIVATE_KEY",
                "POLYMARKET_WALLET_ADDRESS",
                "POLYMARKET_API_KEY",
                "POLYMARKET_API_SECRET",
                "POLYMARKET_API_PASSPHRASE",
                "POLYMARKET_USER_COUNTRY",
            ],
        }
    )
    for venue, names in requirements.items():
        missing = [*common, *(name for name in names if not env.get(name))]
        if missing:
            service._readiness[(tenant, venue)] = missing
            continue
        try:
            adapter: ExecutionAdapter
            if venue == "binance":
                adapter = (
                    BinanceTestnetAdapter(env[names[0]], env[names[1]])
                    if service.mode == "testnet"
                    else BinanceSpotAdapter(env[names[0]], env[names[1]])
                )
            else:
                adapter = PolymarketAdapter(
                    *(env[name] for name in names[:5]), user_country=env[names[5]]
                )
            service.bind_execution(
                tenant,
                venue,
                adapter,
                acknowledge=True,
                funding_quote=float(env.get("QT_AGENT_HUB_EXECUTION_BUDGET", "100")),
            )
        except Exception:
            service._readiness[(tenant, venue)] = ["execution_adapter_initialization_failed"]
    return service
