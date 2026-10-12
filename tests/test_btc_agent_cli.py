"""CLI acceptance requires actual simulated fills, not successful API returns."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from qt.btc_agent import __main__ as cli
from qt.btc_agent import market
from qt.btc_agent.broker import Fill, Quote
from qt.btc_agent.config import AgentConfig
from qt.btc_agent.paper import PaperBroker
from qt.btc_agent.reviewer import Review
from qt.btc_agent.signals import Signal
from qt.btc_agent.store import Store


def _quote() -> Quote:
    return Quote(50_000.0, 50_001.0, int(time.time() * 1000))


class SmokeBroker(PaperBroker):
    def __init__(self, store: Store) -> None:
        super().__init__(store, _quote)
        self.buy_status = "FILLED"
        self.sell_status = "FILLED"
        self.base_fee = False
        self.submissions: list[str] = []

    def submit(
        self, client_id: str, side: Literal["buy", "sell"], qty: float, limit_price: float
    ) -> Fill:
        self.submissions.append(side)
        status = self.buy_status if side == "buy" else self.sell_status
        if status == "FILLED":
            fill = super().submit(client_id, side, qty, limit_price)
            if side == "buy" and self.base_fee:
                fill = replace(fill, fee_btc=fill.filled_qty * 0.001, fee_quote=0.0)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE paper_fills SET fill=? WHERE client_id=?",
                        (json.dumps(asdict(fill)), client_id),
                    )
            return fill
        fill = Fill(client_id, side, status, 0.0, 0.0)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO paper_fills VALUES (?,?)", (client_id, json.dumps(asdict(fill)))
            )
        return fill


@dataclass
class Smoke:
    config: AgentConfig
    store: Store
    broker: SmokeBroker
    approve: bool = True
    reviews: list[Signal] = field(default_factory=list)
    broker_creations: int = 0


@pytest.fixture
def smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Smoke]:
    for name in list(os.environ):
        if name.startswith("BTC_AGENT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("BTC_AGENT_MODE", "testnet")
    monkeypatch.setenv("BTC_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "unit-test-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "unit-test-secret")
    monkeypatch.setattr(sys, "argv", ["btc-agent", "testnet-round-trip"])
    config = AgentConfig()
    store = Store(config.database)
    broker = SmokeBroker(store)
    harness = Smoke(config, store, broker)

    def create_broker(api_key: str, api_secret: str) -> SmokeBroker:
        assert api_key == "unit-test-key"
        assert api_secret == "unit-test-secret"
        harness.broker_creations += 1
        return broker

    def review(candidate: Signal, url: str, timeout: int) -> Review:
        harness.reviews.append(candidate)
        return Review(approve=harness.approve, reason="Synthetic CLI acceptance review")

    def no_network() -> None:
        pytest.fail("CLI smoke must not request production market data")

    monkeypatch.setattr(cli, "BinanceTestnetBroker", create_broker)
    monkeypatch.setattr(cli, "review_signal", review)
    monkeypatch.setattr(market, "candles", no_network)
    monkeypatch.setattr(market, "quote", no_network)
    yield harness
    store.close()


@pytest.mark.parametrize("missing", ["key", "secret", "both"])
def test_round_trip_requires_both_dedicated_testnet_credentials(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    if missing in {"key", "both"}:
        monkeypatch.delenv("BINANCE_TESTNET_API_KEY")
    if missing in {"secret", "both"}:
        monkeypatch.delenv("BINANCE_TESTNET_API_SECRET")
    with pytest.raises(ValueError, match="Dedicated BINANCE_TESTNET"):
        cli.main()
    assert smoke.broker_creations == 0
    assert smoke.reviews == []
    assert smoke.broker.submissions == []


def test_cli_rejects_live_mode_before_any_broker_access(
    smoke: Smoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BTC_AGENT_MODE", "live")
    with pytest.raises(ValidationError):
        cli.main()
    assert smoke.broker_creations == 0


def test_round_trip_rejects_paper_mode(smoke: Smoke, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTC_AGENT_MODE", "paper")
    with pytest.raises(ValueError, match="requires BTC_AGENT_MODE=testnet"):
        cli.main()
    assert smoke.broker_creations == 0


def test_expired_buy_with_zero_fill_cannot_pass(
    smoke: Smoke, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke.broker.buy_status = "EXPIRED"
    assert cli.main() == 1
    assert smoke.broker.submissions == ["buy"]
    assert smoke.store.position().qty == 0
    assert smoke.store.pending() == []
    output = capsys.readouterr().out
    assert '"reason": "no_buy_fill"' in output
    assert '"status": "no_fill"' in output
    assert '"status": "executed"' not in output


def test_expired_sells_with_zero_fill_cannot_pass(
    smoke: Smoke, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke.broker.sell_status = "EXPIRED"
    assert cli.main() == 1
    assert smoke.broker.submissions == ["buy", "sell", "sell", "sell", "sell", "sell"]
    assert smoke.store.position().qty > 0
    assert smoke.store.pending() == []
    assert '"status": "incomplete"' in capsys.readouterr().out


@pytest.mark.parametrize("with_dust", [False, True])
def test_round_trip_passes_only_with_positive_buy_sell_and_no_active_position(
    smoke: Smoke, capsys: pytest.CaptureFixture[str], with_dust: bool
) -> None:
    smoke.broker.base_fee = with_dust
    assert cli.main() == 0
    assert smoke.broker.submissions == ["buy", "sell"]
    assert smoke.store.position().qty == 0
    assert smoke.store.pending() == []
    fills = [
        json.loads(row[0])
        for row in smoke.store.db.execute("SELECT fill FROM intents ORDER BY rowid")
    ]
    assert [fill["side"] for fill in fills] == ["buy", "sell"]
    assert all(fill["filled_qty"] > 0 for fill in fills)
    dust = smoke.store.get("dust_btc") or 0.0
    assert isinstance(dust, (float, int))
    assert dust > 0 if with_dust else dust == 0
    assert smoke.broker.balances()["BTC"] == pytest.approx(dust)
    assert len(smoke.reviews) == 1
    assert smoke.reviews[0].factors == {"smoke_test": True}
    assert '"status": "passed"' in capsys.readouterr().out


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_pending_order_fails_smoke_and_prevents_further_submissions(
    smoke: Smoke, side: str
) -> None:
    if side == "buy":
        smoke.broker.buy_status = "NEW"
    else:
        smoke.broker.sell_status = "NEW"
    assert cli.main() == 1
    assert len(smoke.store.pending()) == 1
    assert smoke.broker.submissions == (["buy"] if side == "buy" else ["buy", "sell"])


def test_preexisting_pending_intent_blocks_smoke_before_review(smoke: Smoke) -> None:
    smoke.store.intent("pending-before-smoke", "buy", 0.0004, 50_100, 1)
    with pytest.raises(ValueError, match="clean, dedicated testnet ledger"):
        cli.main()
    assert smoke.reviews == []
    assert smoke.broker.submissions == []


def test_codex_veto_stops_before_any_order(smoke: Smoke) -> None:
    smoke.approve = False
    assert cli.main() == 1
    assert smoke.broker.submissions == []
    assert smoke.store.pending() == []


def test_os_lock_rejects_second_worker_before_broker_access(smoke: Smoke) -> None:
    with (smoke.config.state_dir / "testnet.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another worker owns"):
            cli.main()
    assert smoke.broker_creations == 0
    assert smoke.broker.submissions == []
