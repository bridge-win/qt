from __future__ import annotations

import pytest
from typer.testing import CliRunner

from qt.cli import app
from qt.core.config import Settings
from qt.execution.live import LiveBroker


class _FakeLiveBroker:
    def reconcile(self, symbol: str | None = None) -> dict[str, float]:
        return {"cash": 100.0, "position_qty": 0.01}


def test_live_preflight_requires_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    settings.execution.mode = "paper"
    settings.execution.live_enabled = False
    monkeypatch.setattr("qt.cli.load_settings", lambda config=None: settings)

    result = CliRunner().invoke(app, ["live", "preflight"])

    assert result.exit_code == 2
    assert "set it to 'live'" in result.output


def test_live_preflight_reports_dry_run_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    settings.execution.mode = "live"
    settings.execution.live_enabled = True
    settings.execution.dry_run = True
    monkeypatch.setattr("qt.cli.load_settings", lambda config=None: settings)
    monkeypatch.setattr(
        "qt.execution.live.LiveBroker.from_settings",
        lambda _settings: _FakeLiveBroker(),
    )

    result = CliRunner().invoke(app, ["live", "preflight"])

    assert result.exit_code == 0
    assert '"status": "dry-run"' in result.output
    assert '"symbol": "BTC/USDT"' in result.output


def test_cli_logging_does_not_retain_closed_capture_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated Click captures must not leave the live guardrail logger unusable."""

    settings = Settings()
    settings.execution.mode = "live"
    settings.execution.live_enabled = True
    settings.execution.dry_run = True
    monkeypatch.setattr("qt.cli.load_settings", lambda config=None: settings)
    monkeypatch.setattr(
        "qt.execution.live.LiveBroker.from_settings",
        lambda _settings: _FakeLiveBroker(),
    )

    runner = CliRunner()
    first = runner.invoke(app, ["live", "preflight"])
    second = runner.invoke(app, ["live", "preflight"])

    assert first.exit_code == 0
    assert second.exit_code == 0

    LiveBroker(Settings().execution, _client=_FakeLiveBroker())
    assert "live_broker_disabled" in capsys.readouterr().err
