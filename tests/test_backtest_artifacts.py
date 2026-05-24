from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from qt.backtest.artifacts import latest_backtest_summary, write_backtest_artifacts
from qt.backtest.engine import BacktestResult
from qt.backtest.metrics import Metrics


def test_write_backtest_artifacts(tmp_path: Path) -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    result = BacktestResult(
        equity_curve=pd.Series([100.0, 101.0, 102.0], index=idx, name="equity"),
        trades=pd.DataFrame([{"pnl": 2.0, "holding_bars": 1.0}]),
        signals=pd.DataFrame([{"ts": idx[1], "score": 0.8}]),
        metrics=Metrics(
            total_return=0.02,
            cagr=0.1,
            sharpe=1.5,
            sortino=2.0,
            calmar=1.0,
            max_drawdown=-0.01,
            win_rate=1.0,
            avg_win=2.0,
            avg_loss=0.0,
            profit_factor=float("inf"),
            num_trades=1,
            avg_holding_bars=1.0,
        ),
    )

    artifact = write_backtest_artifacts(
        result,
        tmp_path,
        ohlcv_key="binance_BTCUSDT_1h",
        initial_cash=100.0,
        sources={"ohlcv": "binance_BTCUSDT_1h"},
    )

    assert artifact.summary_path.exists()
    assert artifact.equity_path.exists()
    assert artifact.trades_path.exists()
    assert artifact.signals_path.exists()
    summary = json.loads(artifact.summary_path.read_text())
    assert summary["metrics"]["profit_factor"] is None
    latest = latest_backtest_summary(tmp_path)
    assert latest is not None
    assert latest["run_id"] == artifact.run_id
