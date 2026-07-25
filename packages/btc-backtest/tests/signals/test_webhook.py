from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from btc_backtest.errors import ProviderError
from btc_backtest.signals.webhook import WebhookVerifier

UTC = timezone.utc
NOW = datetime(2024, 1, 1, 0, 1, tzinfo=UTC)


def payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_event_id": "alert-1",
        "source_type": "alert",
        "symbol": "BTC/USD",
        "horizon": "1d",
        "direction": "0.7",
        "confidence": "0.8",
        "raw_value": "42",
        "observed_at": "2024-01-01T00:00:30Z",
        "effective_at": "2024-01-01T00:00:00Z",
        "expires_at": "2024-01-02T00:00:00Z",
        "provenance": "https://alerts.example/events/alert-1",
    }
    values.update(updates)
    return values


def body(**updates: object) -> bytes:
    return json.dumps(
        payload(**updates),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sign(secret: bytes, timestamp: str, raw_body: bytes) -> str:
    signed = timestamp.encode() + b"." + raw_body
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


def test_valid_webhook_maps_observation() -> None:
    raw_body = body()
    timestamp = "1704067260"
    signature = sign(b"secret", timestamp, raw_body)

    item = WebhookVerifier(b"secret").verify(
        raw_body,
        timestamp,
        signature,
        now=NOW,
    )

    assert item.provider == "webhook"
    assert item.source_event_id == "alert-1"
    assert item.direction == Decimal("0.7")
    assert item.confidence == Decimal("0.8")
    assert item.payload_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert "hmac_sha256_verified" in item.quality_flags


@pytest.mark.parametrize(
    "timestamp",
    ["1704066900", "1704067330"],
)
def test_webhook_rejects_stale_or_future_timestamp(
    timestamp: str,
) -> None:
    raw_body = body()

    with pytest.raises(ProviderError, match="timestamp"):
        WebhookVerifier(
            b"secret",
            max_age_seconds=300,
            max_future_skew_seconds=30,
        ).verify(
            raw_body,
            timestamp,
            sign(b"secret", timestamp, raw_body),
            now=NOW,
        )


def test_webhook_rejects_invalid_signature() -> None:
    with pytest.raises(ProviderError, match="signature"):
        WebhookVerifier(b"secret").verify(
            body(),
            "1704067260",
            "0" * 64,
            now=NOW,
        )


def test_webhook_accepts_sha256_signature_prefix() -> None:
    raw_body = body()
    timestamp = "1704067260"

    item = WebhookVerifier(b"secret").verify(
        raw_body,
        timestamp,
        f"sha256={sign(b'secret', timestamp, raw_body)}",
        now=NOW,
    )

    assert item.source_event_id == "alert-1"


def test_webhook_rejects_oversized_or_malformed_body() -> None:
    verifier = WebhookVerifier(b"secret", max_body_bytes=8)
    raw_body = body()
    timestamp = "1704067260"

    with pytest.raises(ProviderError, match="size"):
        verifier.verify(
            raw_body,
            timestamp,
            sign(b"secret", timestamp, raw_body),
            now=NOW,
        )

    malformed = b"{"
    with pytest.raises(ProviderError, match="JSON"):
        WebhookVerifier(b"secret").verify(
            malformed,
            timestamp,
            sign(b"secret", timestamp, malformed),
            now=NOW,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"observed_at": "2024-01-01T00:02:00Z"}, "observed_at"),
        ({"provenance": "http://alerts.example/1"}, "HTTPS"),
        ({"direction": "2"}, "invalid"),
        ({"source_event_id": ""}, "invalid"),
    ],
)
def test_webhook_rejects_unsafe_or_invalid_observation(
    updates: dict[str, object],
    message: str,
) -> None:
    raw_body = body(**updates)
    timestamp = "1704067260"

    with pytest.raises(ProviderError, match=message):
        WebhookVerifier(b"secret").verify(
            raw_body,
            timestamp,
            sign(b"secret", timestamp, raw_body),
            now=NOW,
        )


def test_webhook_rejects_naive_now_and_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="secret"):
        WebhookVerifier(b"")
    with pytest.raises(ValueError, match="max_age"):
        WebhookVerifier(b"secret", max_age_seconds=0)
    with pytest.raises(ProviderError, match="timezone"):
        WebhookVerifier(b"secret").verify(
            body(),
            "1704067260",
            "0" * 64,
            now=datetime(2024, 1, 1),
        )
