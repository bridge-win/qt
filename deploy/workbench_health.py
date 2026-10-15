"""Quiet, credential-safe Compose probes for the local research workbench."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def api_healthy() -> bool:
    client_id = os.environ.get("QT_WORKBENCH_ORIGIN_CLIENT_ID", "")
    secret = os.environ.get("QT_WORKBENCH_ORIGIN_CLIENT_SECRET", "")
    if not client_id or not secret:
        return False
    request = Request(
        "http://127.0.0.1:8877/api/v3/experiments?limit=1",
        headers={
            "CF-Access-Client-Id": client_id,
            "CF-Access-Client-Secret": secret,
            "X-QT-Access-Subject": "local:health-probe",
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status == 200 and isinstance(json.load(response), dict)
    except Exception:
        return False


def worker_healthy(database: Path = Path("/state/research.sqlite3")) -> bool:
    try:
        # Read-only URI never creates an empty database as a healthcheck side effect.
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5) as db:
            rows = db.execute(
                "SELECT heartbeat_at FROM research_workers WHERE worker_id = ? "
                "UNION ALL SELECT attempt_heartbeat_at FROM research_jobs "
                "WHERE claimed_by = ? AND status IN ('running', 'cancelling')",
                ("qt-orbstack-worker", "qt-orbstack-worker"),
            ).fetchall()
        now = datetime.now(timezone.utc)
        for (raw,) in rows:
            if isinstance(raw, str):
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if stamp.tzinfo is not None and -30 <= (now - stamp).total_seconds() < 90:
                    return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    selected = sys.argv[1] if len(sys.argv) == 2 else ""
    raise SystemExit(0 if (api_healthy() if selected == "api" else worker_healthy() if selected == "worker" else False) else 1)
