"""Constant-time verification for signed inbound signal observations."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from btc_backtest.errors import ProviderError
from btc_backtest.signals.models import (
    IDENTIFIER_PATTERN,
    SignalObservation,
)


class _WebhookPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    source_event_id: str = Field(min_length=1)
    source_type: str = Field(pattern=IDENTIFIER_PATTERN)
    symbol: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    direction: Decimal = Field(ge=-1, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    raw_value: Decimal | None = None
    observed_at: datetime
    effective_at: datetime
    expires_at: datetime
    provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_payload(self) -> _WebhookPayload:
        timestamps = (
            self.observed_at,
            self.effective_at,
            self.expires_at,
        )
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in timestamps
        ):
            raise ValueError("webhook timestamps must be timezone-aware")
        parsed = urlparse(self.provenance)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("webhook provenance must use safe HTTPS")
        return self


class WebhookVerifier:
    """Verify a timestamped HMAC-SHA256 body and normalize its payload."""

    def __init__(
        self,
        secret: bytes,
        max_age_seconds: int = 300,
        *,
        max_future_skew_seconds: int = 30,
        max_body_bytes: int = 65_536,
    ) -> None:
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("webhook secret must be non-empty bytes")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if max_future_skew_seconds < 0:
            raise ValueError(
                "max_future_skew_seconds must be non-negative"
            )
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self._secret = bytes(secret)
        self.max_age_seconds = max_age_seconds
        self.max_future_skew_seconds = max_future_skew_seconds
        self.max_body_bytes = max_body_bytes

    def verify(
        self,
        body: bytes,
        timestamp: str,
        signature: str,
        now: datetime,
    ) -> SignalObservation:
        if not isinstance(body, bytes) or not body:
            raise ProviderError("webhook body must be non-empty bytes")
        if len(body) > self.max_body_bytes:
            raise ProviderError("webhook body exceeds maximum size")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProviderError("webhook now must include a timezone")
        now_utc = now.astimezone(timezone.utc)
        signed_at = _signed_at(timestamp)
        age_seconds = (now_utc - signed_at).total_seconds()
        if (
            age_seconds > self.max_age_seconds
            or age_seconds < -self.max_future_skew_seconds
        ):
            raise ProviderError(
                "webhook timestamp is outside the accepted replay window"
            )
        supplied_signature = _signature_hex(signature)
        expected_signature = hmac.new(
            self._secret,
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(
            supplied_signature,
            expected_signature,
        ):
            raise ProviderError("webhook signature is invalid")

        try:
            payload = _WebhookPayload.model_validate_json(body)
        except (ValidationError, ValueError) as error:
            if _looks_malformed_json(error):
                message = "webhook body contains invalid JSON"
            elif "provenance" in str(error).casefold():
                message = "webhook provenance must use safe HTTPS"
            else:
                message = "webhook observation is invalid"
            raise ProviderError(message) from error
        observed_at = payload.observed_at.astimezone(timezone.utc)
        effective_at = payload.effective_at.astimezone(timezone.utc)
        expires_at = payload.expires_at.astimezone(timezone.utc)
        if observed_at > now_utc:
            raise ProviderError("webhook observed_at cannot be in the future")
        if effective_at > now_utc:
            raise ProviderError("webhook effective_at cannot be in the future")
        try:
            return SignalObservation(
                id=f"webhook:{payload.source_event_id}",
                source_event_id=payload.source_event_id,
                provider="webhook",
                source_type=payload.source_type,
                symbol=payload.symbol,
                horizon=payload.horizon,
                direction=payload.direction,
                confidence=payload.confidence,
                raw_value=payload.raw_value,
                observed_at=observed_at,
                effective_at=effective_at,
                expires_at=expires_at,
                provenance=payload.provenance,
                payload_sha256=hashlib.sha256(body).hexdigest(),
                quality_flags=(
                    "signed_webhook",
                    "hmac_sha256_verified",
                ),
            )
        except ValueError as error:
            raise ProviderError("webhook observation is invalid") from error


def _signed_at(timestamp: str) -> datetime:
    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        raise ProviderError("webhook timestamp is invalid")
    try:
        return datetime.fromtimestamp(
            int(timestamp),
            tz=timezone.utc,
        )
    except (OverflowError, OSError, ValueError) as error:
        raise ProviderError("webhook timestamp is invalid") from error


def _signature_hex(signature: str) -> str:
    normalized = (
        signature.removeprefix("sha256=").casefold()
        if isinstance(signature, str)
        else ""
    )
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ProviderError("webhook signature is invalid")
    return normalized


def _looks_malformed_json(error: Exception) -> bool:
    text = str(error).casefold()
    return "json" in text and (
        "invalid" in text
        or "eof" in text
        or "expected" in text
    )
