"""Versioned, finite contracts shared by the alert worker and protected UI."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Direction = Literal["long", "reduce_long", "short"]
TIMEFRAMES = {"15m": 900, "1h": 3600, "4h": 14400}
ALGORITHM_VERSION = "btc-alerts-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True)


class Thresholds(Contract):
    lookback: int = Field(default=20, ge=10, le=100)
    fast_ema: int = Field(default=20, ge=5, le=50)
    slow_ema: int = Field(default=50, ge=20, le=200)
    breakout_volume: float = Field(default=1.5, ge=1, le=10)
    distress_rsi: float = Field(default=25, ge=5, le=40)
    distress_volume: float = Field(default=2, ge=1, le=10)
    wick_body: float = Field(default=2, ge=1, le=10)
    wick_fraction: float = Field(default=0.6, ge=0.5, lt=1)
    wick_atr: float = Field(default=1.5, ge=1, le=10)
    overheat_rsi: float = Field(default=75, ge=65, le=95)
    overheat_recovery_rsi: float = Field(default=70, ge=50, le=85)
    stop_atr: float = Field(default=1.5, ge=1, le=5)
    target_atr: float = Field(default=4, ge=2, le=12)

    @model_validator(mode="after")
    def coherent(self) -> Thresholds:
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema_must_precede_slow_ema")
        if self.overheat_recovery_rsi >= self.overheat_rsi:
            raise ValueError("overheat_recovery_must_be_lower")
        return self


class Policy(Contract):
    mode: Literal["off", "shadow", "notify"] = "shadow"
    thresholds: Thresholds = Field(default_factory=Thresholds)
    candidate_ttl_seconds: int = Field(default=600, ge=120, le=600)
    cooldown_seconds: Literal[3600] = 3600
    quote_max_age_seconds: float = Field(default=5, gt=0, le=5)
    max_spread_bps: float = Field(default=10, gt=0, le=10)
    max_price_deviation: float = Field(default=0.003, gt=0, le=0.003)
    min_net_reward_risk: float = Field(default=2, ge=2)
    fee_per_side_bps: float = Field(default=10, ge=0, le=100)
    slippage_per_side_bps: float = Field(default=2, ge=0, le=100)
    cost_buffer_bps: float = Field(default=3, ge=0, le=100)
    horizon_hours: int = Field(default=72, ge=4, le=72)

    @property
    def strategy_version(self) -> str:
        from .knowledge import CARDS, SOURCES

        return digest({
            "algorithm": ALGORITHM_VERSION, "evidence": digest({"cards": CARDS, "sources": SOURCES}),
            **self.model_dump(exclude={"mode"}),
        })[:24]

    @property
    def round_trip_cost_bps(self) -> float:
        return 2 * (self.fee_per_side_bps + self.slippage_per_side_bps) + self.cost_buffer_bps


class Bar(Contract):
    open_time: int = Field(ge=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def contains_prices(self) -> Bar:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("incoherent_ohlcv")
        return self


class Quote(Contract):
    venue: Literal["binance", "okx"]
    product: Literal["spot", "perpetual"]
    symbol: Literal["BTCUSDT", "BTC-USDT-SWAP"]
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    observed_at: float = Field(ge=0)
    source_at: float | None = None
    mark_price: float | None = Field(default=None, gt=0)
    funding_cost_bps: float | None = Field(default=None, ge=0)
    funding_as_of: float | None = None
    funding_coverage_start: float | None = None

    @model_validator(mode="after")
    def ordered(self) -> Quote:
        if self.ask < self.bid:
            raise ValueError("crossed_quote")
        return self

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000


class Trigger(Contract):
    strategy_id: Literal["trend", "recovery", "wick", "overheat"]
    observations: dict[str, float | bool]
    thresholds: dict[str, float]


class Candidate(Contract):
    schema_version: Literal[1] = 1
    candidate_id: str
    strategy_version: str
    venue: Literal["binance", "okx"]
    product: Literal["spot", "perpetual"]
    symbol: Literal["BTCUSDT", "BTC-USDT-SWAP"]
    direction: Direction
    bar_close: int
    expires_at: float
    reference_price: float = Field(gt=0)
    atr_1h: float = Field(gt=0)
    invalidation_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    net_reward_risk: float
    cost_bps: float = Field(ge=0)
    triggers: list[Trigger] = Field(min_length=1, max_length=4)
    data_snapshot_hash: str
    evidence_ids: list[str] = Field(min_length=1)
    missing_evidence: list[str] = Field(default_factory=list)


class Review(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    candidate_id: str = Field(min_length=1, max_length=64)
    verdict: Literal["approve", "reject", "insufficient_evidence"]
    reason_codes: list[str] = Field(min_length=1, max_length=10)
    supporting_evidence_ids: list[str] = Field(max_length=16)
    opposing_evidence_ids: list[str] = Field(max_length=16)
    missing_evidence: list[str] = Field(max_length=16)
    summary: str = Field(min_length=1, max_length=1000)


class ReviewBatch(Contract):
    reviews: list[Review] = Field(min_length=1, max_length=3)


class PolicyUpdate(Contract):
    expected_revision: int = Field(ge=1)
    policy: Policy


class TestNotificationRequest(Contract):
    request_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
