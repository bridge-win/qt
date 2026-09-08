"""Local registration for the native-only test subset.

The core Python 3.10/Intel job still exercises pure causal and contract tests.
Only tests that instantiate Nautilus' compiled engine require the ARM native
research environment.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "native_engine: requires the pinned NautilusTrader native runtime",
    )
    config.addinivalue_line(
        "markers",
        "performance: bounded multi-year native execution acceptance",
    )
