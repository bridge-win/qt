from __future__ import annotations

from pathlib import Path

from btc_backtest.strategies.registry import BUILTIN_STRATEGY_IDS

GUIDE = Path("docs/btc-backtest.md")
EVIDENCE = Path("docs/verification/btc-backtest-acceptance.md")

PUBLIC_COMMANDS = (
    "btc-backtest data sync",
    "btc-backtest data inspect",
    "btc-backtest strategies list",
    "btc-backtest strategies describe",
    "btc-backtest run",
    "btc-backtest run-custom",
    "btc-backtest validate",
    "btc-backtest signals collect",
    "btc-backtest signals top",
)

ACCEPTANCE_CRITERIA = (
    "Independent clean install",
    "Public API and CLI",
    "Live ten-year BTC/USD run",
    "No silent synthetic fallback",
    "Exact twenty algorithms",
    "Three additional strategies",
    "External custom entry point",
    "Five signal-source types",
    "Future-bar/signal isolation",
    "Complete robust validation",
    "Accounting and daily/hourly metrics",
    "QT regressions through adapter",
    "Static/build/live release gates",
    "Committed and pushed evidence",
)


def test_operator_guide_covers_public_workflows_and_all_algorithms() -> None:
    text = GUIDE.read_text(encoding="utf-8")

    for command in PUBLIC_COMMANDS:
        assert command in text
    for strategy_id in BUILTIN_STRATEGY_IDS:
        assert f"`{strategy_id}`" in text
    assert "not a profitability guarantee" in text.lower()
    assert "synthetic" in text.lower()
    assert "observed_at" in text
    assert "2016-07-25T00:00:00Z" in text
    assert "2026-07-25T00:00:00Z" in text


def test_acceptance_evidence_maps_every_design_criterion() -> None:
    text = EVIDENCE.read_text(encoding="utf-8")

    for criterion in ACCEPTANCE_CRITERIA:
        assert f"| {criterion} |" in text
    assert "Missing" not in text
    assert "Skipped" not in text
    assert "Indirect" not in text
    assert "abf01af" in text
    assert "2 passed in 74.26s" in text
    assert "2 passed in 44.73s" in text
