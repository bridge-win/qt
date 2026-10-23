"""V2 immutable research bridge to the existing real Nautilus dataframe engine.

This bar adapter runs ONE frozen chronological test sleeve. It does not claim
L2 fills, fixed-bps slippage, shared-capital multi-asset execution, CPCV fitting
or real venue readiness. Unsupported assumptions fail rather than disappearing.
Native dependencies are imported only when a native experiment is executed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import os
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from qt.contracts import (
    ExperimentSpecV2,
    FeeSchedule,
    InstrumentSpec,
    StrategyArtifact,
    content_hash,
)
from qt.datasets import DatasetRegistry
from qt.experiments.metrics import net_equity_metrics

_TIMEFRAMES = {
    60_000_000_000: "1m", 300_000_000_000: "5m", 900_000_000_000: "15m",
    3_600_000_000_000: "1h", 14_400_000_000_000: "4h", 86_400_000_000_000: "1d",
}
_KINDS = {
    "sma_crossover", "ema_crossover", "time_series_momentum", "rate_of_change",
    "rsi_mean_reversion", "bollinger_mean_reversion", "buy_and_hold",
}


def builtin_strategy_code_hash(kind: str) -> str:
    """Hash the registered strategy implementation and shared strategy sources.

    Engine/adapter code remains separately bound by the release image and the
    bridge hash in raw evidence. This is not a hash supplied by the model.
    """
    from btc_backtest.strategies.registry import default_strategy_registry

    if kind not in _KINDS:
        raise ValueError("unsupported_native_builtin_strategy")
    factory = default_strategy_registry().factories[kind]
    registry_path = Path(inspect.getfile(factory)).resolve()
    digest = hashlib.sha256()
    for source in sorted(registry_path.parent.rglob("*.py")):
        digest.update(str(source.relative_to(registry_path.parent)).encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
    return digest.hexdigest()


class _BudgetedBuiltin:
    """Deterministic pre-order sizing; matching and accounting stay in Nautilus.

    This overlay enforces spot/no-borrow and capital/position/order bounds.
    Portfolio drawdown/stop-loss/availability acceptance remains a separate
    risk test; the adapter never claims the full production risk gate passed.
    """

    def __init__(self, source: Any, artifact: StrategyArtifact, instrument: InstrumentSpec,
                 taker_fee: Decimal, *, benchmark: bool = False) -> None:
        self.source = source
        self.artifact = artifact
        self.instrument = instrument
        self.taker_fee = taker_fee
        self.benchmark = benchmark
        maximum = Decimal("1") if benchmark else artifact.risk_budget.maximum_position_fraction
        source.metadata = source.metadata.model_copy(update={"max_weight": maximum})
        self.metadata = source.metadata
        self.parameters = source.parameters
        self.rejected: list[dict[str, object]] = []

    def initialize(self, context: Any) -> None:
        self.source.initialize(context)

    def on_bar(self, context: Any) -> tuple[Any, ...]:
        from btc_backtest.engine.models import InstrumentKind, OrderSide

        output = []
        for intent in self.source.on_bar(context):
            if intent.instrument != InstrumentKind.SPOT:
                raise ValueError("non_spot_native_intent")
            mark = Decimal(str(context.current_bar["close"]))
            if intent.side == OrderSide.BUY:
                current = context.portfolio.position(InstrumentKind.SPOT).quantity * mark
                budget = self.artifact.risk_budget
                max_position = budget.capital if self.benchmark else budget.capital * budget.maximum_position_fraction
                max_order = budget.capital if self.benchmark else budget.capital * budget.maximum_order_fraction
                amount = min(intent.quote_amount or Decimal("0"), max_order,
                             max(Decimal("0"), max_position - current),
                             context.portfolio.cash / (1 + self.taker_fee))
                quantity = amount / mark / (1 + self.taker_fee)
            else:
                quantity = min(intent.base_quantity or Decimal("0"),
                               context.portfolio.position(InstrumentKind.SPOT).quantity)
                if not self.benchmark:
                    quantity = min(quantity, self.artifact.risk_budget.capital
                                   * self.artifact.risk_budget.maximum_order_fraction / mark)
            increment = self.instrument.quantity_increment
            quantity = (quantity / increment).to_integral_value(rounding=ROUND_DOWN) * increment
            if quantity < self.instrument.minimum_quantity or quantity * mark < self.instrument.minimum_notional:
                self.rejected.append({"timestamp": context.timestamp.isoformat(), "reason": "below_native_instrument_minimum"})
                continue
            # Freeze a base quantity before native submission. Fees are reserved
            # above; the native engine may still reject a delayed adverse fill.
            output.append(intent.model_copy(update={"quote_amount": None, "base_quantity": quantity}))
        return tuple(output)

    def finalize(self, context: Any) -> None:
        self.source.finalize(context)


def _select(values: Sequence[Any] | Mapping[str, Any], identity: str, *, fee: bool = False) -> Any:
    iterable = values.values() if isinstance(values, Mapping) else values
    matches = [item for item in iterable if (content_hash(item) if fee else item.instrument) == identity]
    if len(matches) != 1:
        raise ValueError("native_fee_or_instrument_identity_missing_or_ambiguous")
    return matches[0]


def _strategy_parameters(artifact: StrategyArtifact) -> tuple[str, dict[str, object]]:
    parameters = dict(artifact.parameters)
    kind = str(parameters.pop("kind", artifact.strategy_id))
    if kind not in _KINDS:
        raise ValueError("unsupported_native_builtin_strategy")
    # The public artifact can use concise fast/slow names for MA strategies;
    # canonical native keys remain explicit in the immutable raw result.
    if kind in {"sma_crossover", "ema_crossover"}:
        for alias, canonical in (("fast", "fast_window"), ("slow", "slow_window")):
            if alias in parameters:
                if canonical in parameters:
                    raise ValueError("ambiguous_native_strategy_parameter_alias")
                parameters[canonical] = parameters.pop(alias)
    if artifact.code_hash != builtin_strategy_code_hash(kind):
        raise ValueError("registered_native_strategy_code_hash_mismatch")
    return kind, parameters


def run(
    spec: ExperimentSpecV2,
    strategy: StrategyArtifact,
    datasets: DatasetRegistry,
    instruments: Sequence[InstrumentSpec] | Mapping[str, InstrumentSpec],
    fees: Sequence[FeeSchedule] | Mapping[str, FeeSchedule],
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Execute one approved native research input, returning strict JSON evidence.

    The calling worker must authorize development/holdout reads before invoking
    this trusted engine boundary. The adapter never opens arbitrary code paths.
    """
    if cancelled():
        raise InterruptedError("native_experiment_cancelled")
    if spec.engine.name != "nautilus":
        raise ValueError("experiment_engine_is_not_nautilus")
    installed = importlib.metadata.version("nautilus_trader")
    if installed != spec.engine.version or installed != "2.0.0rc4":
        raise ValueError("native_engine_version_mismatch_or_unaccepted_upgrade")
    if len(spec.instruments) != 1 or len(spec.dataset_ids) != 1 or len(spec.timeframes_ns) != 1:
        raise ValueError("native_adapter_requires_one_instrument_dataset_timeframe_per_sleeve")
    if spec.split.method not in {"holdout", "walk_forward"} or spec.split.folds != 1:
        raise ValueError("native_adapter_requires_one_chronological_fold_worker_must_orchestrate_splits")
    if spec.strategy_version not in {content_hash(strategy), f"{strategy.strategy_id}@{strategy.version}"}:
        raise ValueError("native_strategy_version_binding_mismatch")
    if "nautilus" not in strategy.engine_compatibility and installed not in strategy.engine_compatibility:
        raise ValueError("strategy_does_not_declare_native_compatibility")
    if strategy.instruments != spec.instruments or strategy.signal_interval_ns != spec.timeframes_ns[0]:
        raise ValueError("native_strategy_instrument_or_interval_mismatch")
    if set(strategy.feature_dependencies) - {"open", "high", "low", "close", "volume"}:
        raise ValueError("native_bar_builtin_cannot_supply_declared_external_features")
    if strategy.exit_rules and dict(strategy.exit_rules) != {"mode": "native_strategy"}:
        raise ValueError("native_builtin_adapter_cannot_ignore_custom_exit_rules")
    if spec.cost_model.slippage_bps != 0:
        raise ValueError("fixed_bps_slippage_not_supported_by_native_bar_model")
    if spec.cost_model.funding_enabled or spec.cost_model.borrow_enabled or spec.cost_model.capital_cost_bps_per_day != 0:
        raise ValueError("native_bar_model_requires_explicit_funding_borrow_capital_cost_data")
    if spec.fill_model.name not in {"native_bar", "nautilus_bar", "bar"}:
        raise ValueError("unsupported_native_fill_model")
    fill_parameters = dict(spec.fill_model.parameters)
    if set(fill_parameters) - {"one_tick_slippage", "limit_fill_probability", "slippage_probability"}:
        raise ValueError("unsupported_native_fill_parameter")
    if "one_tick_slippage" in fill_parameters and type(fill_parameters["one_tick_slippage"]) is not bool:
        raise ValueError("native_one_tick_slippage_requires_boolean")
    for probability in ("limit_fill_probability", "slippage_probability"):
        if probability in fill_parameters and (
            type(fill_parameters[probability]) not in (int, float)
            or not 0 <= fill_parameters[probability] <= 1
        ):
            raise ValueError("native_fill_probability_requires_numeric_unit_interval")
    if fill_parameters.get("slippage_probability", 0) and not fill_parameters.get("one_tick_slippage"):
        raise ValueError("slippage_probability_requires_explicit_one_tick_model")
    if spec.latency_model.parameters:
        raise ValueError("native_adapter_only_supports_declared_static_latency")
    if len(spec.cost_model.fee_schedule_hashes) != 1:
        raise ValueError("native_adapter_requires_one_constant_fee_schedule")
    if set(spec.benchmarks) - {"buy_and_hold", "btc_buy_and_hold", "btc_hold", "risk_matched_buy_and_hold", "cash"}:
        raise ValueError("unsupported_native_benchmark")
    interval = spec.timeframes_ns[0]
    if interval not in _TIMEFRAMES:
        raise ValueError("native_builtin_initialization_timeframe_not_supported")
    manifest = datasets.load(spec.dataset_ids[0])
    if manifest.instrument != spec.instruments[0] or manifest.data_type not in {"bar", "bars", "ohlcv"}:
        raise ValueError("native_dataset_instrument_or_bar_type_mismatch")
    if manifest.expected_interval_ns != interval:
        raise ValueError("native_dataset_interval_mismatch")
    instrument = _select(instruments, manifest.instrument)
    if spec.instrument_spec_hashes and (
        len(spec.instrument_spec_hashes) != 1
        or content_hash(instrument) != spec.instrument_spec_hashes[0]
    ):
        raise ValueError("native_instrument_version_hash_mismatch")
    fee = _select(fees, spec.cost_model.fee_schedule_hashes[0], fee=True)
    if instrument.instrument_type != "spot" or strategy.risk_budget.maximum_leverage != 1:
        raise ValueError("native_v2_adapter_is_unleveraged_spot_only")
    if (instrument.venue != manifest.venue or fee.venue != manifest.venue
            or instrument.quote_asset != instrument.settlement_asset
            or fee.fee_asset != instrument.quote_asset or fee.fee_basis != "notional"
            or strategy.risk_budget.currency != instrument.quote_asset
            or instrument.contract_multiplier != 1):
        raise ValueError("native_instrument_fee_currency_or_notional_semantics_mismatch")
    if fee.maker_rate < 0 or fee.taker_rate < 0 or fee.fee_rules or fee.funding_rules:
        raise ValueError("native_adapter_requires_simple_nonnegative_notional_fees")
    start, end = spec.split.validation_end_ns, spec.split.test_end_ns
    total_submission_delay = (spec.latency_model.decision_ns + spec.latency_model.submission_ns
                              + spec.latency_model.exchange_ns)
    for contract in (instrument, fee):
        if contract.effective_from_ns > start or (contract.effective_to_ns is not None
                and contract.effective_to_ns <= end + total_submission_delay + spec.latency_model.feed_ns):
            raise ValueError("native_instrument_or_fee_not_valid_for_entire_test_window")
    image_digest = os.environ.get("QT_EXPERIMENT_IMAGE_DIGEST")
    if image_digest and image_digest != spec.engine.image_digest:
        raise ValueError("native_runtime_image_digest_mismatch")

    import pandas as pd
    from btc_backtest.strategies.registry import default_strategy_registry

    from qt.nautilus.executor import _decision_traces, _initializer, _json_contract_value
    from qt.nautilus.models import ExecutionAssumptions
    from qt.nautilus.runner import InstrumentDefinition, NautilusDataFrameRunner

    bars, observed_delays, quality = [], set(), set()
    for event in datasets.events(manifest.dataset_id):
        if cancelled():
            raise InterruptedError("native_experiment_cancelled")
        if event.exchange_ts_ns > end:
            break
        if event.exchange_ts_ns < spec.split.training_start_ns:
            continue
        if event.event_type != "bar":
            raise ValueError("non_bar_event_in_native_bar_dataset")
        delay = event.received_ts_ns - event.exchange_ts_ns
        if delay < 0:
            raise ValueError("native_bar_observable_before_close")
        observed_delays.add(delay)
        quality.update(event.quality_flags)
        bars.append({"timestamp": event.received_ts_ns + spec.latency_model.feed_ns,
                     **{field: float(event.payload[field]) for field in ("open", "high", "low", "close", "volume")}})
    if len(observed_delays) != 1:
        raise ValueError("native_bar_adapter_cannot_model_variable_receipt_delay")
    if len(bars) < 3:
        raise ValueError("insufficient_native_bar_window")
    feed_offset = observed_delays.pop() + spec.latency_model.feed_ns
    if feed_offset % 1000:
        raise ValueError("native_datetime_strategy_context_requires_microsecond_aligned_feed_times")
    if feed_offset % interval:
        raise ValueError("native_initializer_requires_bar_aligned_constant_receipt_delay")
    for contract in (instrument, fee):
        if (contract.effective_to_ns is not None
                and contract.effective_to_ns <= end + feed_offset + total_submission_delay):
            raise ValueError("native_instrument_or_fee_expires_before_delayed_execution")
    frame = pd.DataFrame(bars)
    frame.index = pd.to_datetime(frame.pop("timestamp"), unit="ns", utc=True)
    if any(int(delta.value) != interval for delta in frame.index.to_series().diff().dropna()):
        raise ValueError("native_bar_window_contains_gaps")
    if frame.index[-1].value != end + feed_offset:
        raise ValueError("native_test_end_is_not_covered_by_a_closed_bar")
    decision_start = pd.Timestamp(start + feed_offset + 1, unit="ns", tz="UTC")
    if len(frame.loc[decision_start:]) < 2:
        raise ValueError("insufficient_native_test_bars")
    kind, parameters = _strategy_parameters(strategy)
    source = default_strategy_registry().create(kind, parameters)
    warmup = int((frame.index < decision_start).sum())
    if warmup < source.metadata.warmup_bars:
        raise ValueError("insufficient_native_training_warmup_before_test")
    assumptions = ExecutionAssumptions(
        initial_cash=strategy.risk_budget.capital, maker_fee=fee.maker_rate,
        taker_fee=fee.taker_rate, slippage_bps=spec.cost_model.slippage_bps,
        one_tick_slippage=bool(fill_parameters.get("one_tick_slippage", False)),
        limit_fill_probability=float(fill_parameters.get("limit_fill_probability", 1.0)),
        slippage_probability=float(fill_parameters.get("slippage_probability", 1.0 if fill_parameters.get("one_tick_slippage") else 0.0)),
        latency_nanos=total_submission_delay, seed=spec.seed,
    )
    definition = InstrumentDefinition(
        symbol=f"{instrument.base_asset}{instrument.quote_asset}", venue=instrument.venue.upper(),
        base_currency=instrument.base_asset, quote_currency=instrument.quote_asset,
        kind="spot", price_precision=max(0, -instrument.price_increment.as_tuple().exponent),
        size_precision=max(0, -instrument.quantity_increment.as_tuple().exponent),
        price_increment=instrument.price_increment, size_increment=instrument.quantity_increment,
        timestamp_at_close=True,
    )
    descriptor = {"provider": manifest.venue, "symbol": f"{instrument.base_asset}/{instrument.quote_asset}",
                  "timeframe": _TIMEFRAMES[interval], "market": "spot"}

    def execute(native_source: Any, *, benchmark: bool = False) -> dict[str, Any]:
        bounded = _BudgetedBuiltin(native_source, strategy, instrument, fee.taker_rate, benchmark=benchmark)
        native = NautilusDataFrameRunner().run(
            frame=frame, strategy=bounded, assumptions=assumptions, instrument=definition,
            initialize=_initializer(bounded, {"strategy_params": dict(native_source.parameters)}, descriptor, frame, assumptions),
            decision_start=decision_start, cancelled=cancelled,
        )
        if native.engine_version != installed or native.account_invariants.get("status") != "verified":
            raise ValueError("native_engine_or_cash_ledger_attestation_failed")
        nav = {start + feed_offset: str(native.initial_equity)}
        for trace in native.traces:
            observed = dict(trace.observed_values)
            if "native_equity" not in observed:
                raise ValueError("native_trace_missing_marked_equity")
            nav[int(pd.Timestamp(trace.timestamp).value)] = str(observed["native_equity"])
        nav[int(pd.Timestamp(native.terminal_timestamp).value)] = str(native.terminal_equity)
        timestamps = sorted(nav)
        metrics = net_equity_metrics([float(nav[t]) for t in timestamps], timestamps)
        undefined: dict[str, str] = {}

        def report_frame(value: pd.DataFrame) -> list[dict[str, object]]:
            return json.loads(value.reset_index().to_json(orient="records", date_format="iso", date_unit="ns"))

        return {
            "nav": [{"timestamp_ns": timestamp, "equity": nav[timestamp]} for timestamp in timestamps],
            "metrics": metrics, "decision_traces": _decision_traces(native.traces),
            "orders": report_frame(native.orders), "fills": report_frame(native.fills),
            "positions": report_frame(native.positions), "account": report_frame(native.account),
            "account_invariants": dict(native.account_invariants),
            "realized_costs": _json_contract_value(native.metrics["costs"], path="costs", undefined=undefined),
            "undefined_native_statistics": undefined,
            "risk_rejected_intents": bounded.rejected,
        }

    result = execute(source)
    benchmarks: dict[str, Any] = {}
    for requested in spec.benchmarks:
        if requested in {"buy_and_hold", "btc_buy_and_hold", "btc_hold"}:
            benchmarks[requested] = execute(default_strategy_registry().create("buy_and_hold", {}), benchmark=True)
        elif requested == "risk_matched_buy_and_hold":
            benchmarks[requested] = execute(default_strategy_registry().create("buy_and_hold", {}))
            benchmarks[requested]["matching"] = "same_capital_position_and_order_caps_not_volatility_matched"
        elif requested == "cash":
            benchmarks[requested] = {"metrics": net_equity_metrics(
                [float(strategy.risk_budget.capital)] * len(result["nav"]),
                [point["timestamp_ns"] for point in result["nav"]],
            )}
        else:
            raise ValueError("unsupported_native_benchmark")
    payload = {
        "schema_version": "2.0", "engine": spec.engine.model_dump(mode="json"),
        "experiment_hash": content_hash(spec), "strategy_hash": content_hash(strategy),
        "dataset_manifest_hash": content_hash(manifest),
        "instrument_hash": content_hash(instrument), "fee_hash": content_hash(fee),
        "native_builtin": {"kind": kind, "parameters": parameters},
        "bridge_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "image_attestation": "verified_environment" if image_digest else "unavailable_local_runtime",
        "native_version": installed, "seed": spec.seed,
        "window": {"start_exclusive_ns": start, "end_inclusive_ns": end,
                   "training_start_ns": spec.split.training_start_ns, "warmup_bars": warmup},
        "bar_timestamp_semantics": "native_event_and_init_at_observable_closed_bar",
        "actual_receipt_delay_ns": feed_offset - spec.latency_model.feed_ns,
        "quality_flags": sorted(quality), "result": result, "benchmarks": benchmarks,
        "limitations": ["bar_matching_not_order_book_execution", "no_market_impact_model",
                        "drawdown_stop_loss_and_stress_acceptance_not_evaluated",
                        "single_frozen_test_fold_not_parameter_selection_validation"],
        "live_ready": False,
    }
    return {**payload, "sha256": content_hash(payload)}
