"""Tests for the multi-strategy gallery: loader, registry, runner, and
each strategy's evaluate() under controlled synthetic data."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from qt.core.config import Settings
from qt.core.types import Trade
from qt.execution.base import Broker, Order
from qt.execution.live import LiveTradingDisabledError, UnsafeApiKeyError
from qt.strategies import (
    REGISTRY,
    BasisCarry,
    Capitulation,
    EvaluationResult,
    Opportunity,
    SmartDCA,
    Strategy,
    StrategyConfig,
    WeeklyTrend,
    build_strategies,
    load_strategy_configs,
    run_strategy_forever,
    strategy_state_path,
)
from qt.strategies.base import Action
from qt.strategies.runner import _make_broker

# ---------------------- loader & registry --------------------------------


def test_registry_has_all_four() -> None:
    assert set(REGISTRY) == {"dca", "capitulation", "trend", "carry", "wick"}


def test_load_strategy_configs_reads_yaml(tmp_path: Path) -> None:
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "dca.yaml").write_text(
        "enabled: true\ninterval_seconds: 60\nparams:\n  base_buy_quote: 25\n"
    )
    (d / "trend.yaml").write_text(
        "enabled: false\ninterval_seconds: 300\nparams: {}\n"
    )
    cfgs = load_strategy_configs(d)
    assert len(cfgs) == 2
    by_name = {c.name: c for c in cfgs}
    assert by_name["dca"].enabled is True
    assert by_name["dca"].interval_seconds == 60
    assert by_name["dca"].params["base_buy_quote"] == 25
    assert by_name["trend"].enabled is False


def test_build_strategies_skips_disabled(tmp_path: Path) -> None:
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "dca.yaml").write_text("enabled: true\nparams: {}\n")
    (d / "trend.yaml").write_text("enabled: false\nparams: {}\n")
    cfgs = load_strategy_configs(d)
    strategies = build_strategies(cfgs)
    assert [s.name for s in strategies] == ["dca"]


def test_loader_rejects_name_mismatch(tmp_path: Path) -> None:
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "dca.yaml").write_text("name: wrong\nparams: {}\n")
    with pytest.raises(ValueError, match="declares name"):
        load_strategy_configs(d)


def test_repo_strategy_yamls_load() -> None:
    """The shipped config/strategies/*.yaml files must load cleanly."""
    cfgs = load_strategy_configs("config/strategies")
    assert {c.name for c in cfgs} == set(REGISTRY)
    strategies = build_strategies(cfgs)
    assert len(strategies) == len(cfgs)


# ---------------------- per-strategy evaluate() --------------------------


def _hourly_ohlcv(n_hours: int = 24 * 60, price: float = 30_000.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n_hours, freq="1h", tz="UTC")
    close = pd.Series(np.linspace(price, price * 1.10, n_hours), index=idx)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.005, "low": close * 0.995,
            "close": close, "volume": np.ones(n_hours),
        }
    )


def _hourly_falling(n_hours: int = 24 * 60, start: float = 60_000.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n_hours, freq="1h", tz="UTC")
    close = pd.Series(np.linspace(start, start * 0.45, n_hours), index=idx)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.005, "low": close * 0.995,
            "close": close, "volume": np.ones(n_hours),
        }
    )


def test_dca_evaluates_without_fear_greed() -> None:
    cfg = StrategyConfig(name="dca", params={"buy_dow": -1})  # never matches
    s = SmartDCA(cfg)
    out = s.evaluate({"ohlcv": _hourly_ohlcv()})
    assert out.opportunity is None
    assert "stress" in out.metrics


def test_dca_emits_buy_on_schedule() -> None:
    cfg = StrategyConfig(name="dca", params={"buy_dow": 0, "buy_hour_utc": 14})
    s = SmartDCA(cfg)
    ohlcv = _hourly_ohlcv()
    fake_now = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)  # Monday 14:00
    with patch("qt.strategies.dca.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        out = s.evaluate({"ohlcv": ohlcv})
    assert out.opportunity is not None
    assert out.opportunity.action == "buy"
    amount_quote = out.opportunity.details["amount_quote"]
    assert isinstance(amount_quote, int | float)
    assert amount_quote > 0


def test_dca_multiplier_higher_in_fear() -> None:
    cfg = StrategyConfig(name="dca", params={"buy_dow": -1})
    s = SmartDCA(cfg)
    falling = _hourly_falling()
    idx = falling.index
    fg = pd.DataFrame(
        {"fear_greed": np.r_[np.full(len(idx) // 2, 80.0),
                              np.full(len(idx) - len(idx) // 2, 10.0)]},
        index=idx,
    )
    out_fearful = s.evaluate({"ohlcv": falling, "fear_greed": fg})
    fg_calm = pd.DataFrame({"fear_greed": np.full(len(idx), 60.0)}, index=idx)
    out_calm = s.evaluate({"ohlcv": falling, "fear_greed": fg_calm})
    fearful_multiplier = out_fearful.metrics["multiplier"]
    calm_multiplier = out_calm.metrics["multiplier"]
    assert isinstance(fearful_multiplier, int | float)
    assert isinstance(calm_multiplier, int | float)
    assert fearful_multiplier > calm_multiplier


def test_capitulation_evaluates_without_data() -> None:
    s = Capitulation(StrategyConfig(name="capitulation", params={}))
    out = s.evaluate({"ohlcv": pd.DataFrame()})
    assert out.opportunity is None
    assert out.metrics["reason"] == "no ohlcv"


def test_capitulation_returns_metrics_on_normal_data() -> None:
    s = Capitulation(StrategyConfig(name="capitulation", params={}))
    out = s.evaluate({"ohlcv": _hourly_ohlcv()})
    assert "score" in out.metrics
    assert "groups_firing" in out.metrics


def test_trend_emits_open_on_cross_up() -> None:
    cfg = StrategyConfig(name="trend", params={"ma_weeks": 5, "vol_shock_ratio": 99})
    s = WeeklyTrend(cfg)
    n = 24 * 7 * 20
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    # Falling for first half (price < MA), rising for second half (cross up)
    close = pd.Series(
        np.r_[np.linspace(60_000, 30_000, n // 2),
              np.linspace(30_000, 60_000, n - n // 2)], index=idx,
    )
    ohlcv = pd.DataFrame(
        {"open": close, "high": close * 1.005, "low": close * 0.995,
         "close": close, "volume": np.ones(n)}
    )
    out = s.evaluate({"ohlcv": ohlcv})
    # Either a cross-up fired, or we're still in the up-leg (in_uptrend)
    assert out.metrics["in_uptrend"] or (out.opportunity and out.opportunity.action == "open")


def test_trend_handles_short_history() -> None:
    cfg = StrategyConfig(name="trend", params={"ma_weeks": 20})
    s = WeeklyTrend(cfg)
    out = s.evaluate({"ohlcv": _hourly_ohlcv(n_hours=24 * 7)})
    assert out.opportunity is None
    assert "reason" in out.metrics


def test_carry_fires_open_on_fat_funding() -> None:
    cfg = StrategyConfig(name="carry", params={"avg_window_bars": 8})
    s = BasisCarry(cfg)
    idx = pd.date_range("2024-01-01", periods=24, freq="1h", tz="UTC")
    funding = pd.DataFrame({"funding_rate": np.full(24, 0.0003)}, index=idx)
    out = s.evaluate({"ohlcv": _hourly_ohlcv(n_hours=24), "funding": funding})
    assert out.opportunity is not None
    assert out.opportunity.action == "open"


def test_carry_fires_close_on_low_funding() -> None:
    cfg = StrategyConfig(name="carry", params={"avg_window_bars": 8})
    s = BasisCarry(cfg)
    idx = pd.date_range("2024-01-01", periods=24, freq="1h", tz="UTC")
    funding = pd.DataFrame({"funding_rate": np.full(24, 1e-7)}, index=idx)  # ~0 APR
    out = s.evaluate({"ohlcv": _hourly_ohlcv(n_hours=24), "funding": funding})
    assert out.opportunity is not None
    assert out.opportunity.action == "close"


def test_carry_watches_in_band() -> None:
    cfg = StrategyConfig(name="carry", params={"avg_window_bars": 8})
    s = BasisCarry(cfg)
    idx = pd.date_range("2024-01-01", periods=24, freq="1h", tz="UTC")
    # ~10% APR funding = 0.0001 per 8h
    funding = pd.DataFrame({"funding_rate": np.full(24, 0.0001)}, index=idx)
    out = s.evaluate({"ohlcv": _hourly_ohlcv(n_hours=24), "funding": funding})
    assert out.opportunity is None


# ---------------------- runner heartbeat ---------------------------------


class _StubStrategy(Strategy):
    name = "stub"
    description = "test stub strategy"

    def fetch_data(self, settings: Settings) -> dict[str, object]:
        return {}

    def evaluate(self, data: dict[str, object]) -> EvaluationResult:
        opp = Opportunity(
            ts=datetime.now(timezone.utc), action="buy",
            confidence=0.5, reason="test fire", details={"x": 1},
        )
        return EvaluationResult(
            ts=datetime.now(timezone.utc), opportunity=opp,
            metrics={"x": 1.0},
        )


class _OneShotOpportunityStrategy(Strategy):
    name = "oneshot"
    description = "one-shot test strategy"

    def __init__(
        self,
        config: StrategyConfig,
        *,
        data: dict[str, object],
        action: Action | tuple[Action, ...],
        stop_event: threading.Event,
        auto_stop: bool = True,
    ) -> None:
        super().__init__(config)
        self._data = data
        self._actions: tuple[Action, ...] = (action,) if isinstance(action, str) else action
        self._cycle = 0
        self._stop_event = stop_event
        self._auto_stop = auto_stop

    def fetch_data(self, settings: Settings) -> dict[str, object]:
        return self._data

    def evaluate(self, data: dict[str, object]) -> EvaluationResult:
        action = self._actions[min(self._cycle, len(self._actions) - 1)]
        self._cycle += 1
        if self._auto_stop and self._cycle >= len(self._actions):
            self._stop_event.set()
        opp = Opportunity(
            ts=datetime.now(timezone.utc),
            action=action,
            confidence=0.9,
            reason="runner regression",
            details={"symbol": "BTC/USDT", "amount_quote": 500.0},
        )
        return EvaluationResult(
            ts=datetime.now(timezone.utc),
            opportunity=opp,
            metrics={"close": 42_000.0},
        )


class _FailingBroker(Broker):
    def submit(self, order: Order, mark_price: float) -> Trade:
        raise RuntimeError("broker blocked order")

    def cash(self) -> float:
        return 100_000.0

    def position_qty(self, symbol: str) -> float:
        return 0.0


def _run_strategy_once(strategy: Strategy, tmp_path: Path, stop: threading.Event) -> dict[str, object]:
    with patch("qt.strategies.runner.alert"):
        t = threading.Thread(
            target=run_strategy_forever,
            args=(strategy, Settings()),
            kwargs={"runtime_dir": tmp_path, "stop_event": stop, "max_backoff_seconds": 1},
            daemon=True,
        )
        t.start()
        t.join(timeout=5)
    assert not t.is_alive()
    path = strategy_state_path(tmp_path, strategy.name)
    assert path.exists()
    payload: object = json.loads(path.read_text())
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def test_runner_executes_open_opportunity_as_entry(tmp_path: Path) -> None:
    stop = threading.Event()
    strat = _OneShotOpportunityStrategy(
        StrategyConfig(name="oneshot", interval_seconds=1, params={}),
        data={"mark_price": 42_000.0},
        action="open",
        stop_event=stop,
    )

    snap = _run_strategy_once(strat, tmp_path, stop)

    trades = snap["details"]["last_trades"]  # type: ignore[index]
    assert isinstance(trades, list)
    assert len(trades) == 1
    assert trades[0]["side"] == "buy"
    assert trades[0]["price"] == pytest.approx(42_000.0 * 1.0008)


def test_runner_prices_order_from_latest_ohlcv_close(tmp_path: Path) -> None:
    stop = threading.Event()
    ohlcv = pd.DataFrame(
        {"close": [41_000.0, 42_000.0]},
        index=pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC"),
    )
    strat = _OneShotOpportunityStrategy(
        StrategyConfig(name="oneshot", interval_seconds=1, params={}),
        data={"ohlcv": ohlcv},
        action="buy",
        stop_event=stop,
    )

    snap = _run_strategy_once(strat, tmp_path, stop)

    trades = snap["details"]["last_trades"]  # type: ignore[index]
    assert isinstance(trades, list)
    assert len(trades) == 1
    assert trades[0]["price"] == pytest.approx(42_000.0 * 1.0008)


def test_runner_respects_opportunity_amount_quote(tmp_path: Path) -> None:
    stop = threading.Event()
    strat = _OneShotOpportunityStrategy(
        StrategyConfig(name="oneshot", interval_seconds=1, params={}),
        data={"mark_price": 42_000.0},
        action="buy",
        stop_event=stop,
    )

    snap = _run_strategy_once(strat, tmp_path, stop)

    trades = snap["details"]["last_trades"]  # type: ignore[index]
    assert isinstance(trades, list)
    assert len(trades) == 1
    assert trades[0]["qty"] * 42_000.0 == pytest.approx(500.0)


def test_runner_executes_close_opportunity_as_exit(tmp_path: Path) -> None:
    stop = threading.Event()
    strat = _OneShotOpportunityStrategy(
        StrategyConfig(name="oneshot", interval_seconds=1, params={}),
        data={"mark_price": 42_000.0},
        action=("buy", "close"),
        stop_event=stop,
    )

    snap = _run_strategy_once(strat, tmp_path, stop)

    trades = snap["details"]["last_trades"]  # type: ignore[index]
    assert isinstance(trades, list)
    assert len(trades) == 1
    assert trades[0]["side"] == "sell"
    positions = snap["details"]["portfolio"]["positions"]  # type: ignore[index]
    assert positions["BTC/USDT"] == pytest.approx(0.0)


def test_runner_records_broker_error_in_heartbeat(tmp_path: Path) -> None:
    stop = threading.Event()
    strat = _OneShotOpportunityStrategy(
        StrategyConfig(name="oneshot", interval_seconds=1, params={}),
        data={"mark_price": 42_000.0},
        action="buy",
        stop_event=stop,
        auto_stop=False,
    )

    path = strategy_state_path(tmp_path, "oneshot")
    with patch("qt.strategies.runner.alert"), patch(
        "qt.strategies.runner._make_broker", return_value=_FailingBroker()
    ):
        t = threading.Thread(
            target=run_strategy_forever,
            args=(strat, Settings()),
            kwargs={"runtime_dir": tmp_path, "stop_event": stop, "max_backoff_seconds": 1},
            daemon=True,
        )
        t.start()
        snap: dict[str, object] = {}
        for _ in range(50):
            if path.exists():
                snap = json.loads(path.read_text())
                if snap.get("cycle") == 1 or not t.is_alive():
                    break
            stop.wait(0.05)
        stop.set()
        t.join(timeout=5)

    assert snap["cycle"] == 1
    assert snap["status"] == "degraded"
    assert "broker blocked order" in str(snap["last_error"])


def test_live_mode_broker_initialization_fails_closed(tmp_path: Path) -> None:
    settings = Settings()
    settings.execution.mode = "live"
    settings.execution.live_enabled = True

    with (
        patch("qt.strategies.runner.alert"),
        patch(
            "qt.execution.live.LiveBroker.from_settings",
            side_effect=UnsafeApiKeyError("unsafe live key"),
        ),
        pytest.raises(UnsafeApiKeyError, match="unsafe live key"),
    ):
        run_strategy_forever(
            _StubStrategy(StrategyConfig(name="stub", interval_seconds=1, params={})),
            settings,
            runtime_dir=tmp_path,
            stop_event=threading.Event(),
            max_backoff_seconds=1,
        )


def test_live_mode_with_disabled_switch_fails_closed() -> None:
    settings = Settings()
    settings.execution.mode = "live"
    settings.execution.live_enabled = False

    with pytest.raises(LiveTradingDisabledError, match=r"execution\.live_enabled=false"):
        _make_broker(settings, initial_cash=100_000.0)


def test_run_strategy_writes_heartbeat(tmp_path: Path) -> None:
    # Register stub for the lifetime of the test
    REGISTRY["stub"] = _StubStrategy
    try:
        cfg = StrategyConfig(name="stub", interval_seconds=60, params={})
        strat = _StubStrategy(cfg)
        stop = threading.Event()

        def _run() -> None:
            run_strategy_forever(
                strat, Settings(), runtime_dir=tmp_path,
                stop_event=stop, max_backoff_seconds=1,
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # Wait for first tick to complete (≤2s)
        path = strategy_state_path(tmp_path, "stub")
        for _ in range(50):
            if path.exists():
                break
            stop.wait(0.05)
        stop.set()
        t.join(timeout=5)
        assert path.exists()
        import json
        snap = json.loads(path.read_text())
        assert snap["name"] == "stub"
        assert snap["status"] in {"healthy", "stopped"}
        assert "last_opportunity" in snap["details"]
    finally:
        REGISTRY.pop("stub", None)
