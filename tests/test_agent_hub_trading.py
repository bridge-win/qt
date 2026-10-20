"""Real ledger/risk tests with public exchange protocol fixtures; no real orders."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from qt.agent_hub import trading
from qt.agent_hub.trading import (
    BinanceSpotAdapter,
    BinanceTestnetAdapter,
    LeaseLostError,
    PolymarketAdapter,
    TradingError,
    TradingService,
    trading_from_env,
)

_CONDITION = "0x" + "a" * 64
_TOKEN = "123456789012345678901234567890"
_OTHER = "987654321098765432109876543210"


def market(*, v2: bool = False, **changes: Any) -> dict[str, Any]:
    return {
        "slug": "bitcoin-above-100000",
        "question": "Will Bitcoin exceed $100,000?",
        "conditionId": _CONDITION,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "outcomes": '["Yes", "No"]',
        **(
            {"version": "v2", "positionIds": [_TOKEN, _OTHER]}
            if v2
            else {"clobTokenIds": json.dumps([_TOKEN, _OTHER])}
        ),
        **changes,
    }


@pytest.fixture
def public(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = {
        "bid": "50000",
        "ask": "50010",
        "size": "10",
        "market": market(),
        "poly_bid": "0.49",
        "poly_ask": "0.50",
        "stamp": time.time(),
        "requests": [],
    }

    def get(url: str, params: dict[str, str] | None = None) -> Any:
        state["requests"].append((url, params))
        if "binance.vision" in url:
            return {
                "symbol": "BTCUSDT",
                "bidPrice": state["bid"],
                "askPrice": state["ask"],
                "bidQty": state["size"],
                "askQty": state["size"],
            }
        if url == f"{trading.GAMMA_URL}/markets":
            return [copy.deepcopy(state["market"])]
        if "/markets/slug/" in url:
            return copy.deepcopy(state["market"])
        if "/markets-by-token/" in url:
            return {
                "condition_id": _CONDITION,
                "primary_token_id": _TOKEN,
                "secondary_token_id": _OTHER,
            }
        if url.endswith("/book"):
            assert params
            return {
                "asset_id": params["token_id"],
                "market": _CONDITION,
                "timestamp": str(int(state["stamp"] * 1000)),
                "bids": [{"price": state["poly_bid"], "size": state["size"]}],
                "asks": [{"price": state["poly_ask"], "size": state["size"]}],
            }
        if url.endswith("/public-search"):
            return {"events": [{"markets": [copy.deepcopy(state["market"])]}]}
        raise AssertionError(f"unexpected public request {url}")

    monkeypatch.setattr(trading, "_get_json", get)
    return state


def intent(*, key: str = "one", venue: str = "binance", **changes: Any) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "venue": venue,
        "instrument": "BTCUSDT" if venue == "binance" else _TOKEN,
        "side": "BUY",
        "quantity": "0.0002" if venue == "binance" else "10",
        "limit_price": "50010" if venue == "binance" else "0.50",
        "quote_timestamp": time.time(),
        **changes,
    }


def test_paper_bid_ask_cash_fees_cost_and_realized_profit_persist(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "trading.sqlite")
    buy = service.submit("alice", intent())
    assert buy["status"] == "FILLED"
    assert Decimal(buy["quote_qty"]) == Decimal("10.002")
    assert Decimal(buy["fee_quote"]) == Decimal("0.010002")
    public.update(bid="51000", ask="51010")
    service.submit("alice", intent(key="sell", side="SELL", quantity="0.0001", limit_price="51000"))
    saved = TradingService(service.path).portfolio("alice")
    position = saved["positions"][0]
    assert Decimal(saved["cash"]["binance"]["amount"]) == Decimal("995.082898")
    assert Decimal(position["quantity"]) == Decimal("0.0001")
    assert Decimal(position["cost"]) == Decimal("5.006001")
    assert Decimal(position["realized"]) == Decimal("0.088899")
    assert Decimal(position["average_price"]) == Decimal("50060.01")


def test_tenant_idempotency_isolation_and_no_global_account_balance(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    order = intent()
    a = service.submit("alice", order)
    reads = len(public["requests"])
    assert service.submit("alice", order) == a
    assert len(public["requests"]) == reads
    b = service.submit("bob", order)
    assert b["order_id"] != a["order_id"]
    assert len(service.orders("alice")) == len(service.orders("bob")) == 1
    assert service.orders("stranger") == []
    assert service.portfolio("stranger")["cash"] == {}
    with pytest.raises(TradingError, match="idempotency_key_conflict"):
        service.submit("alice", {**order, "quantity": "0.0003"})


def test_concurrent_duplicate_delivery_books_exactly_one_fill(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    order = intent()
    with ThreadPoolExecutor(max_workers=6) as pool:
        fills = list(pool.map(lambda _: service.submit("alice", order), range(12)))
    assert len({row["order_id"] for row in fills}) == 1
    assert len(service.orders("alice")) == 1
    assert Decimal(service.portfolio("alice")["positions"][0]["quantity"]) == Decimal("0.0002")


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("quantity", -1),
        ("quantity", 0),
        ("quantity", True),
        ("quantity", "1e9999"),
        ("limit_price", float("nan")),
        ("limit_price", 0),
        ("quote_timestamp", float("inf")),
        ("quote_timestamp", True),
        ("side", []),
        ("side", "SHORT"),
        ("expected_probability", 1.1),
        ("expected_probability", "NaN"),
    ],
)
def test_nonfinite_invalid_and_untrusted_fields_never_reach_ledger(
    tmp_path: Path,
    public: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    with pytest.raises(TradingError):
        service.submit("alice", intent(**{field: value}))
    assert service.orders("alice") == []


def test_intent_cannot_supply_secrets_or_execution_mode(
    tmp_path: Path, public: dict[str, Any]
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    for field in ("mode", "api_key", "tenant_id", "fee_rate", "skip_risk"):
        with pytest.raises(TradingError, match="unsupported_intent_fields"):
            service.submit("alice", intent(**{field: "hidden"}))


@pytest.mark.parametrize("age", [16, -3])
def test_stale_and_future_client_quotes_rejected(
    tmp_path: Path, public: dict[str, Any], age: float
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    with pytest.raises(TradingError, match="stale_or_future_client_quote"):
        service.submit("alice", intent(quote_timestamp=time.time() - age))


def test_price_spread_order_daily_position_kill_and_sell_guards(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", max_position_quote=15, max_daily_quote=50)
    with pytest.raises(TradingError, match="price_deviation"):
        service.submit("alice", intent(limit_price=60000))
    public.update(ask="65000")
    with pytest.raises(TradingError, match="spread"):
        service.submit("alice", intent(limit_price=65000))
    public.update(ask="50010")
    with pytest.raises(TradingError, match="max_order"):
        service.submit("alice", intent(quantity="0.001"))
    with pytest.raises(TradingError, match="no_short"):
        service.submit("alice", intent(side="SELL", limit_price=50000))
    service.submit("alice", intent())
    with pytest.raises(TradingError, match="max_position"):
        service.submit("alice", intent(key="two"))
    service.set_kill("alice", True)
    reopened = TradingService(service.path)
    assert reopened.status("alice")["kill_enabled"] is True
    assert reopened.status("bob")["kill_enabled"] is False
    with pytest.raises(TradingError, match="kill_switch"):
        reopened.submit("alice", intent(key="three"))
    daily = TradingService(tmp_path / "daily.sqlite", max_daily_quote=15)
    daily.submit("alice", intent())
    with pytest.raises(TradingError, match="max_daily"):
        daily.submit("alice", intent(key="next"))


def test_paper_partial_fill_and_non_crossed_ioc_expiry(
    tmp_path: Path, public: dict[str, Any]
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    public["size"] = "0.0001"
    result = service.submit("alice", intent())
    assert result["status"] == "EXPIRED" and Decimal(result["filled_qty"]) == Decimal("0.0001")
    missed = service.submit("alice", intent(key="missed", limit_price=50000))
    assert missed["status"] == "EXPIRED" and Decimal(missed["filled_qty"]) == 0
    assert Decimal(service.portfolio("alice")["positions"][0]["quantity"]) == Decimal("0.0001")


@pytest.mark.parametrize("v2", [False, True])
def test_polymarket_canonical_slug_outcome_token_quote_and_paper_costs(
    tmp_path: Path,
    public: dict[str, Any],
    v2: bool,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    public["market"] = market(v2=v2)
    discovery = service.discover_markets()
    assert discovery[0]["outcomes"][0]["token_id"] == _TOKEN
    assert discovery[0]["prices_loaded"] is False
    quote = service.quote("polymarket", "bitcoin-above-100000")
    assert {q["token_id"] for q in quote["outcomes"]} == {_TOKEN, _OTHER}
    assert quote["version"] == ("v2" if v2 else "v1")
    token = service.quote("polymarket", _TOKEN)
    assert token["bid"] == 0.49 and token["ask"] == 0.5
    assert token["timestamp_kind"] == "exchange" and token["timestamp"] <= time.time()
    bought = service.submit("alice", intent(venue="polymarket"))
    assert Decimal(bought["fee_quote"]) == Decimal("0.175")
    sold = service.submit(
        "alice", intent(venue="polymarket", key="sell", side="SELL", limit_price="0.49")
    )
    assert Decimal(sold["fee_quote"]) == Decimal("0.17493")
    assert Decimal(service.portfolio("alice")["cash"]["polymarket"]["amount"]) == Decimal(
        "999.55007"
    )
    with pytest.raises(TradingError, match="explicit_polymarket_token"):
        service.submit(
            "alice", intent(venue="polymarket", key="ambiguous", instrument="bitcoin-above-100000")
        )


def test_polymarket_stale_identity_and_disabled_market_fail_closed(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    public["stamp"] = time.time() - 20
    with pytest.raises(TradingError, match="not_ready"):
        service.submit("alice", intent(venue="polymarket"))
    public.update(stamp=time.time(), market=market(closed=True))
    with pytest.raises(TradingError, match="not_ready"):
        service.submit("alice", intent(venue="polymarket"))


class FakeExecution:
    venue = "binance"
    environment = "testnet"
    account_id = "dedicated-test-account"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail, self.sends, self.lookups = fail, 0, 0
        self.result: dict[str, Any] | None = None

    def balances(self, instrument: str) -> dict[str, Any]:
        return {"cash": "100", "quantity": "1"}

    def submit(self, client_id: str, order: dict[str, Any]) -> dict[str, Any]:
        self.sends += 1
        if self.fail:
            raise TimeoutError("api_key=do-not-store-secret")
        return {
            "status": "FILLED",
            "filled_qty": "0.0002",
            "quote_qty": "10.002",
            "fee_quote": "0.010002",
            "exchange_id": "123",
        }

    def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
        self.lookups += 1
        return self.result


def test_execution_requires_server_binding_matching_mode_and_separate_tenant_account(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    assert not service.status("alice")["execution_enabled"]
    with pytest.raises(TradingError, match="not_configured"):
        service.submit("alice", intent())
    adapter = FakeExecution()
    with pytest.raises(TradingError, match="explicit_matching"):
        service.bind_execution("alice", "binance", adapter)
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    with pytest.raises(TradingError, match="another_tenant"):
        service.bind_execution("bob", "binance", adapter, acknowledge=True)
    with pytest.raises(TradingError, match="not_configured"):
        service.submit("bob", intent())
    assert service.submit("alice", intent())["mode"] == "testnet"
    assert adapter.sends == 1
    paper = TradingService(service.path)
    assert paper.orders("alice") == []


def test_timeout_unknown_survives_restart_blocks_new_order_never_resends_and_reconciles_once(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    adapter = FakeExecution(fail=True)
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    order = intent()
    result = service.submit("alice", order)
    assert result["status"] == "UNKNOWN"
    assert "secret" not in json.dumps(result)
    assert service.submit("alice", order)["status"] == "UNKNOWN" and adapter.sends == 1
    with pytest.raises(TradingError, match="unresolved"):
        service.submit("alice", intent(key="new"))
    assert service.reconcile("alice")["unresolved_orders"] == 1
    assert adapter.lookups == 1 and adapter.sends == 1
    restarted = TradingService(service.path, mode="testnet")
    restarted.bind_execution("alice", "binance", adapter, acknowledge=True)
    adapter.result = {
        "status": "FILLED",
        "filled_qty": "0.0002",
        "quote_qty": "10.002",
        "fee_quote": "0.010002",
        "exchange_id": "123",
    }
    assert restarted.reconcile("alice")["unresolved_orders"] == 0
    assert restarted.reconcile("alice")["orders"] == []
    assert adapter.sends == 1
    assert Decimal(restarted.portfolio("alice")["positions"][0]["quantity"]) == Decimal("0.0002")


def test_live_limits_kill_and_external_cash_also_enforced(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    adapter = FakeExecution()
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    with pytest.raises(TradingError, match="max_order"):
        service.submit("alice", intent(quantity="0.001"))
    with pytest.raises(TradingError, match="no_short"):
        service.submit("alice", intent(side="SELL", limit_price="50000"))
    service.set_kill("alice", True)
    with pytest.raises(TradingError, match="kill_switch"):
        service.submit("alice", intent())
    assert adapter.sends == 0


def test_private_preflight_exception_is_sanitized_and_no_order_is_reserved(
    tmp_path: Path,
    public: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    adapter = FakeExecution()
    service.bind_execution("alice", "binance", adapter, acknowledge=True)

    def fail(instrument: str) -> Any:
        raise RuntimeError("Authorization: fixture-sensitive-token")

    monkeypatch.setattr(adapter, "balances", fail)
    with pytest.raises(TradingError, match="preflight_unavailable") as error:
        service.submit("alice", intent())
    assert "fixture-sensitive-token" not in str(error.value)
    assert error.value.__suppress_context__
    assert service.orders("alice") == [] and adapter.sends == 0


def test_server_factory_never_enables_from_mode_only_and_reports_only_missing_names(
    tmp_path: Path,
) -> None:
    assert trading_from_env(tmp_path / "default.sqlite", environ={}).mode == "paper"
    service = trading_from_env(
        tmp_path / "live.sqlite",
        environ={
            "QT_AGENT_HUB_TRADING_MODE": "live",
            "QT_AGENT_HUB_EXECUTION_TENANT": "alice",
            "POLYMARKET_PRIVATE_KEY": "do-not-output-this-value",
        },
    )
    status = service.status("alice")
    assert status["execution_enabled"] is False
    assert "POLYMARKET_API_SECRET" in status["venues"]["polymarket"]["missing_settings"]
    assert "do-not-output" not in json.dumps(status)
    assert service.status("bob")["venues"]["polymarket"]["missing_settings"] == [
        "server_execution_binding"
    ]


def test_testnet_factory_attaches_only_server_selected_tenant_without_private_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trading, "BinanceTestnetAdapter", lambda key, secret: FakeExecution())
    service = trading_from_env(
        tmp_path / "testnet.sqlite",
        environ={
            "QT_AGENT_HUB_TRADING_MODE": "testnet",
            "QT_AGENT_HUB_EXECUTION_TENANT": "alice",
            "QT_AGENT_HUB_EXECUTION_ENABLED": "true",
            "BINANCE_TESTNET_API_KEY": "not-real",
            "BINANCE_TESTNET_API_SECRET": "not-real",
        },
    )
    assert service.status("alice")["execution_enabled"]
    assert not service.status("bob")["execution_enabled"]


def test_secret_file_requires_private_permissions_and_never_leaks_values(tmp_path: Path) -> None:
    secret = tmp_path / "secrets.json"
    secret.write_text(
        json.dumps({"QT_AGENT_HUB_TRADING_MODE": "live", "POLYMARKET_PRIVATE_KEY": "private"})
    )
    secret.chmod(0o644)
    with pytest.raises(TradingError, match="TRADING_SECRETS_FILE"):
        trading_from_env(
            tmp_path / "ledger.sqlite", environ={"QT_AGENT_HUB_TRADING_SECRETS_FILE": str(secret)}
        )
    secret.chmod(0o600)
    service = trading_from_env(
        tmp_path / "ledger.sqlite", environ={"QT_AGENT_HUB_TRADING_SECRETS_FILE": str(secret)}
    )
    assert service.mode == "live"
    assert "private" not in json.dumps(service.status("local"))


def test_binance_testnet_fixed_origin_hmac_ioc_and_no_ccxt_dependency(
    production_transport: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    production_transport["host"] = "testnet.binance.vision"
    caplog.set_level(logging.INFO, logger="httpx")
    adapter = BinanceTestnetAdapter("fixture-key", "fixture-secret")
    assert adapter.environment == "testnet"
    assert adapter.balances("BTCUSDT") == {"cash": "100", "quantity": "0.5"}
    assert "fixture-key" not in adapter.account_id
    result = adapter.submit("qth_fixture", intent())
    assert result["status"] == "FILLED"
    assert adapter.lookup("qth_fixture", {})["exchange_id"] == "567"
    assert "testnet.binance.vision/api/v3/account" in caplog.text
    assert "signature=" not in caplog.text and "fixture-secret" not in caplog.text
    assert len([r for r in production_transport["requests"] if r.method == "POST"]) == 1
    adapter._ORIGIN = "https://api.binance.com"
    with pytest.raises(TradingError, match="blocked_binance_environment_origin"):
        adapter.balances("BTCUSDT")
    assert all(r.url.host == "testnet.binance.vision" for r in production_transport["requests"])


def test_binance_quotes_match_execution_environment(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    for mode, environment, host in (
        ("paper", "production-public", "data-api.binance.vision"),
        ("live", "production-public", "data-api.binance.vision"),
        ("testnet", "testnet-public", "testnet.binance.vision"),
    ):
        quote = TradingService(tmp_path / f"{mode}.sqlite", mode=mode).quote("binance", "BTCUSDT")
        assert quote["environment"] == environment
        assert quote["source"] == f"https://{host}/api/v3/ticker/bookTicker"


def test_polymarket_sg_close_only_never_uses_server_location_to_open_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Official https://docs.polymarket.com/api-reference/geoblock, checked
    # 2026-10-21, lists SG as close-only on both the API and frontend.
    adapter = object.__new__(PolymarketAdapter)
    adapter.user_country = "SG"
    monkeypatch.setattr(
        trading, "_get_json", lambda *args: pytest.fail("must reject before geoblock request")
    )
    with pytest.raises(TradingError, match="restricted_or_close_only"):
        adapter._eligibility()


def test_polymarket_live_geoblock_fails_closed_and_does_not_use_foreign_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = object.__new__(PolymarketAdapter)
    adapter.user_country = "CN"
    monkeypatch.setattr(trading, "_get_json", lambda *_: {"blocked": True, "country": "CN"})
    with pytest.raises(TradingError, match="geoblock"):
        adapter._eligibility()
    monkeypatch.setattr(trading, "_get_json", lambda *_: {"blocked": False, "country": "PT"})
    with pytest.raises(TradingError, match="country_must_match"):
        adapter._eligibility()
    monkeypatch.setattr(trading, "_get_json", lambda *_: {"blocked": False, "country": "CN"})
    adapter._eligibility()


def test_polymarket_sdk_fixture_uses_explicit_asset_and_fak_without_accepting_fill_as_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class Signed:
        order_type: str = "GTC"

    class Client:
        def create_limit_order(self, **kwargs: Any) -> Signed:
            assert kwargs == {"asset_id": _TOKEN, "side": "BUY", "price": "0.50", "size": "10"}
            return Signed()

        def post_order(self, signed: Signed) -> Any:
            assert signed.order_type == "FAK"
            return SimpleNamespace(ok=True, order_id="0x123")

    adapter = object.__new__(PolymarketAdapter)
    adapter._client = Client()
    monkeypatch.setattr(adapter, "_eligibility", lambda: None)
    result = adapter.submit("local-client-id", intent(venue="polymarket"))
    assert result["status"] == "SETTLEMENT_PENDING" and result["exchange_id"] == "0x123"
    assert result["filled_qty"] == 0
    assert adapter.lookup("unknown", {"exchange_id": ""}) is None


def test_execution_invalid_fill_stays_unknown_and_accounting_transaction_rolls_back(
    tmp_path: Path,
    public: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    adapter = FakeExecution()
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    monkeypatch.setattr(
        adapter,
        "submit",
        lambda *_: {"status": "FILLED", "filled_qty": "0.0002", "quote_qty": "12"},
    )
    assert service.submit("alice", intent())["status"] == "UNKNOWN"
    assert service.portfolio("alice")["positions"] == []
    assert service.portfolio("alice")["cash"]["binance"]["amount"] == "100"
    with sqlite3.connect(service.path) as db:
        raw = db.execute("SELECT data FROM hub_trade_orders").fetchone()[0]
    assert "secret" not in raw


@pytest.fixture
def production_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "requests": [],
        "post_status": 200,
        "lookup_missing": False,
        "uid": 12345,
        "fees": "0.010002",
        "host": "api.binance.com",
    }
    original = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        assert request.url.scheme == "https" and request.url.host == state["host"]
        assert request.url.path.startswith("/api/v3/")
        route = request.url.path.rsplit("/", 1)[-1]
        params = parse_qs(request.url.query.decode())
        if route == "time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000)})
        if route == "exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "status": "TRADING",
                            "isSpotTradingAllowed": True,
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "minPrice": "0.01",
                                    "maxPrice": "1000000",
                                    "tickSize": "0.01",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.00001",
                                    "maxQty": "100",
                                    "stepSize": "0.00001",
                                },
                                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                            ],
                        }
                    ]
                },
            )
        assert request.headers["X-MBX-APIKEY"] in {"fixture-key", "second-key"}
        query, signature = request.url.query.decode().rsplit("&signature=", 1)
        assert hmac.compare_digest(
            signature, hmac.new(b"fixture-secret", query.encode(), hashlib.sha256).hexdigest()
        )
        assert params["recvWindow"] == ["5000"]
        assert abs(int(params["timestamp"][0]) - int(time.time() * 1000)) < 2000
        if route == "account":
            return httpx.Response(
                200,
                json={
                    "uid": state["uid"],
                    "canTrade": True,
                    "balances": [{"asset": "BTC", "free": "0.5"}, {"asset": "USDT", "free": "100"}],
                },
            )
        if route == "myTrades":
            return httpx.Response(
                200,
                json=[
                    {
                        "orderId": 567,
                        "qty": "0.0002",
                        "commission": state["fees"],
                        "commissionAsset": "USDT",
                    }
                ],
            )
        if route == "order":
            if request.method == "POST":
                assert params["type"] == ["LIMIT"] and params["timeInForce"] == ["IOC"]
                client_id = params["newClientOrderId"][0]
                if state["post_status"] != 200:
                    return httpx.Response(
                        state["post_status"], json={"code": -1007, "msg": "fixture-secret"}
                    )
            else:
                assert "origClientOrderId" in params
                client_id = params["origClientOrderId"][0]
                if state["lookup_missing"]:
                    return httpx.Response(400, json={"code": -2013, "msg": "not found"})
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderId": 567,
                    "clientOrderId": client_id,
                    "side": "BUY",
                    "status": "FILLED",
                    "executedQty": "0.0002",
                    "cummulativeQuoteQty": "10.002",
                },
            )
        raise AssertionError(route)

    monkeypatch.setattr(
        trading.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    return state


def test_production_binance_fixed_origin_hmac_ioc_lot_filters_and_trade_fee_reconciliation(
    production_transport: dict[str, Any],
) -> None:
    adapter = BinanceSpotAdapter("fixture-key", "fixture-secret")
    assert adapter.environment == "live"
    assert adapter.balances("BTCUSDT") == {"cash": "100", "quantity": "0.5"}
    order = intent(quantity="0.00020999", limit_price="50010.009")
    result = adapter.submit("qth_fixture", order)
    assert result["status"] == "FILLED" and Decimal(result["fee_quote"]) == Decimal("0.010002")
    posted = [r for r in production_transport["requests"] if r.method == "POST"]
    params = parse_qs(posted[0].url.query.decode())
    assert params["quantity"] == ["0.00020"] and params["price"] == ["50010.00"]
    assert adapter.lookup("qth_fixture", {})["exchange_id"] == "567"
    with pytest.raises(TradingError, match="notional"):
        adapter.submit("tiny", intent(quantity="0.00001"))
    assert len([r for r in production_transport["requests"] if r.method == "POST"]) == 1
    with pytest.raises(TradingError, match="blocked_binance"):
        adapter._request("withdraw", method="POST", private=True)


def test_production_binance_ambiguous_post_never_retries_and_recovers_via_client_id(
    tmp_path: Path,
    public: dict[str, Any],
    production_transport: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "live.sqlite", mode="live")
    adapter = BinanceSpotAdapter("fixture-key", "fixture-secret")
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    production_transport["post_status"] = 503
    order = intent()
    result = service.submit("alice", order)
    assert result["status"] == "UNKNOWN"
    assert "fixture-secret" not in json.dumps(result)
    assert service.submit("alice", order)["status"] == "UNKNOWN"
    assert len([r for r in production_transport["requests"] if r.method == "POST"]) == 1
    production_transport["lookup_missing"] = True
    assert service.reconcile("alice")["unresolved_orders"] == 1
    production_transport["lookup_missing"] = False
    assert service.reconcile("alice")["unresolved_orders"] == 0
    assert len([r for r in production_transport["requests"] if r.method == "POST"]) == 1


def test_binance_uid_binding_prevents_different_api_keys_sharing_one_account_between_tenants(
    tmp_path: Path,
    production_transport: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="live")
    first = BinanceSpotAdapter("fixture-key", "fixture-secret")
    second = BinanceSpotAdapter("second-key", "fixture-secret")
    service.bind_execution("alice", "binance", first, acknowledge=True)
    with pytest.raises(TradingError, match="another_tenant"):
        service.bind_execution("bob", "binance", second, acknowledge=True)


def test_binance_production_factory_only_explicit_mode_credentials_and_budget(
    tmp_path: Path,
    production_transport: dict[str, Any],
) -> None:
    service = trading_from_env(
        tmp_path / "ledger.sqlite",
        environ={
            "QT_AGENT_HUB_TRADING_MODE": "live",
            "QT_AGENT_HUB_EXECUTION_TENANT": "alice",
            "QT_AGENT_HUB_EXECUTION_ENABLED": "true",
            "QT_AGENT_HUB_EXECUTION_BUDGET": "40",
            "BINANCE_API_KEY": "fixture-key",
            "BINANCE_API_SECRET": "fixture-secret",
        },
    )
    assert service.status("alice")["venues"]["binance"]["ready"]
    assert service.portfolio("alice")["cash"]["binance"]["amount"] == "40.0"
    assert not any(request.method == "POST" for request in production_transport["requests"])


def test_real_production_adapter_obeys_shared_limits_and_kill_before_any_post(
    tmp_path: Path,
    public: dict[str, Any],
    production_transport: dict[str, Any],
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="live", max_position_quote=15)
    service.bind_execution(
        "alice", "binance", BinanceSpotAdapter("fixture-key", "fixture-secret"), acknowledge=True
    )
    with pytest.raises(TradingError, match="max_order"):
        service.submit("alice", intent(quantity="0.001"))
    service.submit("alice", intent())
    with pytest.raises(TradingError, match="max_position"):
        service.submit("alice", intent(key="extra"))
    service.set_kill("alice", True)
    with pytest.raises(TradingError, match="kill_switch"):
        service.submit("alice", intent(key="killed"))
    assert len([r for r in production_transport["requests"] if r.method == "POST"]) == 1


def test_signed_binance_request_urls_are_redacted_from_httpx_logs(
    production_transport: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    BinanceSpotAdapter("fixture-key", "fixture-secret").balances("BTCUSDT")
    assert "api.binance.com/api/v3/account" in caplog.text
    assert "signature=" not in caplog.text and "fixture-secret" not in caplog.text


def test_stale_reconciliation_cannot_overwrite_or_double_book_a_concurrent_fill(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    send_started, allow_send, lookup_started, allow_lookup = Event(), Event(), Event(), Event()

    class RacedExecution(FakeExecution):
        def submit(self, client_id: str, order: dict[str, Any]) -> dict[str, Any]:
            send_started.set()
            assert allow_send.wait(3)
            return super().submit(client_id, order)

        def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
            lookup_started.set()
            assert allow_lookup.wait(3)
            return None

    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    service.bind_execution("alice", "binance", RacedExecution(), acknowledge=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(service.submit, "alice", intent())
        assert send_started.wait(3)
        reconciliation = pool.submit(service.reconcile, "alice")
        assert lookup_started.wait(3)
        allow_send.set()
        assert sending.result(timeout=3)["status"] == "FILLED"
        allow_lookup.set()
        assert reconciliation.result(timeout=3)["unresolved_orders"] == 0
    assert service.orders("alice")[0]["status"] == "FILLED"
    assert Decimal(service.portfolio("alice")["positions"][0]["quantity"]) == Decimal("0.0002")


def test_timeout_after_reconciliation_cannot_downgrade_terminal_state_or_book_twice(
    tmp_path: Path,
    public: dict[str, Any],
) -> None:
    started, allow_timeout = Event(), Event()

    class TimeoutAfterFill(FakeExecution):
        def submit(self, client_id: str, order: dict[str, Any]) -> dict[str, Any]:
            started.set()
            assert allow_timeout.wait(3)
            raise TimeoutError("fixture-only-sensitive-signature")

        def lookup(self, client_id: str, record: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "FILLED",
                "filled_qty": "0.0002",
                "quote_qty": "10.002",
                "fee_quote": "0.010002",
                "exchange_id": "123",
            }

    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    service.bind_execution("alice", "binance", TimeoutAfterFill(), acknowledge=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        sending = pool.submit(service.submit, "alice", intent())
        assert started.wait(3)
        assert service.reconcile("alice")["unresolved_orders"] == 0
        allow_timeout.set()
        assert sending.result(timeout=3)["status"] == "FILLED"
    assert service.orders("alice")[0]["accounted"] is True
    assert service.reconcile("alice")["orders"] == []
    assert Decimal(service.portfolio("alice")["positions"][0]["quantity"]) == Decimal("0.0002")


def test_stale_execution_fence_has_no_market_read_order_or_paper_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite")
    monkeypatch.setattr(
        service, "quote", lambda *_: pytest.fail("stale worker may not read quotes")
    )
    with pytest.raises(LeaseLostError, match="no_order_sent"):
        service.submit("alice", intent(), lease_guard=lambda: False)
    assert service.orders("alice") == [] and service.portfolio("alice")["positions"] == []


def test_fence_rechecked_after_balance_read_before_reserving_order(
    tmp_path: Path,
    public: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="testnet")
    adapter = FakeExecution()
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    active = [True]

    def balance(instrument: str) -> dict[str, Any]:
        active[0] = False
        return {"cash": "100", "quantity": "1"}

    monkeypatch.setattr(adapter, "balances", balance)
    with pytest.raises(LeaseLostError):
        service.submit("alice", intent(), lease_guard=lambda: active[0])
    assert service.orders("alice") == [] and adapter.sends == 0


def test_production_binance_checks_fence_after_metadata_before_post(
    tmp_path: Path,
    public: dict[str, Any],
    production_transport: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TradingService(tmp_path / "ledger.sqlite", mode="live")
    adapter = BinanceSpotAdapter("fixture-key", "fixture-secret")
    service.bind_execution("alice", "binance", adapter, acknowledge=True)
    active = [True]
    prepare = adapter._prepare

    def lose_after_metadata(order: dict[str, Any]) -> Any:
        result = prepare(order)
        active[0] = False
        return result

    monkeypatch.setattr(adapter, "_prepare", lose_after_metadata)
    result = service.submit("alice", intent(), lease_guard=lambda: active[0])
    assert result["status"] == "REJECTED"
    assert result["rejection_reason"] == "execution_lease_lost_no_order_sent"
    assert not any(row.method == "POST" for row in production_transport["requests"])
    assert service.status("alice")["unresolved_orders"] == 0
    assert all(Decimal(row["quantity"]) == 0 for row in service.portfolio("alice")["positions"])


def test_polymarket_checks_fence_after_signing_before_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = [True]

    class Client:
        def create_limit_order(self, **kwargs: Any) -> Any:
            active[0] = False
            return SimpleNamespace()

        def post_order(self, signed: Any) -> Any:
            pytest.fail("lost fence must not broadcast signed order")

    adapter = object.__new__(PolymarketAdapter)
    adapter._client = Client()
    monkeypatch.setattr(adapter, "_eligibility", lambda: None)
    with pytest.raises(LeaseLostError):
        adapter.submit_guarded("fixture", intent(venue="polymarket"), lambda: active[0])


def test_optional_official_sdk_exact_types_and_signatures_when_installed() -> None:
    sdk = pytest.importorskip("polymarket", reason="optional pinned production SDK not installed")
    assert trading.version("polymarket-client") == "0.13.0"
    import inspect

    assert "credentials" in inspect.signature(sdk.SecureClient.create).parameters
    assert "asset_type" in inspect.signature(sdk.SecureClient.get_balance_allowance).parameters
    assert "asset_id" in inspect.signature(sdk.SecureClient.create_limit_order).parameters
    assert "signed_order" in inspect.signature(sdk.SecureClient.post_order).parameters
    creds = sdk.ApiKeyCreds(key="fixture", secret="fixture", passphrase="fixture")
    assert creds.key == "fixture"
    signed = sdk.SignedOrder(
        builder="0x" + "0" * 64,
        expiration=0,
        maker="0x" + "1" * 40,
        maker_amount=5_000_000,
        metadata="0x" + "0" * 64,
        order_type="GTC",
        salt=1,
        side="BUY",
        signature="0x" + "0" * 130,
        signature_type=0,
        signer="0x" + "1" * 40,
        taker_amount=10_000_000,
        timestamp=1,
        token_id=_TOKEN,
    )
    assert replace(signed, order_type="FAK").order_type == "FAK"
    adapter = PolymarketAdapter(
        "0x" + "1" * 64, "0x" + "2" * 40, "fixture", "fixture", "fixture", user_country="NZ"
    )
    assert adapter.environment == "live" and adapter._client is None
