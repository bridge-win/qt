"""Offline OKX demo wire, accounting, isolation, and ambiguous-order contracts."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import cast

import ccxt
import pytest

from qt.btc_agent.broker import BrokerError
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.okx_demo import OKXDemoBroker, _DemoExchange
from qt.btc_agent.service import Service
from qt.btc_agent.store import Store

_CLIENT_ID = "qtc0123456789abcdef0123456789ab"
_INSTRUMENT = {
    "instType": "SPOT", "instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT",
    "state": "live", "tickSz": "0.1", "lotSz": "0.00001", "minSz": "0.0001",
    "maxLmtSz": "100", "maxLmtAmt": "1000000",
}


def _response(*rows: dict[str, object], code: str = "0") -> dict[str, object]:
    return {"code": code, "data": list(rows)}


def _order(**changes: object) -> dict[str, object]:
    return {
        "instId": "BTC-USDT", "instType": "SPOT", "tdMode": "cash", "ordType": "ioc",
        "clOrdId": _CLIENT_ID, "ordId": "123", "side": "buy", "state": "filled",
        "accFillSz": "0.001", "sz": "0.001", "px": "50000", **changes,
    }


def _trade(**changes: object) -> dict[str, object]:
    return {
        "instId": "BTC-USDT", "instType": "SPOT", "ordId": "123", "side": "buy",
        "tradeId": "456", "fillSz": "0.001", "fillPx": "50000",
        "fee": "-0.000001", "feeCcy": "BTC", **changes,
    }


@pytest.fixture
def broker() -> OKXDemoBroker:
    return OKXDemoBroker("fake-demo-key", "fake-demo-secret", "fake-demo-passphrase")


def _transport(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch,
    changes: dict[tuple[str, str], object] | None = None,
) -> list[tuple[str, str, str, dict[str, object]]]:
    responses: dict[tuple[str, str], object] = {
        ("public/instruments", "GET"): _response(copy.deepcopy(_INSTRUMENT)),
        ("trade/order", "POST"): _response({"clOrdId": _CLIENT_ID, "ordId": "123", "sCode": "0"}),
        ("trade/order", "GET"): _response(_order()),
        ("trade/fills", "GET"): _response(_trade()),
    }
    responses.update(changes or {})
    calls: list[tuple[str, str, str, dict[str, object]]] = []

    def request(path: str, api: str, method: str, params: dict[str, object]) -> object:
        calls.append((path, api, method, params))
        value = responses[(path, method)]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(broker._client, "request", request)
    return calls


def test_demo_construction_has_no_retries_redirects_or_production_url(broker: OKXDemoBroker) -> None:
    assert broker._client.options["sandboxMode"] is True
    assert broker._client.headers["x-simulated-trading"] == "1"
    assert broker._client.urls["api"] == {"rest": "https://openapi.okx.com"}
    assert broker._client.maxRetriesOnFailure == 0
    assert broker._client.options["maxRetriesOnFailure"] == 0
    assert broker._client.session.max_redirects == 0
    assert not broker._client.session.trust_env
    assert broker._client.logger.disabled


@pytest.mark.parametrize("credentials", [
    ("key", "", ""), ("", "secret", ""), ("", "", "passphrase"),
    ("key", "secret", ""), (" key", "secret", "passphrase"),
    ("key", "secret\n", "passphrase"),
])
def test_partial_or_malformed_credentials_rejected(credentials: tuple[str, str, str]) -> None:
    with pytest.raises(BrokerError):
        OKXDemoBroker(*credentials)


@pytest.mark.parametrize("url,method,headers", [
    ("https://www.okx.com/api/v5/trade/order", "POST", {"x-simulated-trading": "1"}),
    ("https://openapi.okx.com.evil.example/api/v5/trade/order", "POST", {"x-simulated-trading": "1"}),
    ("http://openapi.okx.com/api/v5/trade/order", "POST", {"x-simulated-trading": "1"}),
    ("https://openapi.okx.com/api/v5/asset/withdrawal", "POST", {"x-simulated-trading": "1"}),
    ("https://openapi.okx.com/api/v5/trade/order", "DELETE", {"x-simulated-trading": "1"}),
    ("https://openapi.okx.com/api/v5/trade/order#x", "GET", {"x-simulated-trading": "1"}),
    ("https://openapi.okx.com/api/v5/trade/order", "GET", {}),
    ("https://openapi.okx.com/api/v5/trade/order", "GET", {"x-simulated-trading": "0"}),
    ("https://openapi.okx.com/api/v5/trade/order", "GET", {"x-simulated-trading": "1", "X-Simulated-Trading": "0"}),
])
def test_final_transport_fail_closed(url: str, method: str, headers: dict[str, str]) -> None:
    with pytest.raises(BrokerError, match="allowlist"):
        _DemoExchange().fetch(url, method, headers)


def test_actual_ccxt_signing_keeps_demo_header_before_transport(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, str] | None, str | None]] = []

    def fetch(
        self: object, url: str, method: str = "GET", headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> object:
        calls.append((url, method, headers, body))
        return _response(_order(state="live", accFillSz="0"))

    monkeypatch.setattr(ccxt.okx, "fetch", fetch)
    fill = broker.lookup(_CLIENT_ID)
    assert fill is not None and fill.status == "NEW"
    url, method, headers, body = calls[0]
    assert url == f"https://openapi.okx.com/api/v5/trade/order?instId=BTC-USDT&clOrdId={_CLIENT_ID}"
    assert method == "GET" and body is None
    assert headers is not None and headers["x-simulated-trading"] == "1"
    assert all(name in headers for name in (
        "OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-TIMESTAMP", "OK-ACCESS-PASSPHRASE",
    ))
    broker._client.set_sandbox_mode(False)
    with pytest.raises(BrokerError, match="allowlist"):
        broker.lookup(_CLIENT_ID)
    assert len(calls) == 1


def test_precision_and_minimums_use_shared_decimal_rules(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _transport(broker, monkeypatch)
    assert broker.prepare_price(50000.09, "buy") == 50000
    assert broker.prepare_price(50000.01, "sell") == 50000.1
    assert broker.prepare_quantity(0.001019, 50000) == 0.00101
    assert broker.is_dust(0.00009, 50000)
    assert not broker.is_dust(0.0001, 50000)
    with pytest.raises(BrokerError):
        broker.prepare_quantity(0.00009, 50000)


@pytest.mark.parametrize("changes", [
    {"instType": "SWAP"}, {"instId": "ETH-USDT"}, {"state": "suspend"},
    {"tickSz": "0"}, {"lotSz": "nan"}, {"maxLmtAmt": ""}, {"maxLmtSz": "-1"},
])
def test_invalid_market_never_submits(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    calls = _transport(broker, monkeypatch, {
        ("public/instruments", "GET"): _response({**_INSTRUMENT, **changes}),
    })
    with pytest.raises(BrokerError):
        broker.submit(_CLIENT_ID, "buy", 0.001, 50000)
    assert all(call[2] == "GET" for call in calls)


def test_cash_ioc_ack_is_followed_by_exact_order_and_fees(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _transport(broker, monkeypatch)
    result = broker.submit(_CLIENT_ID, "buy", 0.001009, 50000.09)
    assert result.status == "FILLED" and result.filled_qty == 0.001
    assert result.quote_qty == 50 and result.fee_btc == 0.000001
    assert calls[1] == ("trade/order", "private", "POST", {
        "instId": "BTC-USDT", "tdMode": "cash", "side": "buy", "ordType": "ioc",
        "sz": "0.001", "px": "50000.0", "clOrdId": _CLIENT_ID,
    })
    assert calls[2][3] == {"instId": "BTC-USDT", "clOrdId": _CLIENT_ID}
    assert calls[3][3] == {"instType": "SPOT", "instId": "BTC-USDT", "ordId": "123", "limit": 100}


def test_partial_canceled_sell_includes_quote_commission(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _transport(broker, monkeypatch, {
        ("trade/order", "GET"): _response(_order(side="sell", state="canceled", accFillSz="0.0005")),
        ("trade/fills", "GET"): _response(_trade(side="sell", fillSz="0.0005", fee="-0.025", feeCcy="USDT")),
    })
    result = broker.submit(_CLIENT_ID, "sell", 0.001, 50000)
    assert result.status == "CANCELED" and result.filled_qty == 0.0005
    assert result.quote_qty == 25 and result.fee_quote == 0.025


@pytest.mark.parametrize("state,expected", [("live", "NEW"), ("canceled", "CANCELED")])
def test_zero_fill_ack_does_not_invent_execution(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, state: str, expected: str,
) -> None:
    calls = _transport(broker, monkeypatch, {
        ("trade/order", "GET"): _response(_order(state=state, accFillSz="0")),
    })
    fill = broker.submit(_CLIENT_ID, "buy", 0.001, 50000)
    assert fill.status == expected and fill.filled_qty == 0 and fill.quote_qty == 0
    assert not any(call[0] == "trade/fills" for call in calls)


@pytest.mark.parametrize("change", [
    {"clOrdId": "other"}, {"instId": "ETH-USDT"}, {"instType": "SWAP"},
    {"tdMode": "cross"}, {"ordType": "market"}, {"state": "unknown"},
    {"accFillSz": "nan"}, {"accFillSz": "0.002"}, {"ordId": ""},
])
def test_inconsistent_order_remains_unresolved(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    _transport(broker, monkeypatch, {("trade/order", "GET"): _response(_order(**change))})
    with pytest.raises(BrokerError):
        broker.lookup(_CLIENT_ID)


@pytest.mark.parametrize("change", [
    {"ordId": "other"}, {"instId": "ETH-USDT"}, {"side": "sell"},
    {"instType": "SWAP"}, {"clOrdId": "other"},
    {"fillSz": "0.0001"}, {"fillPx": "nan"}, {"fee": "0.01"},
    {"feeCcy": "OKB"}, {"tradeId": ""},
])
def test_incomplete_fills_or_unaccounted_fees_fail_closed(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    _transport(broker, monkeypatch, {("trade/fills", "GET"): _response(_trade(**change))})
    with pytest.raises(BrokerError):
        broker.lookup(_CLIENT_ID)


@pytest.mark.parametrize("failure", [
    ccxt.RequestTimeout("signature=fake-secret"), ccxt.AuthenticationError("fake-secret"),
    ccxt.InvalidOrder("Duplicate client order ID fake-secret"),
    ccxt.InvalidOrder("51016 Client order ID already exists fake-secret"),
    ccxt.InsufficientFunds("fake-secret"),
])
def test_unknown_submission_sanitized_and_never_retried(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    calls = _transport(broker, monkeypatch, {("trade/order", "POST"): failure})
    with pytest.raises(BrokerError) as error:
        broker.submit(_CLIENT_ID, "buy", 0.001, 50000)
    assert "fake-secret" not in str(error.value)
    assert error.value.__suppress_context__
    assert len([call for call in calls if call[2] == "POST"]) == 1


@pytest.mark.parametrize("response", [
    {"code": "50101", "data": []}, {"code": 0, "data": []},
    _response({"sCode": "51008", "ordId": "", "clOrdId": _CLIENT_ID}),
    _response({"sCode": "0", "ordId": "123", "clOrdId": "other"}),
])
def test_http_success_or_wrong_environment_is_not_order_success(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, response: object,
) -> None:
    calls = _transport(broker, monkeypatch, {("trade/order", "POST"): response})
    with pytest.raises(BrokerError):
        broker.submit(_CLIENT_ID, "buy", 0.001, 50000)
    assert len([call for call in calls if call[2] == "POST"]) == 1


def test_not_found_only_is_missing_and_auth_error_never_falls_back(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _transport(broker, monkeypatch, {("trade/order", "GET"): ccxt.OrderNotFound("fake-secret")})
    assert broker.lookup(_CLIENT_ID) is None
    _transport(broker, monkeypatch, {("trade/order", "GET"): ccxt.AuthenticationError("fake-secret")})
    with pytest.raises(BrokerError, match="AuthenticationError"):
        broker.lookup(_CLIENT_ID)
    assert broker._client.urls["api"] == {"rest": "https://openapi.okx.com"}


def test_quote_balances_and_bad_id(broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _transport(broker, monkeypatch, {
        ("market/books", "GET"): _response({"bids": [["50000", "1", "0", "1"]], "asks": [["50001", "1", "0", "1"]], "ts": "1800000000000"}),
        ("account/balance", "GET"): _response({"details": [{"ccy": "BTC", "availBal": "0.1"}, {"ccy": "USDT", "availBal": "100"}]}),
    })
    assert broker.quote().bid == 50000
    assert broker.quote().timestamp_ms == 1800000000000
    assert broker.balances() == {"BTC": 0.1, "USDT": 100}
    length = len(calls)
    for client_id in ("invalid-id", "x" * 33, "../path"):
        with pytest.raises(BrokerError, match="client order ID"):
            broker.lookup(client_id)
    assert len(calls) == length


def test_url_mutation_is_blocked_before_parent_transport(broker: OKXDemoBroker) -> None:
    broker._client.urls["api"] = {"rest": "https://www.okx.com"}
    with pytest.raises(BrokerError, match="allowlist"):
        broker.lookup(cast(str, _CLIENT_ID))


def test_duplicate_fills_are_never_counted_twice(broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    _transport(broker, monkeypatch, {("trade/fills", "GET"): _response(
        _trade(fillSz="0.0005"), _trade(fillSz="0.0005"),
    )})
    with pytest.raises(BrokerError, match="duplicated"):
        broker.lookup(_CLIENT_ID)


@pytest.mark.parametrize("failure,expected", [
    (ccxt.OrderNotFound("delayed visibility"), "unresolved_order"),
    (_response(_trade(fee="0.01")), "DemoFeeAccountingError"),
    (_response(_trade(fillSz="0.0001")), "DemoFillsIncompleteError"),
])
def test_restart_preserves_pending_intent_and_later_reconciles_without_post(
    broker: OKXDemoBroker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    failure: object, expected: str,
) -> None:
    cfg = AgentConfig(mode="testnet", test_environment="okx_demo", state_dir=tmp_path)
    store = Store(cfg.database)
    store.intent(_CLIENT_ID, "buy", 0.001, 50000, 1)
    store.close()

    def no_market_or_order(*args: object) -> object:
        raise BrokerError("Stop after reconciliation; no external calls")

    monkeypatch.setattr(broker, "quote", no_market_or_order)
    monkeypatch.setattr(broker, "submit", no_market_or_order)
    failure_path = "trade/order" if isinstance(failure, Exception) else "trade/fills"
    calls = _transport(broker, monkeypatch, {(failure_path, "GET"): failure})
    store = Store(cfg.database)
    worker = Service(cfg, store, broker, no_market_or_order, no_market_or_order)  # type: ignore[arg-type]
    result = worker.cycle()
    assert result["reason"] == expected
    assert result["test_environment"] == "okx_demo"
    assert len(store.pending()) == 1 and store.position().qty == 0
    assert all(call[2] == "GET" for call in calls)
    store.close()

    # A new process with the same durable ID reconciles; it never creates another ID.
    calls = _transport(broker, monkeypatch)
    store = Store(cfg.database)
    worker = Service(cfg, store, broker, no_market_or_order, no_market_or_order)  # type: ignore[arg-type]
    worker.cycle()
    assert store.pending() == []
    assert store.position().qty == pytest.approx(0.000999)
    assert all(call[2] == "GET" for call in calls)
    worker.cycle()
    assert store.position().qty == pytest.approx(0.000999)
    store.close()
