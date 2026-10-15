"""Offline-only discovery checks; no real token or Telegram requests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


def load_helper():
    spec = importlib.util.spec_from_file_location(
        "telegram_chat_id", Path(__file__).parents[1] / "scripts" / "telegram_chat_id.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper():
    return load_helper()


def test_discovers_deduplicated_chat_ids_without_message_content(helper, monkeypatch):
    call = Mock(side_effect=[{"url": ""}, [
        {"message": {"chat": {"id": 123, "type": "private", "first_name": "Example"}, "text": "PRIVATE"}},
        {"message": {"chat": {"id": 123, "type": "private", "first_name": "Example"}}},
        {"channel_post": {"chat": {"id": -100456, "type": "channel", "title": "News"}}},
        {"message": {"chat": {"id": True, "type": "private"}}},
        None,
    ]])
    monkeypatch.setattr(helper, "api_call", call)
    result = helper.discover("FAKE_TOKEN")
    assert result == [
        {"chat_id": "123", "type": "private", "label": "Example"},
        {"chat_id": "-100456", "type": "channel", "label": "News"},
    ]
    assert "PRIVATE" not in json.dumps(result)
    assert [c.args[1] for c in call.call_args_list] == ["getWebhookInfo", "getUpdates"]


def test_webhook_stops_before_polling_and_never_prints_url(helper, monkeypatch):
    call = Mock(return_value={"url": "https://secret.example/credential"})
    monkeypatch.setattr(helper, "api_call", call)
    with pytest.raises(helper.DiscoveryError, match="has a webhook") as error:
        helper.discover("FAKE_TOKEN")
    assert "secret.example" not in str(error.value)
    assert call.call_count == 1


@pytest.mark.parametrize("status", [301, 401, 404, 409, 429, 500])
def test_transport_failures_hide_token_and_body(helper, monkeypatch, status):
    connection = Mock()
    connection.getresponse.return_value.status = status
    connection.getresponse.return_value.read.return_value = b"SECRET_TOKEN raw server body"
    monkeypatch.setattr(helper.http.client, "HTTPSConnection", Mock(return_value=connection))
    with pytest.raises(helper.DiscoveryError) as error:
        helper.api_call("SECRET_TOKEN", "getUpdates")
    assert "SECRET_TOKEN" not in str(error.value)
    assert "raw server body" not in str(error.value)
    connection.close.assert_called_once()


def test_tls_error_is_sanitized(helper, monkeypatch):
    connection = Mock()
    connection.request.side_effect = OSError("/botSECRET_TOKEN/getUpdates")
    monkeypatch.setattr(helper.http.client, "HTTPSConnection", Mock(return_value=connection))
    with pytest.raises(helper.DiscoveryError) as error:
        helper.api_call("SECRET_TOKEN", "getUpdates")
    assert "SECRET_TOKEN" not in str(error.value)


def test_request_has_no_offset_or_update_filter(helper, monkeypatch):
    connection = Mock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = b'{"ok": true, "result": []}'
    monkeypatch.setattr(helper.http.client, "HTTPSConnection", Mock(return_value=connection))
    assert helper.api_call("FAKE_TOKEN", "getUpdates") == []
    request = connection.request.call_args
    assert request.args[0] == "POST"
    assert request.kwargs["body"] == "timeout=0&limit=100"
    assert "offset" not in request.kwargs["body"]
    assert "allowed_updates" not in request.kwargs["body"]


def test_nonterminal_does_not_read_or_transmit_token(helper, monkeypatch, capsys):
    monkeypatch.setattr(helper.sys.stdin, "isatty", lambda: False)
    api = Mock()
    hidden = Mock()
    monkeypatch.setattr(helper, "api_call", api)
    monkeypatch.setattr(helper.getpass, "getpass", hidden)
    assert helper.main([]) == 1
    assert not capsys.readouterr().out
    hidden.assert_not_called()
    api.assert_not_called()


def test_main_escapes_labels_and_never_outputs_token(helper, monkeypatch, capsys):
    token = "123456:" + "A" * 35
    monkeypatch.setattr(helper.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(helper.getpass, "getpass", lambda _: token)
    monkeypatch.setattr(helper, "discover", lambda _: [{"chat_id": "123", "type": "private", "label": "\x1b[31m"}])
    assert helper.main([]) == 0
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert "\x1b" not in captured.out
    assert json.loads(captured.out)[0]["chat_id"] == "123"
