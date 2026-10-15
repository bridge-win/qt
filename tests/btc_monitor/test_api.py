"""The authenticated workbench reads projections without owning monitor state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qt.btc_monitor.api import MAX_SNAPSHOT_BYTES, build_btc_monitor_router, read_projection
from qt.btc_monitor.models import (
    AnomalyDecision,
    ChannelHealth,
    DecisionRecord,
    Health,
    MonitorSnapshot,
)
from qt.workbench.api import WorkbenchSettings, create_workbench_app

NOW = 1_800_000_000.0


def snapshot(now: float = NOW) -> MonitorSnapshot:
    return MonitorSnapshot(
        config_version="fixture",
        boot_id="test-boot",
        published_at=now,
        health=Health(
            alive=True,
            data_ready=True,
            assessment_ready=True,
            status="ready",
            last_progress_at=now,
            last_persisted_at=now,
            coverage_seconds=60,
            channels={
                name: ChannelHealth(
                    last_event_at=None if name == "quote" else now,
                    last_receive_at=now,
                    event_age_seconds=None if name == "quote" else 0,
                    receive_age_seconds=0,
                    backlog_seconds=0,
                    ready=True,
                    source_time_known=name != "quote",
                )
                for name in ("trade", "quote", "candle")
            },
        ),
        micro={"price": 60000, "spread_bps": 1},
        assessment=DecisionRecord(
            assessment_id="test-assessment",
            as_of=now,
            expires_at=now + 5,
            state="CANDIDATE",
            reasons=["fixture only"],
        ),
    )


def write_snapshot(root: Path, value: MonitorSnapshot | dict[str, object]) -> bytes:
    root.mkdir(parents=True, exist_ok=True)
    raw = (
        value.model_dump_json() if isinstance(value, MonitorSnapshot) else json.dumps(value)
    ).encode()
    (root / "latest.json").write_bytes(raw)
    return raw


def test_missing_snapshot_get_never_creates_monitor_directory(tmp_path: Path) -> None:
    root = tmp_path / "not-created"
    app = FastAPI()
    app.include_router(build_btc_monitor_router(root), prefix="/api/v3")
    with TestClient(app) as client:
        first = client.get("/api/v3/btc-monitor")
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["read_reasons"] == ["snapshot_missing"]
        assert client.get("/api/v3/btc-monitor").json()["live_execution_enabled"] is False
    assert not root.exists()


def test_fresh_projection_is_read_only_and_has_explicit_market_identity(tmp_path: Path) -> None:
    original = write_snapshot(tmp_path, snapshot())
    response = read_projection(tmp_path, now=NOW + 1)
    assert response["read_status"] == "ok"
    assert response["snapshot"]["assessment"]["state"] == "CANDIDATE"
    assert response["snapshot"]["source"]["product"] == "spot"
    assert response["snapshot"]["source"]["price_type"] == "trade"
    assert response["snapshot"]["health"]["channels"]["trade"]["event_age_seconds"] == 1
    assert response["valid_until"] == NOW + 5
    assert (tmp_path / "latest.json").read_bytes() == original
    assert list(tmp_path.iterdir()) == [tmp_path / "latest.json"]


@pytest.mark.parametrize(
    "age,reason", [(11, "snapshot_stale"), (-3, "snapshot_future"), (6, "assessment_expired")]
)
def test_read_time_invalidates_positive_without_rewriting_history(
    tmp_path: Path, age: float, reason: str
) -> None:
    original = write_snapshot(tmp_path, snapshot())
    response = read_projection(tmp_path, now=NOW + age)
    assert response["read_status"] == "degraded"
    assert reason in response["read_reasons"]
    assert response["snapshot"]["assessment"]["state"] == "DATA_UNHEALTHY"
    assert response["snapshot"]["health"]["assessment_ready"] is False
    assert (tmp_path / "latest.json").read_bytes() == original


def test_fresh_heartbeat_cannot_refresh_old_assessment(tmp_path: Path) -> None:
    value = snapshot()
    value.assessment.as_of = NOW - 20
    value.assessment.expires_at = NOW + 5
    write_snapshot(tmp_path, value)
    response = read_projection(tmp_path, now=NOW)
    assert "assessment_stale" in response["read_reasons"]
    assert response["snapshot"]["assessment"]["state"] == "DATA_UNHEALTHY"


@pytest.mark.parametrize(
    "channel,field,value",
    [
        ("trade", "last_event_at", NOW - 6),
        ("quote", "last_receive_at", NOW - 6),
        ("trade", "last_event_at", NOW + 3),
    ],
)
def test_one_stale_or_future_channel_blocks_candidate(
    tmp_path: Path, channel: str, field: str, value: float
) -> None:
    projection = snapshot()
    setattr(projection.health.channels[channel], field, value)
    write_snapshot(tmp_path, projection)
    response = read_projection(tmp_path, now=NOW)
    assert f"{channel}_unready" in response["read_reasons"]
    assert response["snapshot"]["health"]["data_ready"] is False
    assert response["snapshot"]["assessment"]["state"] != "CANDIDATE"


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json}",
        b"[]",
        b'{"schema_version":99}',
        b"x" * (MAX_SNAPSHOT_BYTES + 1),
        b'{"private_secret":"do-not-reflect"}',
    ],
)
def test_malformed_incompatible_or_oversized_snapshot_is_harmless(
    tmp_path: Path, raw: bytes
) -> None:
    (tmp_path / "latest.json").write_bytes(raw)
    response = read_projection(tmp_path, now=NOW)
    assert response["snapshot"] is None
    assert response["read_reasons"] == ["snapshot_invalid"]
    assert "do-not-reflect" not in json.dumps(response)
    assert (tmp_path / "latest.json").read_bytes() == raw


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_nonfinite_nested_evidence_rejected(tmp_path: Path, number: str) -> None:
    # Keep this independent of serializer spacing/numeric representation.
    value = snapshot().model_dump(mode="json")
    value["micro"] = {"nested": "REPLACE_NUMBER"}
    raw = json.dumps(value).replace('"REPLACE_NUMBER"', number)
    (tmp_path / "latest.json").write_text(raw)
    assert read_projection(tmp_path, now=NOW)["snapshot"] is None


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "unrelated"
    secret.write_text("do-not-read")
    (tmp_path / "latest.json").symlink_to(secret)
    assert read_projection(tmp_path, now=NOW)["read_reasons"] == ["snapshot_invalid"]


def test_workbench_origin_auth_is_inherited(tmp_path: Path) -> None:
    settings = WorkbenchSettings(
        parquet_root=tmp_path / "parquet",
        state_root=tmp_path / "state",
        origin_client_id="test-id",
        origin_client_secret="test-secret",
    )
    with TestClient(create_workbench_app(settings)) as client:
        assert client.get("/api/v3/btc-monitor").status_code == 401
        response = client.get(
            "/api/v3/btc-monitor",
            headers={
                "CF-Access-Client-Id": "test-id",
                "CF-Access-Client-Secret": "test-secret",
                "X-QT-Access-Subject": "researcher@example.test",
            },
        )
        assert response.status_code == 200
        assert response.json()["read_reasons"] == ["snapshot_missing"]
    assert not (settings.state_root / "btc-monitor").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "not_alive",
        "unknown_trade_time",
        "unknown_candle_time",
        "backlog_negative",
        "backlog_stale",
        "backlog_missing",
    ],
)
def test_read_side_rechecks_health_invariants_and_withdraws_active_events(
    tmp_path: Path, kind: str
) -> None:
    value = snapshot()
    value.recent_events = [
        AnomalyDecision(
            event_id="wick-test",
            revision=1,
            state="CONFIRMED",
            as_of=NOW,
            expires_at=NOW + 120,
            direction="down",
            baseline=60000,
            extreme=59000,
            recovery_fraction=0.6,
            reason="fixture",
        )
    ]
    if kind == "not_alive":
        value.health.alive = False
    elif kind.startswith("unknown_"):
        value.health.channels[
            "trade" if kind == "unknown_trade_time" else "candle"
        ].source_time_known = False
    else:
        value.health.channels["trade"].backlog_seconds = {
            "backlog_negative": -1,
            "backlog_stale": 3,
            "backlog_missing": None,
        }[kind]
    original = write_snapshot(tmp_path, value)
    response = read_projection(tmp_path, now=NOW)
    assert response["read_status"] == "degraded"
    assert response["snapshot"]["assessment"]["state"] == "DATA_UNHEALTHY"
    assert response["snapshot"]["recent_events"][0]["state"] == "EXPIRED"
    assert (tmp_path / "latest.json").read_bytes() == original


def test_decision_expiry_does_not_misreport_fresh_market_source_as_dead(tmp_path: Path) -> None:
    value = snapshot()
    value.assessment.expires_at = NOW - 1
    write_snapshot(tmp_path, value)
    response = read_projection(tmp_path, now=NOW)
    assert response["snapshot"]["health"]["alive"] is True
    assert response["snapshot"]["health"]["data_ready"] is True
    assert response["snapshot"]["health"]["assessment_ready"] is False


def test_stricter_configured_clock_and_backlog_limits_are_honored(tmp_path: Path) -> None:
    value = snapshot()
    value.context["freshness_limits"] = {"max_future_seconds": 0.1, "max_backlog_seconds": 0.1}
    value.health.channels["trade"].last_event_at = NOW + 0.2
    value.health.channels["quote"].backlog_seconds = 0.2
    write_snapshot(tmp_path, value)
    response = read_projection(tmp_path, now=NOW)
    assert response["future_tolerance_seconds"] == 0.1
    assert "trade_unready" in response["read_reasons"]
    assert "quote_unready" in response["read_reasons"]
    assert response["snapshot"]["assessment"]["state"] != "CANDIDATE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("rest_url", "https://user:private-secret@example.invalid/api"),
        ("product", "perpetual"),
        ("price_type", "mark"),
    ],
)
def test_incompatible_or_credential_bearing_source_is_not_reflected(
    tmp_path: Path, field: str, value: str
) -> None:
    data = snapshot().model_dump(mode="json")
    data["source"][field] = value
    write_snapshot(tmp_path, data)
    response = read_projection(tmp_path, now=NOW)
    assert response["snapshot"] is None
    assert "private-secret" not in json.dumps(response)


def test_snapshot_cannot_enable_execution(tmp_path: Path) -> None:
    data = snapshot().model_dump(mode="json")
    data["live_execution_enabled"] = True
    write_snapshot(tmp_path, data)
    response = read_projection(tmp_path, now=NOW)
    assert response["snapshot"] is None
    assert response["live_execution_enabled"] is False
