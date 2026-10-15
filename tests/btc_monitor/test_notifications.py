from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from qt.btc_monitor.notifications import AsyncTelegram
from qt.btc_monitor.store import SnapshotStore


def healthy():
    return {
        "data_ready": True,
        "channels": {
            "trade": {"ready": True, "receive_age_seconds": 0, "event_age_seconds": 0},
            "quote": {
                "ready": True,
                "receive_age_seconds": 0,
                "event_age_seconds": None,
                "source_time_known": False,
            },
            "candle": {"ready": True, "event_age_seconds": 60},
        },
    }


def snapshot(revision=1, state="CONFIRMED"):
    return {
        "published_at": 1000,
        "health": healthy(),
        "recent_events": [
            {
                "event_id": "event-1",
                "revision": revision,
                "state": state,
                "as_of": 1000,
                "expires_at": 1100,
            }
        ],
    }


def notice(**overrides):
    return dict(
        {
            "event_id": "event-1",
            "revision": 1,
            "notification_class": "anomaly",
            "text": "BTC research signal; no execution",
            "expires_at": 1100,
            "positive": True,
        },
        **overrides,
    )


def saved(store):
    with sqlite3.connect(store.database_path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM outbox").fetchone())


@pytest_asyncio.fixture
async def setup_store(tmp_path):
    now = [1000.0]
    store = SnapshotStore(tmp_path, clock=lambda: now[0])
    await store.start()
    await store.persist(snapshot(), [notice()])
    yield store, now
    await store.stop()


@pytest.mark.asyncio
async def test_api_acceptance_and_dedup_without_network(setup_store):
    store, now = setup_store
    requests = []

    def handler(request):
        requests.append(request)
        assert saved(store)["status"] == "sending"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = AsyncTelegram(
            store,
            SecretStr("token-secret"),
            SecretStr("private-chat"),
            client=client,
            clock=lambda: now[0],
        )
        assert await sender.run_once() == 1
        assert await sender.run_once() == 0
    assert len(requests) == 1
    assert saved(store)["status"] == "accepted"
    assert saved(store)["attempts"] == 1
    assert "token-secret" not in repr(sender)
    assert "private-chat" not in repr(sender)
    assert "token-secret" not in repr(vars(sender))
    assert "private-chat" not in repr(vars(sender))


@pytest.mark.asyncio
async def test_429_honors_larger_retry_after_and_then_accepts(setup_store):
    store, now = setup_store
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={"ok": False, "parameters": {"retry_after": 3}},
                headers={"Retry-After": "5"},
            )
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        assert await sender.run_once() == 1
        assert saved(store)["next_retry_at"] == 1005
        now[0] = 1004
        assert await sender.run_once() == 0
        now[0] = 1005
        assert await sender.run_once() == 1
    assert saved(store)["status"] == "accepted"
    assert saved(store)["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError, httpx.WriteError])
async def test_ambiguous_transport_outcome_never_retries(setup_store, exception, caplog):
    store, now = setup_store
    calls = []

    def handler(request):
        calls.append(request)
        raise exception("fake accepted-but-timeout token-secret private-chat", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = AsyncTelegram(
            store, "token-secret", "private-chat", client=client, clock=lambda: now[0]
        )
        assert await sender.run_once() == 1
        assert await sender.run_once() == 0
    assert len(calls) == 1
    assert saved(store)["status"] == "unknown"
    assert saved(store)["error_code"] == "transport_unknown"
    assert "token-secret" not in caplog.text
    assert "private-chat" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["INVALIDATED", "EXPIRED"])
async def test_new_revision_prevents_positive_network_call(setup_store, state):
    store, now = setup_store
    await store.persist(snapshot(2, state))

    def forbidden(request):
        pytest.fail("obsolete positive notification must never reach transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        sender = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        assert await sender.run_once() == 0
    assert saved(store)["status"] == "obsolete"


@pytest.mark.asyncio
async def test_expired_notice_never_calls_transport(setup_store):
    store, now = setup_store
    now[0] = 1100

    def forbidden(request):
        pytest.fail("expired notification must never reach transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        sender = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        assert await sender.run_once() == 0
    assert saved(store)["status"] == "expired"


@pytest.mark.asyncio
async def test_429_attempts_bounded(setup_store):
    store, now = setup_store
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, json={"parameters": {"retry_after": 1}})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        sender = AsyncTelegram(
            store, "token", "chat", client=client, max_attempts=2, clock=lambda: now[0]
        )
        await sender.run_once()
        now[0] += 1
        await sender.run_once()
        now[0] += 10
        assert await sender.run_once() == 0
    assert saved(store)["status"] == "failed"
    assert saved(store)["attempts"] == 2
    assert saved(store)["error_code"] == "attempt_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("delay,expected", [(100, "expired"), (4000, "failed")])
async def test_429_expiry_and_maximum_retry_horizon(setup_store, delay, expected):
    store, now = setup_store
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, json={"parameters": {"retry_after": delay}})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        sender = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        await sender.run_once()
    assert saved(store)["status"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (400, {"description": "private-chat token"}, "failed"),
        (503, {}, "unknown"),
        (200, [], "unknown"),
    ],
)
async def test_failure_diagnostics_never_copy_response_body(setup_store, status, body, expected):
    store, now = setup_store
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        sender = AsyncTelegram(store, "token", "private-chat", client=client, clock=lambda: now[0])
        await sender.run_once()
    record = saved(store)
    assert record["status"] == expected
    assert "private-chat" not in record["error_code"]


@pytest.mark.asyncio
async def test_cancellation_leaves_durable_unknown_claim_for_restart(setup_store):
    store, now = setup_store
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        task = asyncio.create_task(sender.run_once())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert saved(store)["status"] == "sending"


@pytest.mark.asyncio
async def test_sender_rechecks_revision_after_fetch(setup_store, monkeypatch):
    store, now = setup_store
    original = store.fetch_outbox

    async def fetch(*args, **kwargs):
        result = await original(*args, **kwargs)
        await store.persist(snapshot(2, "INVALIDATED"))
        return result

    monkeypatch.setattr(store, "fetch_outbox", fetch)
    transport = httpx.MockTransport(
        lambda request: pytest.fail("new invalidation must suppress send")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        sender = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        assert await sender.run_once() == 0
    assert saved(store)["status"] == "obsolete"


@pytest.mark.asyncio
async def test_httpx_info_request_logs_redact_bot_credential(setup_store, caplog):
    import logging

    store, now = setup_store
    caplog.set_level(logging.INFO, logger="httpx")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient(transport=transport) as client:
        sender = AsyncTelegram(
            store, "must-never-be-logged", "private-chat", client=client, clock=lambda: now[0]
        )
        await sender.run_once()
    assert "must-never-be-logged" not in caplog.text
    assert "private-chat" not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.asyncio
async def test_two_senders_cannot_send_same_outbox_claim(setup_store, monkeypatch):
    store, now = setup_store
    fetched = asyncio.Event()
    waiting = 0
    calls = []
    original = store.fetch_outbox

    async def both_fetch(*args, **kwargs):
        nonlocal waiting
        result = await original(*args, **kwargs)
        waiting += 1
        if waiting == 2:
            fetched.set()
        await fetched.wait()
        return result

    monkeypatch.setattr(store, "fetch_outbox", both_fetch)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        second = AsyncTelegram(store, "token", "chat", client=client, clock=lambda: now[0])
        result = await asyncio.gather(first.run_once(), second.run_once())
    assert sorted(result) == [0, 1]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_cancellation_closes_owned_client(setup_store, monkeypatch):
    store, now = setup_store
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("qt.btc_monitor.notifications.httpx.AsyncClient", lambda **kwargs: client)
    sender = AsyncTelegram(store, "token", "chat", clock=lambda: now[0])
    task = asyncio.create_task(sender.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.is_closed
