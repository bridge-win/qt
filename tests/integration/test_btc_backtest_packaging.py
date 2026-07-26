from __future__ import annotations

from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_qt_distribution_requires_exact_btc_backtest_version() -> None:
    metadata = tomllib.loads(read("pyproject.toml"))

    assert "btc-backtest==0.1.0" in metadata["project"]["dependencies"]


def test_start_bootstrap_installs_btc_backtest_before_qt() -> None:
    script = read("start.sh")

    btc_install = script.index('-e "packages/btc-backtest"')
    qt_install = script.index('-e ".[dev]"')
    assert btc_install < qt_install


def test_platform_dockerfile_builds_and_installs_both_distributions() -> None:
    source = read("Dockerfile.platform")

    assert "COPY packages/btc-backtest" in source
    assert "btc_backtest-0.1.0" in source
    assert "qt-0.1.0" in source
    assert "btc-backtest==0.1.0" in source
    assert "qt==0.1.0" in source


def test_platform_entrypoint_verifies_installed_package_versions() -> None:
    source = read("deploy/platform-entrypoint.sh")

    assert "import btc_backtest, qt" in source
    assert 'btc_backtest.__version__ != "0.1.0"' in source
    assert 'qt.__version__ != "0.1.0"' in source
