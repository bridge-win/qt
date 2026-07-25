from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btc_backtest.signals.models import SignalObservation, SignalQuery

UTC = timezone.utc
BASE = datetime(2024, 1, 1, tzinfo=UTC)


def utc(*, days: int = 0, hours: int = 0) -> datetime:
    return BASE + timedelta(days=days, hours=hours)


def observation(**updates: object) -> SignalObservation:
    values: dict[str, object] = {
        "id": "fixture:source-1",
        "source_event_id": "source-1",
        "provider": "fixture",
        "source_type": "sentiment",
        "symbol": "BTC/USD",
        "horizon": "1d",
        "direction": Decimal("0.5"),
        "confidence": Decimal("0.75"),
        "raw_value": Decimal("25"),
        "effective_at": utc(),
        "observed_at": utc(hours=1),
        "expires_at": utc(days=2),
        "provenance": "fixture://source-1",
        "payload_sha256": "0" * 64,
        "quality_flags": ("historical",),
    }
    values.update(updates)
    return SignalObservation.model_validate(values)


def full_query(**updates: object) -> SignalQuery:
    values: dict[str, object] = {
        "start": utc(),
        "end": utc(days=10),
        "symbol": "BTC/USD",
        "horizons": ("1d",),
    }
    values.update(updates)
    return SignalQuery.model_validate(values)
