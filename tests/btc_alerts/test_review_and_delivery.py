from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from unittest.mock import Mock

import httpx
import pytest

from qt.btc_alerts import reviewer
from qt.btc_alerts.models import ReviewBatch
from qt.btc_alerts.service import AlertService
from qt.btc_alerts.telegram import Telegram

from .conftest import NOW, FakeMarket, approve

TOKEN = "123456:" + "fixture_only_" * 3
CHAT = "12345678"


def make_telegram(handler):
    return Telegram(TOKEN, CHAT, "fixture_bot", httpx.Client(transport=httpx.MockTransport(handler)))


def verified(store, telegram):
    store.put("recipient_verification", {"verified": True, "binding": telegram.binding})


def qualify(store, candidate):
    store.put("shadow_started:" + candidate.strategy_version, NOW - 8 * 86400)
    store.start_review_shadow(candidate.strategy_version, "fixture-only", NOW - 8 * 86400)
    store.put("reviewer_status", {"model": "fixture-only", "authenticated": True, "model_pinned": True, "at": NOW})
    store.save_validation(candidate.strategy_version, "trend", "long", NOW, {
        "status": "passed", "oos_events": 100, "ci95_lower": 0.2, "costs_included": True, "purged": True,
    })
    for index in range(20):
        timestamp = NOW - 8 * 86400 + index * 900
        past = candidate.model_copy(update={"candidate_id": "past-" + str(index), "bar_close": timestamp, "expires_at": timestamp + 600})
        store.add_candidate(past, NOW - 8 * 86400 + index)
        store.record(past.candidate_id, "review_rejected", "fixture", timestamp, {
            "verdict": "reject", "audit": {"model": "fixture-only", "model_pinned": True, "prompt_version": reviewer.PROMPT_VERSION},
        })
        with store.connection() as db:
            db.execute("UPDATE events SET status='review_rejected',outcome=? WHERE event_id=?", (json.dumps({"status": "labelled", "net_r": 0.1}), past.candidate_id))


@pytest.mark.parametrize("change", ["no_review", "different_model", "new_prompt", "unlabelled"])
def test_forward_promotion_requires_real_matching_model_reviews(store, candidate, change):
    qualify(store, candidate)
    assert not store.promotion_reasons(candidate, NOW)
    with store.connection() as db:
        if change == "no_review":
            db.execute("UPDATE events SET review=NULL")
        elif change == "different_model":
            store.put("reviewer_status", {"model": "other-model"})
        elif change == "new_prompt":
            db.execute("UPDATE events SET review=json_set(review,'$.audit.prompt_version','other-prompt')")
        else:
            db.execute("UPDATE events SET outcome=json_object('status','incomplete_outcome')")
    assert "forward_shadow_evidence_pending" in store.promotion_reasons(candidate, NOW)


@pytest.mark.parametrize("mutation", ["foreign_id", "foreign_citation", "missing", "opposing", "missing_card", "duplicate"])
def test_model_cannot_approve_by_inventing_or_omitting_evidence(candidate, mutation):
    payload = approve({"candidates": [candidate.model_dump()]})["result"]
    row = payload["reviews"][0]
    if mutation == "foreign_id":
        row["candidate_id"] = "another"
    elif mutation == "foreign_citation":
        row["supporting_evidence_ids"].append("invented_source")
    elif mutation == "missing":
        row["missing_evidence"] = ["funding"]
    elif mutation == "opposing":
        row["opposing_evidence_ids"] = ["market"]
    elif mutation == "missing_card":
        row["supporting_evidence_ids"] = ["market", "validation"]
    else:
        payload["reviews"].append(dict(row))
    with pytest.raises(ValueError):
        reviewer.validate_reviews(ReviewBatch.model_validate(payload), [candidate])


def test_cli_is_isolated_and_provider_output_is_not_logged(monkeypatch, candidate):
    monkeypatch.setenv("CODEX_API_KEY", "fixture-key-never-log")
    monkeypatch.setenv("BINANCE_API_KEY", "account-key-never-inherit")
    monkeypatch.setenv("QT_TELEGRAM_BOT_TOKEN", "token-never-inherit")
    monkeypatch.setenv("QT_BTC_ALERTS_CODEX_MODEL", "fixture-model")
    calls = []
    payload = {"candidates": [candidate.model_dump()]}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = command[command.index("--output-last-message") + 1]
        from pathlib import Path
        Path(output).write_text(json.dumps(approve(payload)["result"]))
        kwargs["stdout"].write(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 30}}) + "\n")
        return 0

    monkeypatch.setattr(reviewer, "execute_codex", run)
    result = reviewer.run_codex(payload)
    command, kwargs = calls[0]
    assert "BINANCE_API_KEY" not in kwargs["env"]
    assert "QT_TELEGRAM_BOT_TOKEN" not in kwargs["env"]
    assert "account-key-never-inherit" not in kwargs["input"]
    assert "fixture-key-never-log" not in json.dumps(result)
    assert kwargs["timeout"] == 100
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'web_search="disabled"' in command
    assert "features.code_mode_host=false" in command
    assert "features.browser_use=false" in command
    assert "features.shell_tool=false" in command
    assert result["model"] == "fixture-model"
    assert result["usage"]["input_tokens"] == 50
    assert not kwargs["cwd"].exists()


def test_missing_auth_never_launches_model(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    execute = Mock()
    monkeypatch.setattr(reviewer, "execute_codex", execute)
    with pytest.raises(RuntimeError, match="reviewer_auth_missing"):
        reviewer.run_codex({})
    execute.assert_not_called()


def test_timeout_terminates_cli_descendants(tmp_path):
    marker = tmp_path / "orphan-marker"
    child = "import time; from pathlib import Path; time.sleep(1); Path(" + repr(str(marker)) + ").touch()"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
    with (tmp_path / "stdout").open("w") as output, pytest.raises(subprocess.TimeoutExpired):
        reviewer.execute_codex(
            [sys.executable, "-c", parent], input="", env={"PATH": os.environ.get("PATH", "")},
            cwd=tmp_path, stdout=output, timeout=0.3,
        )
    time.sleep(1.1)
    assert not marker.exists()


def test_recipient_requires_fresh_private_challenge_from_same_person(store):
    nonce = "QT VERIFY fixture_nonce_123456"
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        value = {
            "getMe": {"is_bot": True, "username": "fixture_bot"},
            "getWebhookInfo": {"url": ""},
            "getUpdates": [{"message": {"text": nonce, "date": NOW, "chat": {"id": int(CHAT), "type": "private"},
                                        "from": {"id": int(CHAT), "is_bot": False}}}],
        }[method]
        return httpx.Response(200, json={"ok": True, "result": value})
    telegram = make_telegram(handler)
    assert not telegram.verified(store)
    assert telegram.verify(store, nonce, NOW)["verified"]
    assert telegram.verified(store)
    rotated = Telegram(TOKEN + "X", CHAT, "fixture_bot", telegram.client)
    assert not rotated.verified(store)


@pytest.mark.parametrize("kind", ["wrong_bot", "webhook", "foreign_sender", "group", "stale"])
def test_unverified_or_foreign_recipient_stays_blocked(store, kind):
    nonce = "QT VERIFY fixture_nonce_123456"
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            value = {"is_bot": True, "username": "other_bot" if kind == "wrong_bot" else "fixture_bot"}
        elif method == "getWebhookInfo":
            value = {"url": "https://example.test/webhook" if kind == "webhook" else ""}
        else:
            value = [{"message": {"text": nonce, "date": NOW - (601 if kind == "stale" else 0),
                "chat": {"id": int(CHAT), "type": "group" if kind == "group" else "private"},
                "from": {"id": 999 if kind == "foreign_sender" else int(CHAT), "is_bot": False}}}]
        return httpx.Response(200, json={"ok": True, "result": value})
    telegram = make_telegram(handler)
    with pytest.raises(ValueError):
        telegram.verify(store, nonce, NOW)
    assert not telegram.verified(store)


@pytest.mark.parametrize("kind,expected", [("timeout", "unknown"), ("500", "unknown"), ("bad_success", "unknown"), ("malformed_chat", "unknown"), ("malformed_429", "failed"), ("429", "pending"), ("403", "failed"), ("accepted", "accepted")])
def test_telegram_transport_has_conservative_outcomes(kind, expected):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("credential-bearing-url-must-never-escape", request=request)
        if kind == "accepted":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 101, "chat": {"id": int(CHAT)}}})
        if kind == "malformed_chat":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 101, "chat": "invalid"}})
        if kind == "malformed_429":
            return httpx.Response(429, json={"ok": False, "parameters": "invalid"})
        if kind == "429":
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 20}})
        return httpx.Response(200 if kind == "bad_success" else int(kind), json={"ok": False})
    result = make_telegram(handler).send("QT transport fixture")
    assert result["status"] == expected
    assert TOKEN not in json.dumps(result)
    assert "credential-bearing" not in json.dumps(result)


def test_shadow_and_unvalidated_approval_never_send(store):
    sent = []
    telegram = make_telegram(lambda request: sent.append(request) or httpx.Response(500))
    verified(store, telegram)
    service = AlertService(store, FakeMarket(), telegram, approve, lambda: NOW)
    service.tick()
    event = store.latest_events()[0]
    assert event["status"] == "evidence_pending"
    assert "validation_pending" in event["reason"]
    assert not sent
    service.tick()
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM review_batches").fetchone()[0] == 1


def test_validated_approved_candidate_passes_full_delivery_boundary_once(store, candidate):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101, "chat": {"id": int(CHAT)}}})
    telegram = make_telegram(handler)
    verified(store, telegram)
    qualify(store, candidate)
    revision, policy = store.policy()
    store.set_policy(revision, policy.model_copy(update={"mode": "notify"}), NOW)
    service = AlertService(store, FakeMarket(), telegram, approve, lambda: NOW)
    service.tick()
    assert len(sent) == 1
    assert "QT BTC" in sent[0]["text"]
    assert "高证据等级" in sent[0]["text"]
    assert store.event(candidate.candidate_id)["notification"]["message_id"] == 101
    service.dispatch()
    assert len(sent) == 1


def test_broken_model_or_stream_cannot_send(store):
    telegram = make_telegram(lambda request: pytest.fail("unexpected send"))
    verified(store, telegram)
    bad_reviewer = Mock(side_effect=RuntimeError("provider-private-output"))
    service = AlertService(store, FakeMarket(), telegram, bad_reviewer, lambda: NOW)
    service.tick()
    assert store.latest_events()[0]["status"] == "review_unavailable"
    assert "provider-private-output" not in json.dumps(store.latest_events())


def test_closed_bar_revision_during_review_prevents_delivery(store, candidate):
    telegram = make_telegram(lambda request: pytest.fail("revised snapshot must not send"))
    verified(store, telegram)
    qualify(store, candidate)
    revision, policy = store.policy()
    store.set_policy(revision, policy.model_copy(update={"mode": "notify"}), NOW)
    market = FakeMarket()
    def changing_reviewer(payload):
        bars, quote = market.current
        bars["15m"][-1] = bars["15m"][-1].model_copy(update={"volume": 999.0})
        market.current = bars, quote
        return approve(payload)
    AlertService(store, market, telegram, changing_reviewer, lambda: NOW).tick()
    assert store.event(candidate.candidate_id)["notification"]["status"] == "obsolete"


def test_transport_test_uses_same_hourly_limit_and_requires_verified_recipient(store):
    sent = []
    telegram = make_telegram(lambda request: sent.append(request) or httpx.Response(200, json={
        "ok": True, "result": {"message_id": 101, "chat": {"id": int(CHAT)}},
    }))
    first = store.request_test("fixture-request-one", NOW, "fixture")
    store.request_test("fixture-request-two", NOW, "fixture")
    service = AlertService(store, FakeMarket(), telegram, approve, lambda: NOW)
    service.dispatch()
    assert not sent
    verified(store, telegram)
    service.dispatch()
    assert len(sent) == 1
    assert store.event(first)["status"] == "accepted"
