"""Versioned public projection and normalized events (all wall times are UTC seconds)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .venues import Environment, Venue, spot_source


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MarketEvent(Model):
    channel: Literal["trade", "quote", "candle"]
    event_time: float | None = None
    received_at: float
    received_mono: float
    sequence: int = Field(ge=0)
    price: float = Field(gt=0)
    quantity: float = Field(default=0, ge=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)
    high: float | None = Field(default=None, gt=0)
    open_time: float | None = None
    close_time: float | None = None
    historical: bool = False


class Source(Model):
    exchange: Venue = "binance"
    environment: Environment = "production"
    product: Literal["spot"] = "spot"
    symbol: Literal["BTCUSDT", "BTC-USDT"] = "BTCUSDT"
    display_symbol: Literal["BTC/USDT"] = "BTC/USDT"
    price_type: Literal["trade"] = "trade"
    rest_url: str = (
        "https://data-api.binance.vision/api/v3"
    )
    websocket_url: str = "wss://data-stream.binance.vision:443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker/btcusdt@kline_1m"

    @model_validator(mode="after")
    def official_identity(self) -> Source:
        source = spot_source(self.exchange, self.environment)
        if (self.symbol, self.rest_url, self.websocket_url) != (
            source.symbol, source.rest_url, source.websocket_url,
        ):
            raise ValueError("source identity does not match the selected official spot source")
        return self

    @classmethod
    def for_config(cls, config: Any) -> Source:
        source = spot_source(config.venue, config.environment)
        return cls(
            exchange=source.exchange, environment=source.environment, symbol=source.symbol,
            rest_url=source.rest_url, websocket_url=source.websocket_url,
        )


class Gate(Model):
    name: str
    passed: bool
    value: Any = None
    threshold: Any = None
    reason: str = ""


class AnomalyDecision(Model):
    event_id: str
    revision: int
    state: Literal["WATCH", "CONFIRMED", "INVALIDATED", "EXPIRED"]
    as_of: float
    expires_at: float
    direction: Literal["down", "up"]
    baseline: float
    extreme: float
    recovery_fraction: float
    reason: str
    source: Source = Field(default_factory=Source)


def _allowed_actions() -> list[Literal["monitor", "research"]]:
    return ["monitor", "research"]


class DecisionRecord(Model):
    assessment_id: str
    revision: int = 1
    as_of: float
    expires_at: float
    state: Literal["INSUFFICIENT_DATA", "DATA_UNHEALTHY", "UNSUITABLE", "CANDIDATE"]
    reasons: list[str]
    source: Source = Field(default_factory=Source)
    gates: list[Gate] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    range_lower: float | None = None
    range_upper: float | None = None
    spacing_bps: float | None = None
    net_spacing_bps: float | None = None
    levels: int | None = None
    missing_inputs: list[str] = Field(
        default_factory=lambda: [
            "budget",
            "saleable_btc_inventory",
            "actual_fee_tier",
            "exchange_order_size_constraints",
            "cross_venue_confirmation_unavailable",
            "strategy_profitability_unvalidated",
        ]
    )
    allowed_actions: list[Literal["monitor", "research"]] = Field(default_factory=_allowed_actions)
    blocked_actions: list[str] = Field(default_factory=lambda: ["execute_orders"])
    live_execution_enabled: Literal[False] = False


class ChannelHealth(Model):
    last_event_at: float | None = None
    last_receive_at: float | None = None
    event_age_seconds: float | None = None
    receive_age_seconds: float | None = None
    backlog_seconds: float | None = None
    ready: bool = False
    source_time_known: bool = True


class Health(Model):
    alive: bool = True
    data_ready: bool = False
    assessment_ready: bool = False
    status: str = "warming_up"
    reasons: list[str] = Field(default_factory=list)
    channels: dict[str, ChannelHealth] = Field(default_factory=dict)
    coverage_seconds: float = 0
    last_progress_at: float | None = None
    last_persisted_at: float | None = None
    errors: dict[str, int] = Field(default_factory=dict)


class MonitorSnapshot(Model):
    schema_version: Literal[1] = 1
    config_version: str
    boot_id: str
    published_at: float
    source: Source = Field(default_factory=Source)
    health: Health
    micro: dict[str, Any]
    assessment: DecisionRecord
    recent_events: list[AnomalyDecision] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    live_execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def consistent_source(self) -> MonitorSnapshot:
        if self.assessment.source != self.source or any(
            item.source != self.source for item in self.recent_events
        ):
            raise ValueError("snapshot mixes source identities")
        return self
