"""Immutable BTC-quant strategy catalog preserved inside QT.

The compressed source asset keeps all 50 signals and 100 profiles available
without requiring an adjacent legacy checkout at runtime.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import TypeAlias, cast

from qt.workbench.catalog_signals import add_catalog_indicators, evaluate_signal

JsonDict: TypeAlias = dict[str, object]


@lru_cache(maxsize=1)
def legacy_catalog() -> JsonDict:
    encoded = (
        files("qt.workbench")
        .joinpath("assets/catalog_profiles.json")
        .read_text(encoding="utf-8")
    )
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ValueError("catalog asset must be a JSON object")
    signals = parsed.get("signals")
    profiles = parsed.get("profiles")
    if not isinstance(signals, list) or len(signals) != 50:
        raise ValueError("catalog asset must contain exactly 50 signals")
    if not isinstance(profiles, list) or len(profiles) != 100:
        raise ValueError("catalog asset must contain exactly 100 profiles")
    return cast(JsonDict, parsed)


def catalog_summary() -> JsonDict:
    catalog = legacy_catalog()
    signals = catalog["signals"]
    profiles = catalog["profiles"]
    assert isinstance(signals, list)
    assert isinstance(profiles, list)
    return {
        "schema_version": catalog.get("schema_version"),
        "sha256": catalog.get("sha256"),
        "signal_count": len(signals),
        "profile_count": len(profiles),
        "families": sorted({str(row.get("family")) for row in profiles if isinstance(row, dict)}),
    }


def profile(profile_id: str) -> JsonDict:
    profiles = legacy_catalog()["profiles"]
    assert isinstance(profiles, list)
    for item in profiles:
        if isinstance(item, dict) and item.get("id") == profile_id:
            return cast(JsonDict, item)
    raise KeyError(profile_id)


__all__ = [
    "add_catalog_indicators",
    "catalog_summary",
    "evaluate_signal",
    "legacy_catalog",
    "profile",
]
