"""Immutable local signal archives and the legacy QT-intel adapter."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
from pydantic import TypeAdapter, ValidationError

from btc_backtest.errors import DataValidationError, ProviderError
from btc_backtest.signals.models import (
    SignalObservation,
    SignalProviderMetadata,
    SignalQuery,
)

_DATETIME_ADAPTER: Final = TypeAdapter(datetime)
_ARCHIVE_PROVIDER_ID: Final = "signal_archive"
_QT_INTEL_PROVIDER_ID: Final = "qt_intel"
_REQUIRED_ARCHIVE_FIELDS: Final = (
    "source_event_id",
    "source_type",
    "symbol",
    "horizon",
    "direction",
    "confidence",
    "effective_at",
    "observed_at",
    "expires_at",
    "provenance",
)


class SignalArchiveProvider:
    """Read a normalized CSV, JSON, or Parquet snapshot exactly once."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._content = _read_bytes(self.path)
        self._fingerprint = hashlib.sha256(self._content).hexdigest()
        rows = _load_rows(self.path, self._content)
        self._observations = _map_archive_rows(rows)
        source_types = tuple(
            sorted({item.source_type for item in self._observations})
        ) or ("archive",)
        self._symbols = frozenset(
            item.symbol for item in self._observations
        )
        self.metadata = SignalProviderMetadata(
            id=_ARCHIVE_PROVIDER_ID,
            description=f"Immutable signal archive {self.path.name}",
            source_types=source_types,
            historical=True,
            requires_credentials=False,
        )

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        self._verify_immutable()
        if self._symbols and query.symbol not in self._symbols:
            raise ProviderError(
                f"signal archive does not contain symbol {query.symbol}"
            )
        return tuple(
            item
            for item in self._observations
            if item.symbol == query.symbol
            and item.horizon in query.horizons
            and query.start <= item.effective_at < query.end
            and (
                not query.source_types
                or item.source_type in query.source_types
            )
        )

    def _verify_immutable(self) -> None:
        current = hashlib.sha256(_read_bytes(self.path)).hexdigest()
        if current != self._fingerprint:
            raise DataValidationError(
                f"signal archive changed after snapshot: {self.path}"
            )


class QTIntelArchiveProvider:
    """Map QT's persisted opportunity schema without importing QT."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._content = _read_bytes(self.path)
        self._fingerprint = hashlib.sha256(self._content).hexdigest()
        self._observations = _map_qt_intel(self.path, self._content)
        source_types = tuple(
            sorted({item.source_type for item in self._observations})
        ) or ("intel",)
        self._symbols = frozenset(
            item.symbol for item in self._observations
        )
        self.metadata = SignalProviderMetadata(
            id=_QT_INTEL_PROVIDER_ID,
            description=f"Immutable QT intelligence archive {self.path.name}",
            source_types=source_types,
            historical=True,
            requires_credentials=False,
        )

    def fetch(self, query: SignalQuery) -> tuple[SignalObservation, ...]:
        self._verify_immutable()
        if self._symbols and query.symbol not in self._symbols:
            raise ProviderError(
                f"QT intel archive does not contain symbol {query.symbol}"
            )
        return tuple(
            item
            for item in self._observations
            if item.symbol == query.symbol
            and item.horizon in query.horizons
            and query.start <= item.effective_at < query.end
            and (
                not query.source_types
                or item.source_type in query.source_types
            )
        )

    def _verify_immutable(self) -> None:
        current = hashlib.sha256(_read_bytes(self.path)).hexdigest()
        if current != self._fingerprint:
            raise DataValidationError(
                f"QT intel archive changed after snapshot: {self.path}"
            )


def _read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise DataValidationError(f"signal archive does not exist: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise DataValidationError(
            f"could not read signal archive: {path}"
        ) from error


def _load_rows(
    path: Path,
    content: bytes,
) -> tuple[dict[str, object], ...]:
    try:
        if path.suffix.casefold() == ".json":
            rows = TypeAdapter(
                list[dict[str, object]]
            ).validate_json(content)
        elif path.suffix.casefold() == ".csv":
            text = content.decode("utf-8")
            rows = [
                {key: value for key, value in row.items()}
                for row in csv.DictReader(io.StringIO(text))
            ]
        elif path.suffix.casefold() == ".parquet":
            table = pq.read_table(  # type: ignore[no-untyped-call]
                io.BytesIO(content)
            )
            raw_rows = table.to_pylist()
            rows = TypeAdapter(
                list[dict[str, object]]
            ).validate_python(raw_rows)
        else:
            raise DataValidationError(
                f"unsupported signal archive format: {path.suffix}"
            )
    except DataValidationError:
        raise
    except (
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        OSError,
    ) as error:
        raise DataValidationError(
            f"malformed signal archive: {path}"
        ) from error
    return tuple(rows)


def _map_archive_rows(
    rows: tuple[dict[str, object], ...],
) -> tuple[SignalObservation, ...]:
    observations: list[SignalObservation] = []
    identities: set[str] = set()
    for raw in rows:
        missing = [
            field
            for field in _REQUIRED_ARCHIVE_FIELDS
            if field not in raw
        ]
        if missing:
            raise DataValidationError(
                f"signal archive is missing {', '.join(missing)}"
            )
        source_event_id = _required_string(
            raw["source_event_id"],
            "source_event_id",
        )
        if source_event_id in identities:
            raise DataValidationError(
                f"duplicate signal archive event: {source_event_id}"
            )
        identities.add(source_event_id)
        canonical = _canonical_payload(raw)
        quality_flags = _quality_flags(raw.get("quality_flags"))
        try:
            item = SignalObservation(
                id=f"{_ARCHIVE_PROVIDER_ID}:{source_event_id}",
                source_event_id=source_event_id,
                provider=_ARCHIVE_PROVIDER_ID,
                source_type=_identifier(
                    raw["source_type"],
                    "source_type",
                ),
                symbol=_required_string(raw["symbol"], "symbol"),
                horizon=_required_string(raw["horizon"], "horizon"),
                direction=_decimal(raw["direction"], "direction"),
                confidence=_decimal(raw["confidence"], "confidence"),
                raw_value=_optional_decimal(raw.get("raw_value")),
                effective_at=_datetime(raw["effective_at"], "effective_at"),
                observed_at=_datetime(raw["observed_at"], "observed_at"),
                expires_at=_datetime(raw["expires_at"], "expires_at"),
                provenance=_required_string(
                    raw["provenance"],
                    "provenance",
                ),
                payload_sha256=hashlib.sha256(canonical).hexdigest(),
                quality_flags=(*quality_flags, "immutable_archive"),
            )
        except ValueError as error:
            raise DataValidationError(
                f"invalid signal archive event {source_event_id}"
            ) from error
        observations.append(item)
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.effective_at,
                item.source_event_id,
            ),
        )
    )


def _map_qt_intel(
    path: Path,
    content: bytes,
) -> tuple[SignalObservation, ...]:
    try:
        payload = TypeAdapter(dict[str, object]).validate_json(content)
        generated_at = _datetime(
            payload["generated_at"],
            "generated_at",
        )
        rows = TypeAdapter(
            list[dict[str, object]]
        ).validate_python(payload["opportunities"])
    except (KeyError, ValidationError, ValueError) as error:
        raise DataValidationError(
            f"malformed QT intel archive: {path}"
        ) from error
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise DataValidationError("QT intel archive has invalid count")
    if count != len(rows):
        raise DataValidationError("QT intel archive count mismatch")

    observations: list[SignalObservation] = []
    identities: set[str] = set()
    for raw in rows:
        kind = _identifier(raw.get("kind"), "kind")
        venue = _required_string(raw.get("venue"), "venue")
        raw_symbol = _required_string(raw.get("symbol"), "symbol")
        symbol = (
            "BTC/USD"
            if raw_symbol in ("BTC/USDT", "BTC/USD")
            else raw_symbol
        )
        effective_at = _datetime(raw.get("ts"), "ts")
        observed_at = _datetime(
            raw.get("observed_at", generated_at),
            "observed_at",
        )
        expires_at = _datetime(
            raw.get("expires_at", observed_at + timedelta(days=1)),
            "expires_at",
        )
        score = _decimal(raw.get("score"), "score")
        direction = _clip(score)
        confidence = _decimal(raw.get("confidence"), "confidence")
        timestamp = effective_at.astimezone(timezone.utc).isoformat()
        source_event_id = f"{kind}:{venue}:{raw_symbol}:{timestamp}"
        if source_event_id in identities:
            raise DataValidationError(
                f"duplicate QT intel event: {source_event_id}"
            )
        identities.add(source_event_id)
        try:
            item = SignalObservation(
                id=f"{_QT_INTEL_PROVIDER_ID}:{source_event_id}",
                source_event_id=source_event_id,
                provider=_QT_INTEL_PROVIDER_ID,
                source_type=kind,
                symbol=symbol,
                horizon=_required_string(
                    raw.get("horizon", "1d"),
                    "horizon",
                ),
                direction=direction,
                confidence=confidence,
                raw_value=score,
                effective_at=effective_at,
                observed_at=observed_at,
                expires_at=expires_at,
                provenance=f"{path.as_uri()}#{source_event_id}",
                payload_sha256=hashlib.sha256(
                    _canonical_payload(raw)
                ).hexdigest(),
                quality_flags=(
                    "historical",
                    "immutable_archive",
                    "qt_intel_schema_v1",
                ),
            )
        except ValueError as error:
            raise DataValidationError(
                f"invalid QT intel event {source_event_id}"
            ) from error
        observations.append(item)
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.effective_at,
                item.source_event_id,
            ),
        )
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError(f"invalid {field}")
    return value.strip()


def _identifier(value: object, field: str) -> str:
    parsed = _required_string(value, field)
    normalized = re.sub(r"[^a-z0-9]+", "_", parsed.casefold()).strip("_")
    if not normalized:
        raise DataValidationError(f"invalid {field}")
    return normalized


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DataValidationError(f"invalid {field}")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise DataValidationError(f"invalid {field}") from error
    if not parsed.is_finite():
        raise DataValidationError(f"invalid {field}")
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return _decimal(value, "raw_value")


def _datetime(value: object, field: str) -> datetime:
    try:
        parsed = _DATETIME_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise DataValidationError(f"invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataValidationError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _quality_flags(value: object) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(
            item.strip()
            for item in value.split(",")
            if item.strip()
        )
    try:
        flags = TypeAdapter(tuple[str, ...]).validate_python(value)
    except ValidationError as error:
        raise DataValidationError("invalid quality_flags") from error
    return flags


def _canonical_payload(raw: dict[str, object]) -> bytes:
    return json.dumps(
        raw,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def _clip(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(Decimal("1"), value))
