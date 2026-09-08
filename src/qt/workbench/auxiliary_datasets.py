"""Attested, bounded auxiliary market-data references for native research.

Auxiliary data is registered by a server-owned parquet file plus manifest under
``<parquet_root>/auxiliary``.  Experiment callers select IDs only; they cannot
embed rows, filesystem paths, or claimed fingerprints in a job request.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from math import isfinite
from pathlib import Path

import pandas as pd

from qt.research.datasets import _immutable_file_fingerprint, _inspect_parquet

_KINDS = frozenset(
    {
        "mark_quotes",
        "funding_settlements",
        "open_interest",
        "liquidations",
        "mark_prices",
        "book_tickers",
        "taker_flow",
        "cross_venue_prices",
    }
)
_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "mark_quotes": frozenset({"available_at", "bid", "ask", "bid_size", "ask_size"}),
    "funding_settlements": frozenset({"available_at", "rate"}),
    "open_interest": frozenset({"available_at", "oi"}),
    "liquidations": frozenset({"available_at", "side", "price", "qty"}),
    "mark_prices": frozenset({"available_at", "mark", "index", "funding_rate"}),
    "book_tickers": frozenset({"available_at", "bid", "ask"}),
    "taker_flow": frozenset({"available_at", "buy_qty", "sell_qty"}),
    "cross_venue_prices": frozenset({"available_at", "venue", "price"}),
}


@dataclass(frozen=True)
class AuxiliaryReference:
    dataset_id: str
    kind: str
    fingerprint: str
    provider: str
    symbol: str
    market: str

    def as_spec(self) -> dict[str, str]:
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "provider": self.provider,
            "symbol": self.symbol,
            "market": self.market,
        }


class AuxiliaryDatasetCatalog:
    """Read only server-registered, manifest-attested auxiliary parquets."""

    def __init__(self, parquet_root: Path) -> None:
        self.root = parquet_root / "auxiliary"

    def list_datasets(self) -> list[dict[str, object]]:
        if not self.root.exists():
            return []
        result: list[dict[str, object]] = []
        for manifest_path in sorted(self.root.glob("*.manifest.json")):
            described = self._describe_manifest(manifest_path)
            if described is not None:
                result.append(described)
        return result

    def get(self, dataset_id: str) -> dict[str, object]:
        for item in self.list_datasets():
            if item["dataset_id"] == dataset_id:
                return item
        raise KeyError(dataset_id)

    def reference(self, dataset_id: str) -> AuxiliaryReference:
        item = self.get(dataset_id)
        if item.get("status") != "ready":
            raise ValueError(f"auxiliary dataset is not ready: {dataset_id}")
        fields = ("kind", "fingerprint", "provider", "symbol", "market")
        if any(not isinstance(item.get(field), str) for field in fields):
            raise ValueError(f"auxiliary dataset has an invalid immutable contract: {dataset_id}")
        return AuxiliaryReference(
            dataset_id=dataset_id,
            kind=str(item["kind"]),
            fingerprint=str(item["fingerprint"]),
            provider=str(item["provider"]),
            symbol=str(item["symbol"]),
            market=str(item["market"]),
        )

    def load(self, reference: Mapping[str, object]) -> tuple[AuxiliaryReference, pd.DataFrame]:
        dataset_id = _text(reference.get("dataset_id"), "auxiliary dataset_id")
        expected = _text(reference.get("fingerprint"), "auxiliary fingerprint")
        resolved = self.reference(dataset_id)
        if resolved.fingerprint != expected:
            raise ValueError(
                f"auxiliary dataset fingerprint changed: {dataset_id}; submit a new immutable request",
            )
        for field, actual in (
            ("kind", resolved.kind),
            ("provider", resolved.provider),
            ("symbol", resolved.symbol),
            ("market", resolved.market),
        ):
            if reference.get(field) != actual:
                raise ValueError(f"auxiliary dataset {field} changed: {dataset_id}")
        frame = pd.read_parquet(self.root / f"{dataset_id}.parquet")
        normalized = _normalize_auxiliary_frame(frame, resolved.kind)
        refreshed = self.reference(dataset_id)
        if refreshed.fingerprint != resolved.fingerprint:
            raise ValueError(
                f"auxiliary dataset changed while loading: {dataset_id}; submit a new immutable request",
            )
        return refreshed, normalized

    def _describe_manifest(self, manifest_path: Path) -> dict[str, object] | None:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        dataset_id = raw.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id or dataset_id != manifest_path.name.removesuffix(".manifest.json"):
            return None
        kind = raw.get("kind")
        if kind not in _KINDS:
            return None
        market = raw.get("market")
        provider = raw.get("provider")
        symbol = raw.get("symbol")
        if market not in {"spot", "perpetual"} or not all(
            isinstance(value, str) and value.strip() for value in (provider, symbol)
        ):
            return None
        path = self.root / f"{dataset_id}.parquet"
        inspected = _inspect_parquet(path)
        if inspected is None or inspected.get("status") != "ready":
            return {"dataset_id": dataset_id, "kind": kind, "status": "missing"}
        fingerprint = _immutable_file_fingerprint(path)
        declared = raw.get("fingerprint")
        if not isinstance(declared, str) or declared.removeprefix("sha256:") != fingerprint:
            return {"dataset_id": dataset_id, "kind": kind, "status": "invalid"}
        try:
            _normalize_auxiliary_frame(pd.read_parquet(path), kind)
        except ValueError:
            return {"dataset_id": dataset_id, "kind": kind, "status": "invalid"}
        return {
            "dataset_id": dataset_id,
            "kind": kind,
            "provider": provider,
            "symbol": symbol,
            "market": market,
            "fingerprint": f"sha256:{fingerprint}",
            "required_columns": sorted(_REQUIRED_COLUMNS[kind]),
            "status": "ready",
            "rows": inspected["rows"],
            "start": inspected["start"],
            "end": inspected["end"],
        }


def validate_auxiliary_selection(
    references: Sequence[AuxiliaryReference],
    *,
    primary_symbol: str,
    primary_market: str,
    required_kinds: frozenset[str],
) -> None:
    """Validate a bounded compatible reference set before durable enqueue."""

    kinds = [reference.kind for reference in references]
    if len(kinds) != len(set(kinds)):
        raise ValueError("only one auxiliary dataset may be selected for each kind")
    missing = sorted(required_kinds.difference(kinds))
    if missing:
        raise ValueError("experiment requires auxiliary datasets: " + ", ".join(missing))
    for reference in references:
        if reference.market != primary_market or reference.symbol != primary_symbol:
            raise ValueError(
                f"auxiliary dataset {reference.dataset_id} must match primary symbol and market",
            )


def _normalize_auxiliary_frame(frame: pd.DataFrame, kind: str) -> pd.DataFrame:
    missing = _REQUIRED_COLUMNS[kind].difference(frame.columns)
    if missing:
        raise ValueError(f"{kind} auxiliary parquet is missing columns: {', '.join(sorted(missing))}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{kind} auxiliary parquet requires an observed_at DatetimeIndex")
    result = frame.copy()
    result.index = result.index.tz_localize(timezone.utc) if result.index.tz is None else result.index.tz_convert(timezone.utc)
    available = pd.to_datetime(result["available_at"], utc=True, errors="coerce")
    if available.isna().any() or (available < result.index).any():
        raise ValueError(f"{kind} auxiliary availability must be aware and not precede observation")
    result["available_at"] = available
    for column in _REQUIRED_COLUMNS[kind].difference({"available_at", "side", "venue"}):
        values = pd.to_numeric(result[column], errors="coerce")
        if values.isna().any() or not all(isfinite(float(value)) for value in values):
            raise ValueError(f"{kind} auxiliary column {column} must be finite")
        result[column] = values
    if kind == "liquidations" and not result["side"].isin(("BUY", "SELL")).all():
        raise ValueError("liquidations auxiliary side must be BUY or SELL")
    if kind == "cross_venue_prices" and not result["venue"].map(lambda value: isinstance(value, str) and bool(value)).all():
        raise ValueError("cross_venue_prices auxiliary venue must be nonempty")
    return result.sort_values("available_at", kind="stable")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()
