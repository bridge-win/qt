"""Testnet endpoint isolation, bounded orders, and ambiguous-order reconciliation."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import cast

import ccxt
import pytest

from qt.btc_agent.broker import BinanceTestnetBroker, BrokerError, _TestnetExchange

_CLIENT_ID = "qt-test-intent-1"
_MARKET: dict[str, object] = {"symbols": [{
    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
    "status": "TRADING", "isSpotTradingAllowed": True,
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "100", "stepSize": "0.00001"},
        {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "1000000"},
    ],
}]}


def _order(**values: object) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT", "clientOrderId": _CLIENT_ID, "orderId": 123,
        "side": "BUY", "status": "FILLED", "executedQty": "0.001",
        "cummulativeQuoteQty": "50", "fills": [
            {"qty": "0.001", "commissionAsset": "BTC", "commission": "0.000001"},
        ], **values,
    }


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> BinanceTestnetBroker:
    instance = BinanceTestnetBroker("test-api-key", "test-api-secret")
    def request(
        path: str, api: str, method: str, params: dict[str, object],
    ) -> object:
        if path == "exchangeInfo":
            return copy.deepcopy(_MARKET)
        raise AssertionError(f"Unexpected request: {path}")
    monkeypatch.setattr(instance._client, "request", request)
    return instance


def _transport(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, str], object],
) -> list[tuple[str, str, str, dict[str, object]]]:
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    def request(path: str, api: str, method: str, params: dict[str, object]) -> object:
        calls.append((path, api, method, params))
        if path == "exchangeInfo":
            return copy.deepcopy(_MARKET)
        response = responses[(path, method)]
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(broker._client, "request", request)
    return calls


def test_construction_enforces_sandbox_no_retries_or_redirects() -> None:
    broker = BinanceTestnetBroker("", "")
    assert broker._client.options["sandboxMode"] is True
    assert broker._client.options["maxRetriesOnFailure"] == 0
    assert broker._client.urls["api"] == {
        "public": "https://testnet.binance.vision/api/v3",
        "private": "https://testnet.binance.vision/api/v3",
    }
    assert broker._client.session.max_redirects == 0
    assert not broker._client.session.trust_env
    assert broker._client.logger.disabled


@pytest.mark.parametrize("url,method", [
    ("https://api.binance.com/api/v3/order", "POST"),
    ("https://testnet.binancefuture.com/fapi/v1/order", "POST"),
    ("http://testnet.binance.vision/api/v3/order", "POST"),
    ("https://testnet.binance.vision.evil.example/api/v3/order", "POST"),
    ("https://testnet.binance.vision/sapi/v1/capital/withdraw/apply", "POST"),
    ("https://testnet.binance.vision/api/v3/order", "DELETE"),
    ("https://testnet.binance.vision/api/v3/order#hidden", "POST"),
])
def test_transport_rejects_every_non_allowlisted_route(url: str, method: str) -> None:
    exchange = _TestnetExchange()
    with pytest.raises(BrokerError, match="allowlist"):
        exchange.fetch(url, method)


def test_transport_permits_only_expected_testnet_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(*args: object) -> object:
        return {"ok": True}
    monkeypatch.setattr(ccxt.binance, "fetch", fetch)
    exchange = _TestnetExchange()
    assert exchange.fetch("https://testnet.binance.vision/api/v3/order?symbol=BTCUSDT") == {"ok": True}


def test_precision_never_increases_buy_cost_or_sell_quantity(broker: BinanceTestnetBroker) -> None:
    assert broker.prepare_price(50000.009, "buy") == 50000
    assert broker.prepare_price(50000.001, "sell") == 50000.01
    assert broker.prepare_quantity(0.001019999, 50000) == 0.00101


@pytest.mark.parametrize("qty,price", [
    (0.00001, 50000), (101, 50000), (0.001, float("nan")),
    (float("inf"), 50000), (0, 50000), (-1, 50000), (30, 50000),
])
def test_filter_violations_rejected(broker: BinanceTestnetBroker, qty: float, price: float) -> None:
    with pytest.raises(BrokerError):
        broker.prepare_quantity(qty, price)


@pytest.mark.parametrize("qty,price,expected", [
    (0, 50000, True), (0.0000099, 50000, True), (0.00009, 50000, True),
    (0.0001, 50000, False), (0.001, 50000, False), (101, 50000, False),
])
def test_dust_requires_confirmed_exchange_minimum_violation(
    broker: BinanceTestnetBroker, qty: float, price: float, expected: bool,
) -> None:
    assert broker.is_dust(qty, price) is expected


@pytest.mark.parametrize("qty,price", [
    (-1, 50000), (float("nan"), 50000), (float("inf"), 50000),
    (0.00001, 0), (0.00001, float("nan")), (0.00001, 1_000_001),
])
def test_dust_never_hides_invalid_values(
    broker: BinanceTestnetBroker, qty: float, price: float,
) -> None:
    with pytest.raises(BrokerError):
        broker.is_dust(qty, price)


def test_dust_does_not_swallow_metadata_network_failure(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request(*args: object) -> object:
        raise ccxt.RequestTimeout("secret")
    monkeypatch.setattr(broker._client, "request", request)
    with pytest.raises(BrokerError, match="RequestTimeout"):
        broker.is_dust(0.000001, 50000)


def test_ioc_limit_submission_and_base_fee(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _transport(broker, monkeypatch, {("order", "POST"): _order()})
    result = broker.submit(_CLIENT_ID, "buy", 0.001009, 50000.009)
    assert result.status == "FILLED"
    assert result.filled_qty == 0.001
    assert result.quote_qty == 50
    assert result.fee_btc == 0.000001
    assert calls[-1] == ("order", "private", "POST", {
        "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "IOC",
        "quantity": "0.001", "price": "50000.0", "newClientOrderId": _CLIENT_ID,
        "newOrderRespType": "FULL",
    })


def test_partial_ioc_is_expired_with_fill_and_quote_fees(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _transport(broker, monkeypatch, {("order", "POST"): _order(
        side="SELL", status="EXPIRED", executedQty="0.0005", cummulativeQuoteQty="25",
        fills=[{"qty": "0.0005", "commissionAsset": "USDT", "commission": "0.025"}],
    )})
    result = broker.submit(_CLIENT_ID, "sell", 0.001, 50000)
    assert result.status == "EXPIRED"
    assert result.filled_qty == 0.0005
    assert result.fee_quote == 0.025


@pytest.mark.parametrize("change", [
    {"side": "SELL"}, {"cummulativeQuoteQty": "50.01"},
])
def test_submit_verifies_reported_fill_against_intent(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    _transport(broker, monkeypatch, {("order", "POST"): _order(**change)})
    with pytest.raises(BrokerError):
        broker.submit(_CLIENT_ID, "buy", 0.001, 50000)


@pytest.mark.parametrize("status", ["NEW", "PENDING_NEW", "CANCELED", "REJECTED", "EXPIRED"])
def test_zero_fill_states_preserved(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    _transport(broker, monkeypatch, {("order", "GET"): _order(
        status=status, executedQty="0", cummulativeQuoteQty="0", fills=[],
    )})
    result = broker.lookup(_CLIENT_ID)
    assert result is not None and result.status == status
    assert result.filled_qty == 0


def test_lookup_uses_persisted_client_id_and_recovers_commission(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _transport(broker, monkeypatch, {
        ("order", "GET"): _order(fills=[]),
        ("myTrades", "GET"): [
            {"orderId": 123, "qty": "0.0004", "commission": "0.0000004", "commissionAsset": "BTC"},
            {"orderId": 123, "qty": "0.0006", "commission": "0.0000006", "commissionAsset": "BTC"},
        ],
    })
    result = broker.lookup(_CLIENT_ID)
    assert result is not None and result.fee_btc == 0.000001
    assert calls[0] == ("order", "private", "GET", {
        "symbol": "BTCUSDT", "origClientOrderId": _CLIENT_ID,
    })
    assert calls[1][3] == {"symbol": "BTCUSDT", "orderId": "123", "limit": 1000}


def test_lookup_only_not_found_returns_none(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, str], object] = {("order", "GET"): ccxt.OrderNotFound("secret")}
    _transport(broker, monkeypatch, responses)
    assert broker.lookup(_CLIENT_ID) is None
    responses[("order", "GET")] = ccxt.AuthenticationError("apiKey=secret")
    with pytest.raises(BrokerError, match="AuthenticationError") as error:
        broker.lookup(_CLIENT_ID)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_submit_timeout_is_sanitized_and_never_retried(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _transport(broker, monkeypatch, {
        ("order", "POST"): ccxt.RequestTimeout("signature=private-api-secret"),
    })
    with pytest.raises(BrokerError, match="RequestTimeout") as error:
        broker.submit(_CLIENT_ID, "buy", 0.001, 50000)
    assert "private-api-secret" not in str(error.value)
    assert len([row for row in calls if row[2] == "POST"]) == 1


def test_explicit_rejection_and_duplicate_are_distinct(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, str], object] = {("order", "POST"): ccxt.InvalidOrder("filter secret")}
    _transport(broker, monkeypatch, responses)
    assert broker.submit(_CLIENT_ID, "buy", 0.001, 50000).status == "REJECTED"
    responses[("order", "POST")] = ccxt.InvalidOrder("Duplicate order sent. secret")
    with pytest.raises(BrokerError, match="reconcile"):
        broker.submit(_CLIENT_ID, "buy", 0.001, 50000)


@pytest.mark.parametrize("change", [
    {"clientOrderId": "another-intent"}, {"symbol": "ETHUSDT"},
    {"status": "UNKNOWN"}, {"executedQty": "nan"}, {"cummulativeQuoteQty": "-1"},
    {"fills": [{"qty": "0.0001", "commission": "0", "commissionAsset": "BTC"}]},
    {"fills": [{"qty": "0.001", "commission": "0.01", "commissionAsset": "BNB"}]},
])
def test_unreliable_or_unaccounted_fill_rejected(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    _transport(broker, monkeypatch, {("order", "GET"): _order(**change)})
    with pytest.raises(BrokerError):
        broker.lookup(_CLIENT_ID)


def test_quote_and_balances(broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    _transport(broker, monkeypatch, {
        ("ticker/bookTicker", "GET"): {"symbol": "BTCUSDT", "bidPrice": "50000", "askPrice": "50001"},
        ("account", "GET"): {"balances": [
            {"asset": "BTC", "free": "0.02", "locked": "3"},
            {"asset": "USDT", "free": "40", "locked": "50"},
        ]},
    })
    quote = broker.quote()
    assert (quote.bid, quote.ask) == (50000, 50001)
    assert quote.timestamp_ms > 0
    assert broker.balances() == {"BTC": 0.02, "USDT": 40}


def test_bad_client_id_fails_before_transport(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _transport(broker, monkeypatch, {})
    with pytest.raises(BrokerError, match="Client order ID"):
        broker.lookup("../bad id")
    assert calls == []


def test_production_url_mutation_blocked_before_request(
    broker: BinanceTestnetBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Restore ccxt signing so this verifies the actual final HTTP transport guard.
    original = cast(Callable[..., object], ccxt.binance.request)
    monkeypatch.setattr(broker._client, "request", original.__get__(broker._client))
    broker._client.urls["api"]["private"] = "https://api.binance.com/api/v3"
    with pytest.raises(BrokerError, match="allowlist"):
        broker.lookup(_CLIENT_ID)
