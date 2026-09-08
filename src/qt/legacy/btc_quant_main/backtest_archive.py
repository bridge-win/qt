# Migrated verbatim from btc_quant_main; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any


def read_backtest_payload(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as archive:
        payloads = []
        for name in archive.namelist():
            if not name.endswith(".json") or name.endswith("_config.json"):
                continue
            payload = json.loads(archive.read(name))
            if isinstance(payload, dict) and isinstance(payload.get("strategy"), dict):
                payloads.append(payload)

    if len(payloads) != 1:
        raise ValueError(f"expected one backtest result json in {zip_path}, found {len(payloads)}")
    return payloads[0]

