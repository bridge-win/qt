"""Atomic content-addressed point-in-time signal archive."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from btc_backtest.data.models import SHA256_PATTERN
from btc_backtest.errors import DataValidationError
from btc_backtest.signals.models import SignalObservation, SignalQuery


class _ArchivePointer(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    fingerprint: str = Field(pattern=SHA256_PATTERN)


class _ArchiveManifest(_ArchivePointer):
    observation_count: int = Field(ge=0)


class SignalStore:
    """Append immutable observations and query only currently knowable values."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._versions = root / "versions"
        self._pointer = root / "current.json"
        self._lock = root / ".append.lock"
        self._versions.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        observations: Iterable[SignalObservation],
    ) -> str:
        incoming = tuple(observations)
        if any(
            not isinstance(item, SignalObservation)
            for item in incoming
        ):
            raise DataValidationError(
                "signal archive accepts only SignalObservation values"
            )
        with _exclusive_lock(self._lock):
            existing = self._load_published()
            merged = _merge(existing, incoming)
            fingerprint = _fingerprint(merged)
            self._publish(merged, fingerprint)
            return fingerprint

    def query(
        self,
        query: SignalQuery,
        *,
        available_at: datetime,
    ) -> tuple[SignalObservation, ...]:
        as_of = _utc(available_at)
        observations = self._load_published()
        return tuple(
            observation
            for observation in observations
            if (
                observation.symbol == query.symbol
                and observation.horizon in query.horizons
                and (
                    not query.source_types
                    or observation.source_type in query.source_types
                )
                and query.start
                <= observation.effective_at
                < query.end
                and observation.observed_at <= as_of
                and observation.expires_at > as_of
            )
        )

    def _publish(
        self,
        observations: tuple[SignalObservation, ...],
        fingerprint: str,
    ) -> None:
        target = self._versions / fingerprint
        if not target.exists():
            with TemporaryDirectory(
                dir=self.root,
                prefix=".signals-",
            ) as temporary:
                candidate = Path(temporary) / fingerprint
                candidate.mkdir()
                data_path = candidate / "signals.parquet"
                manifest_path = candidate / "manifest.json"
                _frame(observations).to_parquet(data_path, index=False)
                manifest_path.write_text(
                    _ArchiveManifest(
                        fingerprint=fingerprint,
                        observation_count=len(observations),
                    ).model_dump_json(indent=2),
                    encoding="utf-8",
                )
                _fsync_file(data_path)
                _fsync_file(manifest_path)
                _fsync_directory(candidate)
                try:
                    candidate.replace(target)
                except OSError:
                    if not target.is_dir():
                        raise
                _fsync_directory(self._versions)

        pointer_candidate = self.root / f".current-{os.getpid()}.json"
        try:
            pointer_candidate.write_text(
                _ArchivePointer(
                    fingerprint=fingerprint,
                ).model_dump_json(),
                encoding="utf-8",
            )
            _fsync_file(pointer_candidate)
            pointer_candidate.replace(self._pointer)
            _fsync_directory(self.root)
        finally:
            pointer_candidate.unlink(missing_ok=True)

    def _load_published(self) -> tuple[SignalObservation, ...]:
        if not self._pointer.exists():
            return ()
        try:
            pointer = _ArchivePointer.model_validate_json(
                self._pointer.read_text(encoding="utf-8")
            )
            version = self._versions / pointer.fingerprint
            manifest = _ArchiveManifest.model_validate_json(
                (version / "manifest.json").read_text(encoding="utf-8")
            )
            if manifest.fingerprint != pointer.fingerprint:
                raise DataValidationError(
                    "signal archive manifest does not match pointer"
                )
            observations = _observations(
                pd.read_parquet(version / "signals.parquet")
            )
            if manifest.observation_count != len(observations):
                raise DataValidationError(
                    "signal archive count does not match manifest"
                )
            if _fingerprint(observations) != pointer.fingerprint:
                raise DataValidationError(
                    "signal archive fingerprint does not match content"
                )
            return observations
        except DataValidationError:
            raise
        except (OSError, ValueError, ValidationError) as error:
            raise DataValidationError(
                "published signal archive is corrupt"
            ) from error


def _merge(
    existing: tuple[SignalObservation, ...],
    incoming: tuple[SignalObservation, ...],
) -> tuple[SignalObservation, ...]:
    by_identity = {
        (item.provider, item.source_event_id): item
        for item in existing
    }
    for item in incoming:
        identity = (item.provider, item.source_event_id)
        prior = by_identity.get(identity)
        if prior is None:
            by_identity[identity] = item
            continue
        if prior.payload_sha256 != item.payload_sha256:
            raise DataValidationError(
                "conflicting signal payload for "
                f"{item.provider}/{item.source_event_id}"
            )
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                item.effective_at,
                item.observed_at,
                item.provider,
                item.source_event_id,
                item.id,
            ),
        )
    )


def _fingerprint(
    observations: tuple[SignalObservation, ...],
) -> str:
    payload = [
        item.model_dump(mode="json")
        for item in observations
    ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _frame(
    observations: tuple[SignalObservation, ...],
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": item.id,
                "source_event_id": item.source_event_id,
                "provider": item.provider,
                "source_type": item.source_type,
                "symbol": item.symbol,
                "horizon": item.horizon,
                "direction": str(item.direction),
                "confidence": str(item.confidence),
                "raw_value": (
                    str(item.raw_value)
                    if item.raw_value is not None
                    else None
                ),
                "observed_at": item.observed_at,
                "effective_at": item.effective_at,
                "expires_at": item.expires_at,
                "provenance": item.provenance,
                "payload_sha256": item.payload_sha256,
                "quality_flags": json.dumps(item.quality_flags),
            }
            for item in observations
        ],
        columns=(
            "id",
            "source_event_id",
            "provider",
            "source_type",
            "symbol",
            "horizon",
            "direction",
            "confidence",
            "raw_value",
            "observed_at",
            "effective_at",
            "expires_at",
            "provenance",
            "payload_sha256",
            "quality_flags",
        ),
    )


def _observations(frame: pd.DataFrame) -> tuple[SignalObservation, ...]:
    required = set(_frame(()).columns)
    if set(frame.columns) != required:
        raise DataValidationError(
            "signal archive columns do not match schema"
        )
    items: list[SignalObservation] = []
    for row in frame.to_dict(orient="records"):
        raw = row["raw_value"]
        items.append(
            SignalObservation(
                id=str(row["id"]),
                source_event_id=str(row["source_event_id"]),
                provider=str(row["provider"]),
                source_type=str(row["source_type"]),
                symbol=str(row["symbol"]),
                horizon=str(row["horizon"]),
                direction=Decimal(str(row["direction"])),
                confidence=Decimal(str(row["confidence"])),
                raw_value=(
                    None
                    if raw is None or bool(pd.isna(raw))
                    else Decimal(str(raw))
                ),
                observed_at=_datetime(row["observed_at"]),
                effective_at=_datetime(row["effective_at"]),
                expires_at=_datetime(row["expires_at"]),
                provenance=str(row["provenance"]),
                payload_sha256=str(row["payload_sha256"]),
                quality_flags=tuple(
                    str(value)
                    for value in json.loads(str(row["quality_flags"]))
                ),
            )
        )
    return tuple(items)


def _datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    return _utc(timestamp.to_pydatetime())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    return value.astimezone(timezone.utc)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
