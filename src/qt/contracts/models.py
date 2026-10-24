"""Public V2 research, data, release and execution contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from qt.contracts.base import (
    Contract,
    DecimalString,
    FrozenDict,
    NonEmptyStr,
    NonNegativeDecimal,
    PositiveDecimal,
    Probability,
    Sha256,
    TimestampNS,
)

MarketEventType = Literal[
    "snapshot", "delta", "trade", "quote", "bar", "funding", "liquidation",
    "news", "onchain", "instrument", "heartbeat",
]


class MarketEvent(Contract):
    venue: NonEmptyStr
    instrument: NonEmptyStr
    event_type: MarketEventType
    exchange_ts_ns: TimestampNS
    received_ts_ns: TimestampNS
    timestamp_precision: Literal["ns"] = "ns"
    sequence: int | None = Field(default=None, ge=0, le=2**63 - 1)
    payload: FrozenDict = Field(default_factory=FrozenDict)
    quality_flags: tuple[str, ...] = ()
    published_at_ns: TimestampNS | None = None
    available_at_ns: TimestampNS | None = None
    revision_id: NonEmptyStr = "initial"
    source_hash: Sha256 | None = None

    @model_validator(mode="after")
    def available_before_receipt(self) -> MarketEvent:
        if self.available_at_ns is not None and self.available_at_ns > self.received_ts_ns:
            raise ValueError("event version cannot be received before it is available")
        return self


class InstrumentSpec(Contract):
    venue: NonEmptyStr
    instrument: NonEmptyStr
    base_asset: NonEmptyStr
    quote_asset: NonEmptyStr
    settlement_asset: NonEmptyStr
    price_increment: PositiveDecimal
    quantity_increment: PositiveDecimal
    minimum_quantity: PositiveDecimal
    minimum_notional: NonNegativeDecimal
    contract_multiplier: PositiveDecimal = Decimal("1")
    instrument_type: Literal["spot", "perpetual", "future", "prediction"] = "spot"
    trading_sessions: tuple[str, ...] = ("24x7",)
    effective_from_ns: TimestampNS
    effective_to_ns: TimestampNS | None = None
    native_identifiers: FrozenDict = Field(default_factory=FrozenDict)
    source: NonEmptyStr

    @model_validator(mode="after")
    def valid_interval(self) -> InstrumentSpec:
        if self.effective_to_ns is not None and self.effective_to_ns <= self.effective_from_ns:
            raise ValueError("instrument validity interval must be nonempty")
        return self


class FeeSchedule(Contract):
    venue: NonEmptyStr
    account_tier: NonEmptyStr
    maker_rate: DecimalString
    taker_rate: DecimalString
    fee_basis: Literal["notional", "shares", "price_curve"]
    fee_asset: NonEmptyStr
    funding_rules: FrozenDict = Field(default_factory=FrozenDict)
    fee_rules: FrozenDict = Field(default_factory=FrozenDict)
    effective_from_ns: TimestampNS
    effective_to_ns: TimestampNS | None = None
    source: NonEmptyStr
    source_hash: Sha256

    @model_validator(mode="after")
    def valid_interval(self) -> FeeSchedule:
        if self.effective_to_ns is not None and self.effective_to_ns <= self.effective_from_ns:
            raise ValueError("fee validity interval must be nonempty")
        return self


class DataGap(Contract):
    start_ns: TimestampNS
    end_ns: TimestampNS
    missing_count: int = Field(ge=1)
    reason: NonEmptyStr

    @model_validator(mode="after")
    def ordered(self) -> DataGap:
        if self.end_ns < self.start_ns:
            raise ValueError("gap end precedes start")
        return self


class DatasetFile(Contract):
    relative_path: NonEmptyStr
    sha256: Sha256
    bytes: int = Field(ge=1)
    rows: int = Field(ge=1)
    start_ns: TimestampNS
    end_ns: TimestampNS

    @model_validator(mode="after")
    def ordered(self) -> DatasetFile:
        if self.end_ns < self.start_ns:
            raise ValueError("file end precedes start")
        return self


class DatasetManifest(Contract):
    dataset_id: Sha256
    files: tuple[DatasetFile, ...] = Field(min_length=1)
    venue: NonEmptyStr
    instrument: NonEmptyStr
    data_type: NonEmptyStr
    timestamp_column: NonEmptyStr
    timestamp_precision: Literal["ns"] = "ns"
    rows: int = Field(ge=1)
    start_ns: TimestampNS
    end_ns: TimestampNS
    expected_interval_ns: int | None = Field(default=None, gt=0)
    gaps: tuple[DataGap, ...] = ()
    revision_id: NonEmptyStr
    availability: Literal["received", "reconstructed_available"]
    rights: NonEmptyStr
    provenance: FrozenDict
    columns: tuple[str, ...] = Field(min_length=1)
    arrow_schema: NonEmptyStr

    @model_validator(mode="after")
    def consistent_files(self) -> DatasetManifest:
        if len({part.relative_path for part in self.files}) != len(self.files):
            raise ValueError("dataset parts must be unique")
        if sum(part.rows for part in self.files) != self.rows:
            raise ValueError("dataset row count disagrees with files")
        if min(part.start_ns for part in self.files) != self.start_ns:
            raise ValueError("dataset start disagrees with files")
        if max(part.end_ns for part in self.files) != self.end_ns:
            raise ValueError("dataset end disagrees with files")
        return self


class RiskBudget(Contract):
    capital: PositiveDecimal
    currency: NonEmptyStr
    maximum_position_fraction: Probability = Decimal("0.30")
    maximum_order_fraction: Probability = Decimal("0.02")
    planned_loss_fraction: Probability = Decimal("0.001")
    maximum_drawdown_fraction: Probability = Decimal("0.03")
    maximum_daily_loss_fraction: Probability = Decimal("0.005")
    maximum_leverage: PositiveDecimal = Decimal("1")


class StrategyArtifact(Contract):
    strategy_id: NonEmptyStr
    version: NonEmptyStr
    code_hash: Sha256
    parameters: FrozenDict = Field(default_factory=FrozenDict)
    feature_dependencies: tuple[str, ...]
    instruments: tuple[str, ...] = Field(min_length=1)
    signal_interval_ns: int = Field(gt=0)
    signal_ttl_ns: int = Field(gt=0)
    exit_rules: FrozenDict
    risk_budget: RiskBudget
    engine_compatibility: tuple[str, ...] = Field(min_length=1)


class SplitSpec(Contract):
    method: Literal["walk_forward", "holdout", "cpcv"]
    training_start_ns: TimestampNS
    training_end_ns: TimestampNS
    validation_end_ns: TimestampNS
    test_end_ns: TimestampNS
    folds: int = Field(default=1, ge=1)
    purge_ns: int = Field(default=0, ge=0)
    embargo_ns: int = Field(default=0, ge=0)
    label_information_interval: NonEmptyStr
    final_holdout: bool = False

    @model_validator(mode="after")
    def temporal_order(self) -> SplitSpec:
        if not (self.training_start_ns < self.training_end_ns
                <= self.validation_end_ns < self.test_end_ns):
            raise ValueError("training, validation and test boundaries must be causal")
        return self


class CostModel(Contract):
    version: NonEmptyStr
    fee_schedule_hashes: tuple[Sha256, ...] = Field(min_length=1)
    slippage_bps: NonNegativeDecimal
    funding_enabled: bool = False
    borrow_enabled: bool = False
    capital_cost_bps_per_day: NonNegativeDecimal = Decimal("0")


class ModelSpec(Contract):
    name: NonEmptyStr
    version: NonEmptyStr
    parameters: FrozenDict = Field(default_factory=FrozenDict)


class LatencyModel(Contract):
    version: NonEmptyStr
    feed_ns: int = Field(ge=0)
    decision_ns: int = Field(ge=0)
    submission_ns: int = Field(ge=0)
    exchange_ns: int = Field(ge=0)
    parameters: FrozenDict = Field(default_factory=FrozenDict)


class EngineSpec(Contract):
    name: Literal["nautilus", "hftbacktest", "fixture"]
    version: NonEmptyStr
    image_digest: NonEmptyStr


class ExperimentSpecV2(Contract):
    campaign_id: NonEmptyStr
    strategy_version: NonEmptyStr
    dataset_ids: tuple[Sha256, ...] = Field(min_length=1)
    instruments: tuple[str, ...] = Field(min_length=1)
    timeframes_ns: tuple[int, ...] = Field(min_length=1)
    instrument_spec_hashes: tuple[Sha256, ...] = ()
    split: SplitSpec
    cost_model: CostModel
    fill_model: ModelSpec
    latency_model: LatencyModel
    benchmarks: tuple[str, ...] = Field(min_length=1)
    seed: int = Field(ge=0)
    engine: EngineSpec
    policy_version: NonEmptyStr

    @model_validator(mode="after")
    def intervals(self) -> ExperimentSpecV2:
        if any(interval <= 0 for interval in self.timeframes_ns):
            raise ValueError("timeframes must be positive nanosecond intervals")
        return self


class MetricResult(Contract):
    value: float | None
    reason: str | None = None
    sample_count: int = Field(ge=0)
    effective_sample_count: float | None = Field(default=None, ge=0)
    confidence_lower: float | None = None
    confidence_upper: float | None = None
    calculation_version: NonEmptyStr
    start_ns: TimestampNS
    end_ns: TimestampNS

    @model_validator(mode="after")
    def unavailable_reason(self) -> MetricResult:
        if self.value is None and not self.reason:
            raise ValueError("unavailable metrics require a reason")
        if self.end_ns < self.start_ns:
            raise ValueError("metric end precedes start")
        if (self.confidence_lower is None) != (self.confidence_upper is None):
            raise ValueError("confidence interval requires both bounds")
        if (self.confidence_lower is not None and self.confidence_upper is not None
                and self.confidence_lower > self.confidence_upper):
            raise ValueError("confidence interval is reversed")
        return self


class GateResult(Contract):
    name: NonEmptyStr
    status: Literal["pass", "fail", "insufficient", "not_applicable"]
    reason: NonEmptyStr
    evidence_hashes: tuple[Sha256, ...] = ()


class ValidationReportV2(Contract):
    experiment_hash: Sha256
    policy_version: NonEmptyStr
    integrity: tuple[GateResult, ...]
    metrics: tuple[tuple[str, MetricResult], ...]
    statistical_tests: tuple[GateResult, ...]
    search_campaign_id: NonEmptyStr
    trial_count: int = Field(ge=1)
    search_complete: bool
    stress_results: tuple[GateResult, ...]
    gates: tuple[GateResult, ...]
    evidence_artifacts: tuple[Sha256, ...]
    created_at_ns: TimestampNS


class ForecastRecord(Contract):
    forecast_id: NonEmptyStr
    event_id: NonEmptyStr
    event_cluster_id: NonEmptyStr
    market_id: NonEmptyStr
    outcome_id: NonEmptyStr
    probability: Probability
    market_bid: Probability | None = None
    market_ask: Probability | None = None
    market_mid: Probability | None = None
    market_probability_missing_reason: str | None = None
    decision_at_ns: TimestampNS
    horizon_end_ns: TimestampNS
    model_provider: NonEmptyStr
    model_id: NonEmptyStr
    prompt_hash: Sha256
    rules_hash: Sha256
    prediction_mode: Literal["blind", "market_conditioned"]
    evidence_hashes: tuple[Sha256, ...] = ()

    @model_validator(mode="after")
    def valid_forecast(self) -> ForecastRecord:
        if self.horizon_end_ns <= self.decision_at_ns:
            raise ValueError("forecast horizon must follow the decision")
        if self.market_mid is None and not self.market_probability_missing_reason:
            raise ValueError("missing market baseline requires an explicit reason")
        if self.market_bid is not None and self.market_ask is not None:
            if self.market_bid > self.market_ask:
                raise ValueError("market bid exceeds ask")
            if self.market_mid is not None and not self.market_bid <= self.market_mid <= self.market_ask:
                raise ValueError("market midpoint is outside the spread")
        return self


RiskMode = Literal["ACTIVE", "REDUCE_ONLY", "HALTED", "EMERGENCY_EXIT"]
DeploymentMode = Literal["paper", "shadow", "live_canary"]


class DeploymentManifest(Contract):
    deployment_id: NonEmptyStr
    strategy_hash: Sha256
    provider: NonEmptyStr
    model_id: NonEmptyStr
    prompt_hash: Sha256
    tools_hash: Sha256
    risk_policy_hash: Sha256
    fee_schedule_hashes: tuple[Sha256, ...] = Field(min_length=1)
    validation_report_hash: Sha256
    venue: NonEmptyStr
    account_id: NonEmptyStr
    mode: DeploymentMode
    risk_budget: RiskBudget
    valid_from_ns: TimestampNS
    valid_until_ns: TimestampNS
    signature: NonEmptyStr

    @model_validator(mode="after")
    def valid_interval(self) -> DeploymentManifest:
        if self.valid_until_ns <= self.valid_from_ns:
            raise ValueError("deployment validity interval must be nonempty")
        return self


class SignalIntent(Contract):
    signal_id: NonEmptyStr
    deployment_id: NonEmptyStr
    event_id: NonEmptyStr
    instrument: NonEmptyStr
    target_position: DecimalString
    produced_at_ns: TimestampNS
    valid_until_ns: TimestampNS
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def not_expired_at_creation(self) -> SignalIntent:
        if self.valid_until_ns <= self.produced_at_ns:
            raise ValueError("signal validity interval must be nonempty")
        return self


class OrderIntent(Contract):
    intent_id: NonEmptyStr
    account_id: NonEmptyStr
    deployment_id: NonEmptyStr
    signal_id: NonEmptyStr
    leg_no: int = Field(default=0, ge=0)
    venue: NonEmptyStr
    instrument: NonEmptyStr
    side: Literal["buy", "sell"]
    quantity: PositiveDecimal
    limit_price: PositiveDecimal
    time_in_force: Literal["GTC", "IOC", "FOK", "GTD"]
    strategy_hash: Sha256
    policy_hash: Sha256
    fee_schedule_hash: Sha256
    instrument_spec_hash: Sha256
    risk_reservation_id: NonEmptyStr
    created_at_ns: TimestampNS
    valid_until_ns: TimestampNS
    reduce_only: bool = False

    @model_validator(mode="after")
    def valid_interval(self) -> OrderIntent:
        if self.valid_until_ns <= self.created_at_ns:
            raise ValueError("order validity interval must be nonempty")
        return self


class OrderIdentity(Contract):
    venue: NonEmptyStr
    account_id: NonEmptyStr
    intent_id: NonEmptyStr
    venue_order_id: str | None = None


class SubmitResult(Contract):
    status: Literal["accepted", "rejected", "unknown"]
    identity: OrderIdentity
    received_at_ns: TimestampNS
    reason: str | None = None


class CancelResult(Contract):
    status: Literal["confirmed", "rejected", "unknown"]
    identity: OrderIdentity
    received_at_ns: TimestampNS
    reason: str | None = None


class OrderSnapshot(Contract):
    identity: OrderIdentity
    status: Literal[
        "CREATED", "RESERVED", "SUBMITTING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED",
        "REJECTED", "CANCEL_PENDING", "CANCELED", "EXPIRED", "UNKNOWN",
    ]
    settlement_status: Literal[
        "NOT_REQUIRED", "PENDING", "CONFIRMED", "FAILED", "REORGED",
    ] = "NOT_REQUIRED"
    quantity: PositiveDecimal
    filled_quantity: NonNegativeDecimal
    asof_ns: TimestampNS

    @model_validator(mode="after")
    def bounded_fill(self) -> OrderSnapshot:
        if self.filled_quantity > self.quantity:
            raise ValueError("filled quantity exceeds order quantity")
        if self.status == "FILLED" and self.filled_quantity != self.quantity:
            raise ValueError("filled orders must have their complete quantity")
        if self.status == "PARTIALLY_FILLED" and not 0 < self.filled_quantity < self.quantity:
            raise ValueError("partially filled orders require a positive incomplete fill")
        return self


class ReconcileCursor(Contract):
    venue: NonEmptyStr
    account_id: NonEmptyStr
    cursor: str | None = None


class ReconcileBatch(Contract):
    next_cursor: ReconcileCursor
    orders: tuple[OrderSnapshot, ...]
    fills: tuple[FrozenDict, ...]
    complete: bool


class StrategyState(Contract):
    values: FrozenDict = Field(default_factory=FrozenDict)


class Position(Contract):
    instrument: NonEmptyStr
    quantity: DecimalString
    mark_price: PositiveDecimal


class PortfolioSnapshot(Contract):
    account_id: NonEmptyStr
    asof_ns: TimestampNS
    currency: NonEmptyStr
    net_asset_value: NonNegativeDecimal
    positions: tuple[Position, ...]
    balances: FrozenDict
    trusted: bool


class ExecutableMarketSnapshot(Contract):
    venue: NonEmptyStr
    instrument: NonEmptyStr
    bid: PositiveDecimal
    ask: PositiveDecimal
    asof_ns: TimestampNS
    book_valid: bool
    instrument_spec_hash: Sha256
    fee_schedule_hash: Sha256

    @model_validator(mode="after")
    def valid_spread(self) -> ExecutableMarketSnapshot:
        if self.bid >= self.ask:
            raise ValueError("executable book must have a positive spread")
        return self


class RiskDecision(Contract):
    approved: bool
    reason_codes: tuple[str, ...] = Field(min_length=1)
    reservation_id: str | None = None
    order_intent: OrderIntent | None = None

    @model_validator(mode="after")
    def reservation_required(self) -> RiskDecision:
        if self.approved and (not self.reservation_id or self.order_intent is None):
            raise ValueError("approved risk decisions require a reserved immutable order")
        if self.order_intent is not None and self.reservation_id != self.order_intent.risk_reservation_id:
            raise ValueError("risk decision and order reservation must match")
        if not self.approved and self.order_intent is not None:
            raise ValueError("rejected risk decisions cannot contain executable orders")
        return self


class ModelRequest(Contract):
    run_id: NonEmptyStr
    model_id: NonEmptyStr
    prompt_hash: Sha256
    prompt: str = Field(min_length=1, max_length=1_000_000)
    tools_hash: Sha256
    evidence_hashes: tuple[Sha256, ...] = ()


class CallBudget(Contract):
    maximum_cost_usd: NonNegativeDecimal
    maximum_input_tokens: int = Field(ge=1)
    maximum_output_tokens: int = Field(ge=1)
    deadline_ns: TimestampNS


class ModelResult(Contract):
    provider: NonEmptyStr
    model_id: NonEmptyStr
    output: FrozenDict
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: NonNegativeDecimal
    raw_artifact_hash: Sha256
    raw_response: FrozenDict | None = None


class ExperimentResult(Contract):
    experiment_hash: Sha256
    status: Literal["completed", "failed", "canceled"]
    metrics: tuple[tuple[str, MetricResult], ...]
    artifact_hashes: tuple[Sha256, ...]
    reason: str | None = None
