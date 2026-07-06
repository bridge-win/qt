"""Plain-language dashboard summary tests."""

from __future__ import annotations

from qt.dashboard.server import _plain_summary


def test_summary_all_healthy_positive_pnl() -> None:
    strategies = [
        {"name": "dca", "status": "healthy"},
        {"name": "carry", "status": "healthy"},
    ]
    portfolios = [
        {"name": "dca", "realized_pnl": 100.0, "num_trades": 5},
        {"name": "carry", "realized_pnl": 50.0, "num_trades": 3},
    ]
    light, en, zh = _plain_summary(strategies, portfolios)
    assert light == "good"
    assert "2 strategy(ies) running" in en
    assert "up 150.00" in en
    assert "盈利" in zh


def test_summary_failed_is_red() -> None:
    strategies = [{"name": "dca", "status": "failed"}]
    portfolios = []
    light, en, _ = _plain_summary(strategies, portfolios)
    assert light == "bad"
    assert "1 failed" in en


def test_summary_degraded_is_yellow() -> None:
    strategies = [{"name": "dca", "status": "degraded"}]
    light, _, _ = _plain_summary(strategies, [])
    assert light == "warn"


def test_summary_negative_pnl() -> None:
    strategies = [{"name": "dca", "status": "healthy"}]
    portfolios = [{"name": "dca", "realized_pnl": -30.0, "num_trades": 2}]
    _, en, zh = _plain_summary(strategies, portfolios)
    assert "down 30.00" in en
    assert "亏损" in zh
