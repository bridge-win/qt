from __future__ import annotations

import json

import httpx
import pytest

from qt.btc_alerts.market import BINANCE, PublicMarket

from .conftest import NOW, packet


def binance_rows(timeframe):
    return [[bar.open_time * 1000, str(bar.open), str(bar.high), str(bar.low),
             str(bar.close), str(bar.volume), (bar.open_time + 900) * 1000 - 1]
            for bar in packet()[0][timeframe]]


def test_only_closed_public_bars_are_loaded_without_account_headers():
    calls = []
    def handler(request):
        calls.append(request)
        rows = [*binance_rows("15m"), [NOW * 1000, "60000", "60001", "59999", "60000", "1"]]
        return httpx.Response(200, json=rows)
    market = PublicMarket(client=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: NOW)
    assert len(market.bars("spot", "15m", NOW)) == 300
    assert calls[0].url.host == "data-api.binance.vision"
    assert "X-MBX-APIKEY" not in calls[0].headers
    assert "authorization" not in calls[0].headers
    assert int(calls[0].url.params["limit"]) == 300
    market.bars("spot", "15m", NOW)
    assert int(calls[-1].url.params["limit"]) == 3


@pytest.mark.parametrize("kind", ["gap", "duplicate", "unordered", "nan"])
def test_invalid_provider_history_fails_closed(kind):
    rows = binance_rows("15m")
    if kind == "gap":
        rows.pop(250)
    elif kind == "duplicate":
        rows[-1] = rows[-2]
    elif kind == "unordered":
        rows[-2], rows[-1] = rows[-1], rows[-2]
    else:
        rows[-1][4] = "nan"
    market = PublicMarket(client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows))))
    with pytest.raises(ValueError):
        market.bars("spot", "15m", NOW)


def test_public_client_cannot_reach_account_order_or_arbitrary_endpoint():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("forbidden request sent")))
    market = PublicMarket(client=client)
    for endpoint in [BINANCE + "/account", BINANCE + "/order", "https://example.test/klines", "https://data-api.binance.vision.evil.test/api/v3/klines"]:
        with pytest.raises(ValueError, match="public_endpoint_not_allowed"):
            market.get(endpoint, {})


def test_quote_receipt_time_is_not_reused_from_before_slow_history_fetch():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"bidPrice": "60000", "askPrice": "60001"})))
    market = PublicMarket(client=client, clock=lambda: NOW + 12)
    from qt.btc_alerts.models import Policy
    assert market.quote("spot", NOW, Policy()).observed_at == NOW + 12


def test_revised_closed_bar_forces_full_rest_bootstrap():
    rows = binance_rows("15m")
    calls = []
    def handler(request):
        calls.append(int(request.url.params["limit"]))
        return httpx.Response(200, json=rows if len(calls) != 2 else rows[-3:])
    market = PublicMarket(client=httpx.Client(transport=httpx.MockTransport(handler)))
    market.bars("spot", "15m", NOW)
    rows[-1][5] = "999"
    with pytest.raises(ValueError, match="closed_bar_revision"):
        market.bars("spot", "15m", NOW)
    assert ("spot", "15m") not in market.cache
    assert market.bars("spot", "15m", NOW)[-1].volume == 999
    assert calls == [300, 3, 300]


@pytest.mark.parametrize("failure", ["old_snapshot", "wrong_market", "stale_quote", "unready_trade"])
def test_stream_readiness_checks_source_and_actual_receive_time(tmp_path, failure):
    source = {"exchange": "binance", "environment": "production", "product": "spot", "symbol": "BTCUSDT"}
    channels = {key: {"ready": True, "last_receive_at": NOW} for key in ("quote", "trade")}
    value = {"source": source, "published_at": NOW, "health": {"channels": channels}}
    if failure == "old_snapshot":
        value["published_at"] = NOW - 6
    elif failure == "wrong_market":
        source["environment"] = "testnet"
    elif failure == "stale_quote":
        channels["quote"]["last_receive_at"] = NOW - 6
    else:
        channels["trade"]["ready"] = False
    (tmp_path / "latest.json").write_text(json.dumps(value))
    assert not PublicMarket(tmp_path).stream_ready(NOW)
