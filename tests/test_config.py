from __future__ import annotations

from pathlib import Path

import pytest

from qt.core.config import load_settings


def test_load_settings_lets_nested_environment_override_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
execution:
  mode: paper
  live_enabled: false
  dry_run: true
  max_order_quote: 100.0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("QT_EXECUTION__MODE", "live")
    monkeypatch.setenv("QT_EXECUTION__LIVE_ENABLED", "true")
    monkeypatch.setenv("QT_EXECUTION__MAX_ORDER_QUOTE", "50")

    settings = load_settings(config)

    assert settings.execution.mode == "live"
    assert settings.execution.live_enabled is True
    assert settings.execution.dry_run is True
    assert settings.execution.max_order_quote == 50.0


def test_load_settings_uses_yaml_when_environment_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("QT_EXECUTION__MODE", raising=False)
    monkeypatch.delenv("QT_EXECUTION__LIVE_ENABLED", raising=False)
    monkeypatch.delenv("QT_EXECUTION__DRY_RUN", raising=False)
    monkeypatch.delenv("QT_EXECUTION__MAX_ORDER_QUOTE", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        """
execution:
  mode: live
  live_enabled: true
  dry_run: false
  max_order_quote: 25.0
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.execution.mode == "live"
    assert settings.execution.live_enabled is True
    assert settings.execution.dry_run is False
    assert settings.execution.max_order_quote == 25.0
