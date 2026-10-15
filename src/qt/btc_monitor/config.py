"""Small validated runtime configuration; credentials are deliberately not model fields."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    state_dir: Path = Path("data/workbench/btc-monitor")
    symbol: str = "BTCUSDT"
    rest_url: str = "https://data-api.binance.vision/api/v3"
    websocket_url: str = "wss://data-stream.binance.vision:443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker/btcusdt@kline_1m"
    queue_size: int = Field(default=4096, ge=8, le=100000)
    ring_size: int = Field(default=3601, ge=100, le=10000)
    minimum_coverage_seconds: float = Field(default=60, ge=5, le=3600)
    grid_horizon_seconds: int = Field(default=900, ge=60, le=3600)
    grid_minimum_coverage_seconds: float = Field(default=600, ge=30, le=3600)
    grid_minimum_buckets: int = Field(default=450, ge=10, le=3600)
    minimum_trade_buckets: int = Field(default=45, ge=3)
    trade_stale_seconds: float = Field(default=5, gt=0)
    quote_stale_seconds: float = Field(default=5, gt=0)
    candle_stale_seconds: float = Field(default=125, gt=60)
    max_backlog_seconds: float = Field(default=2, gt=0)
    max_future_seconds: float = Field(default=2, ge=0)
    event_ttl_seconds: float = Field(default=120, ge=5)
    decision_ttl_seconds: float = Field(default=5, gt=0)
    anomaly_window_seconds: int = Field(default=10, ge=1, le=60)
    wick_threshold_bps: float = Field(default=50, gt=0)
    wick_recovery_fraction: float = Field(default=0.5, gt=0, le=1)
    confirmation_seconds: float = Field(default=3, ge=0)
    shock_cooldown_seconds: float = Field(default=30, ge=0)
    maximum_efficiency: float = Field(default=0.35, ge=0, le=1)
    minimum_reversals: int = Field(default=5, ge=1)
    range_quantile: float = Field(default=0.05, ge=0, lt=0.5)
    grid_levels: int = Field(default=6, ge=3, le=50)
    fee_per_side_bps: float = Field(default=10, ge=0)
    slippage_per_side_bps: float = Field(default=2, ge=0)
    cost_buffer_bps: float = Field(default=3, ge=0)
    maximum_spread_bps: float = Field(default=10, gt=0)
    tick_size: float = Field(default=0.01, gt=0)
    bootstrap_candles: int = Field(default=120, ge=3, le=1000)
    session_rotation_seconds: float = Field(default=23 * 3600, ge=60, le=24 * 3600)
    watchdog_seconds: float = Field(default=30, ge=3)
    shutdown_seconds: float = Field(default=10, ge=1, le=120)
    retention_events: int = Field(default=1000, ge=10, le=100000)

    @model_validator(mode="after")
    def supported_source(self) -> MonitorConfig:
        # This first adapter is explicitly spot BTCUSDT, not a generic venue switch.
        if (
            self.rest_url != "https://data-api.binance.vision/api/v3"
            or self.websocket_url
            != "wss://data-stream.binance.vision:443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker/btcusdt@kline_1m"
        ):
            raise ValueError("only the official Binance spot data-only endpoints are supported")
        if self.symbol != "BTCUSDT":
            raise ValueError("this adapter supports only spot BTCUSDT")
        if self.minimum_trade_buckets > self.minimum_coverage_seconds + 1:
            raise ValueError("minimum_trade_buckets cannot exceed coverage seconds")
        if self.ring_size < self.grid_horizon_seconds:
            raise ValueError("ring_size must cover the grid horizon in seconds")
        if (
            self.grid_minimum_coverage_seconds > self.grid_horizon_seconds
            or self.grid_minimum_buckets > self.grid_horizon_seconds
        ):
            raise ValueError("grid coverage must fit the configured horizon")
        return self

    @classmethod
    def from_env(cls, state_dir: Path | None = None) -> MonitorConfig:
        values: dict[str, object] = {}
        if os.environ.get("QT_BTC_MONITOR_STATE_DIR"):
            values["state_dir"] = os.environ["QT_BTC_MONITOR_STATE_DIR"]
        if os.environ.get("QT_BTC_MONITOR_CONFIG"):
            raw = json.loads(Path(os.environ["QT_BTC_MONITOR_CONFIG"]).read_text())
            if not isinstance(raw, dict):
                raise ValueError("monitor configuration must be an object")
            values.update(raw)
        if state_dir is not None:
            values["state_dir"] = state_dir
        return cls.model_validate(values)

    @property
    def version(self) -> str:
        data = self.model_dump(mode="json", exclude={"state_dir"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
