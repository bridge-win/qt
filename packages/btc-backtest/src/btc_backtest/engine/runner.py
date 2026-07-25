"""Deterministic point-in-time event loop."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import cast

import pandas as pd

from btc_backtest import __version__
from btc_backtest.data.models import DataManifest, MarketBundle
from btc_backtest.engine.accounting import Portfolio
from btc_backtest.engine.fills import BarFillModel, FillPolicy
from btc_backtest.engine.models import (
    BacktestResult,
    BacktestSpec,
    Fill,
    FundingEvent,
    InstrumentKind,
    Order,
    OrderIntent,
    OrderStatus,
    PortfolioSnapshot,
)
from btc_backtest.errors import ExecutionError
from btc_backtest.signals.models import RankedSignal, SignalQuery
from btc_backtest.signals.ranking import SignalAggregator
from btc_backtest.signals.store import SignalStore
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
)


class EventRunner:
    """Execute a strategy once over an immutable point-in-time market bundle."""

    def __init__(
        self,
        fill_model: BarFillModel | None = None,
        *,
        signal_store: SignalStore | None = None,
        signal_aggregator: SignalAggregator | None = None,
    ) -> None:
        self._fill_model = fill_model
        self._signal_store = signal_store
        self._signal_aggregator = signal_aggregator or SignalAggregator()

    def run(
        self,
        spec: BacktestSpec,
        bundle: MarketBundle,
        strategy: Strategy,
    ) -> BacktestResult:
        frame = bundle.primary.frame
        _validate_run_contract(spec, bundle, strategy)
        manifests = _ordered_manifests(bundle)
        run_id = _run_id(spec, manifests, strategy)
        fill_model = self._fill_model or BarFillModel(
            FillPolicy(
                fee_bps=spec.fee_bps,
                slippage_bps=spec.slippage_bps,
                intrabar_policy=spec.intrabar_policy,
            )
        )
        portfolio = Portfolio(spec.initial_cash)
        orders: list[Order] = []
        fills: list[Fill] = []
        snapshots: list[PortfolioSnapshot] = []
        warnings: list[str] = []
        signal_ids: list[str] = []
        order_counter = 0
        fill_counter = 0
        funding_counter = 0
        previous_timestamp: datetime | None = None

        initialization = InitializationContext(
            spec=spec,
            data_manifests=manifests,
            parameters=spec.strategy_params,
        )
        _initialize(strategy, initialization, spec.data.start)

        for pandas_timestamp, row in frame.iterrows():
            timestamp = cast(pd.Timestamp, pandas_timestamp).to_pydatetime()
            primary_bar = _bar_mapping(row)
            instrument_bars = _instrument_bars(
                bundle,
                cast(pd.Timestamp, pandas_timestamp),
                primary_bar,
            )
            funding_counter = _apply_available_funding(
                bundle=bundle,
                portfolio=portfolio,
                timestamp=timestamp,
                previous_timestamp=previous_timestamp,
                fallback_price=_decimal(row["open"], "open"),
                run_id=run_id,
                next_counter=funding_counter,
            )
            fill_counter = _process_open_orders(
                timestamp=timestamp,
                bars=instrument_bars,
                portfolio=portfolio,
                fill_model=fill_model,
                orders=orders,
                fills=fills,
                warnings=warnings,
                run_id=run_id,
                next_fill_counter=fill_counter,
            )
            close = _decimal(row["close"], "close")
            marks: dict[str | InstrumentKind, Decimal] = {
                InstrumentKind.SPOT: close
            }
            perpetual_bar = instrument_bars.get(InstrumentKind.PERPETUAL)
            if perpetual_bar is not None:
                marks[InstrumentKind.PERPETUAL] = _decimal(
                    perpetual_bar["close"],
                    "perpetual close",
                )
            snapshot = portfolio.mark(timestamp, marks)
            snapshots.append(snapshot)

            history = frame.loc[:pandas_timestamp]
            if len(history) >= strategy.metadata.warmup_bars:
                ranked_signals = self._rank_signals(
                    spec=spec,
                    strategy=strategy,
                    timestamp=timestamp,
                )
                context = StrategyContext(
                    timestamp=timestamp,
                    bars=history,
                    auxiliary={
                        name: dataset.frame.loc[
                            dataset.frame.index <= pandas_timestamp
                        ]
                        for name, dataset in bundle.auxiliary.items()
                    },
                    portfolio=snapshot,
                    open_orders=tuple(
                        order
                        for order in orders
                        if order.status
                        in (
                            OrderStatus.OPEN,
                            OrderStatus.PARTIALLY_FILLED,
                        )
                    ),
                    signals=ranked_signals,
                    parameters=spec.strategy_params,
                )
                intents = _on_bar(strategy, context)
                _validate_atomic_intents(intents)
                context_signal_ids = _signal_observation_ids(ranked_signals)
                for intent in intents:
                    order_counter += 1
                    order = _intent_to_order(
                        intent=intent,
                        timestamp=timestamp,
                        close=marks.get(intent.instrument),
                        run_id=run_id,
                        counter=order_counter,
                        strategy=strategy,
                        context_signal_ids=context_signal_ids,
                    )
                    orders.append(order)
                    for signal_id in order.signal_ids:
                        if signal_id not in signal_ids:
                            signal_ids.append(signal_id)
            previous_timestamp = timestamp

        terminal = snapshots[-1]
        _finalize(
            strategy,
            FinalizationContext(
                spec=spec,
                portfolio=terminal,
                orders=tuple(orders),
                fills=tuple(fills),
                warnings=tuple(warnings),
            ),
            terminal.timestamp,
        )
        return BacktestResult(
            run_id=run_id,
            strategy_id=strategy.metadata.id,
            data_manifests=manifests,
            orders=tuple(orders),
            fills=tuple(fills),
            positions=terminal.positions,
            snapshots=tuple(snapshots),
            trades=(),
            signal_ids=tuple(signal_ids),
            diagnostics={
                "engine_version": __version__,
                "seed": spec.seed,
                "funding_events": funding_counter,
                "intrabar_policy": fill_model.policy.intrabar_policy,
            },
            warnings=tuple(warnings),
        )

    def _rank_signals(
        self,
        *,
        spec: BacktestSpec,
        strategy: Strategy,
        timestamp: datetime,
    ) -> tuple[RankedSignal, ...]:
        if (
            self._signal_store is None
            or not strategy.metadata.signal_dependencies
        ):
            return ()
        observations = self._signal_store.query(
            SignalQuery(
                start=spec.data.start,
                end=spec.data.end,
                symbol=spec.data.symbol,
                horizons=(spec.data.timeframe,),
                source_types=strategy.metadata.signal_dependencies,
            ),
            available_at=timestamp,
        )
        return self._signal_aggregator.rank(observations, as_of=timestamp)


def _validate_run_contract(
    spec: BacktestSpec,
    bundle: MarketBundle,
    strategy: Strategy,
) -> None:
    if not isinstance(strategy, Strategy):
        raise ExecutionError("strategy does not satisfy the Strategy protocol")
    if strategy.metadata.id != spec.strategy:
        raise ExecutionError(
            f"strategy id {strategy.metadata.id} does not match spec "
            f"{spec.strategy}"
        )
    if spec.data.timeframe not in strategy.metadata.supported_timeframes:
        raise ExecutionError(
            f"strategy {strategy.metadata.id} does not support timeframe "
            f"{spec.data.timeframe}"
        )
    frame = bundle.primary.frame
    manifest = bundle.primary.manifest
    if manifest.symbol != spec.data.symbol:
        raise ExecutionError(
            f"primary manifest symbol {manifest.symbol} does not match "
            f"{spec.data.symbol}"
        )
    if manifest.timeframe != spec.data.timeframe:
        raise ExecutionError(
            f"primary manifest timeframe {manifest.timeframe} does not match "
            f"{spec.data.timeframe}"
        )
    if frame.empty:
        raise ExecutionError("primary market data is empty")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ExecutionError(
            "primary market data requires a timezone-aware DatetimeIndex"
        )
    if not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ExecutionError(
            "primary market data timestamps must be unique and ascending"
        )
    missing = [
        field
        for field in strategy.metadata.required_fields
        if field not in frame.columns
    ]
    if missing:
        raise ExecutionError(
            f"strategy {strategy.metadata.id} requires missing fields: "
            f"{', '.join(missing)}"
        )
    for name, dataset in bundle.auxiliary.items():
        index = dataset.frame.index
        if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
            raise ExecutionError(
                f"auxiliary data {name} requires a timezone-aware DatetimeIndex"
            )
        if not index.is_monotonic_increasing or not index.is_unique:
            raise ExecutionError(
                f"auxiliary data {name} timestamps must be unique and ascending"
            )


def _ordered_manifests(bundle: MarketBundle) -> tuple[DataManifest, ...]:
    return (
        bundle.primary.manifest,
        *(
            bundle.auxiliary[name].manifest
            for name in sorted(bundle.auxiliary)
        ),
    )


def _run_id(
    spec: BacktestSpec,
    manifests: tuple[DataManifest, ...],
    strategy: Strategy,
) -> str:
    payload = {
        "engine_version": __version__,
        "strategy": {
            "id": strategy.metadata.id,
            "version": strategy.metadata.version,
            "api_version": strategy.metadata.api_version,
        },
        "spec": spec.model_dump(mode="json"),
        "manifests": [
            {
                "market": manifest.market,
                "provider": manifest.provider,
                "symbol": manifest.symbol,
                "timeframe": manifest.timeframe,
                "normalized_sha256": manifest.normalized_sha256,
            }
            for manifest in manifests
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _initialize(
    strategy: Strategy,
    context: InitializationContext,
    timestamp: datetime,
) -> None:
    try:
        strategy.initialize(context)
    except Exception as error:
        raise _strategy_error(strategy, timestamp, "initialize") from error


def _on_bar(
    strategy: Strategy,
    context: StrategyContext,
) -> tuple[OrderIntent, ...]:
    try:
        returned = strategy.on_bar(context)
        if isinstance(returned, (str, bytes)) or not isinstance(
            returned,
            Sequence,
        ):
            raise TypeError("on_bar must return a sequence of OrderIntent")
        intents = tuple(returned)
        if any(not isinstance(intent, OrderIntent) for intent in intents):
            raise TypeError("on_bar returned a value that is not OrderIntent")
        return intents
    except Exception as error:
        raise _strategy_error(
            strategy,
            context.timestamp,
            "on_bar",
        ) from error


def _finalize(
    strategy: Strategy,
    context: FinalizationContext,
    timestamp: datetime,
) -> None:
    try:
        strategy.finalize(context)
    except Exception as error:
        raise _strategy_error(strategy, timestamp, "finalize") from error


def _strategy_error(
    strategy: Strategy,
    timestamp: datetime,
    phase: str,
) -> ExecutionError:
    return ExecutionError(
        f"strategy {strategy.metadata.id} failed during {phase} at "
        f"{timestamp.isoformat()}"
    )


def _validate_atomic_intents(intents: tuple[OrderIntent, ...]) -> None:
    groups: dict[str, list[OrderIntent]] = defaultdict(list)
    for intent in intents:
        if intent.group_id is not None:
            groups[intent.group_id].append(intent)
    for group_id, members in groups.items():
        atomic = [member.atomic_group for member in members]
        if any(atomic) and (not all(atomic) or len(members) < 2):
            raise ExecutionError(
                f"atomic group {group_id} must contain at least two "
                "consistently atomic intents"
            )


def _intent_to_order(
    *,
    intent: OrderIntent,
    timestamp: datetime,
    close: Decimal | None,
    run_id: str,
    counter: int,
    strategy: Strategy,
    context_signal_ids: tuple[str, ...],
) -> Order:
    if intent.instrument not in strategy.metadata.supported_instruments:
        raise ExecutionError(
            f"strategy {strategy.metadata.id} does not support instrument "
            f"{intent.instrument.value}"
        )
    quantity = intent.base_quantity
    if quantity is None:
        quote_amount = intent.quote_amount
        if quote_amount is None:
            raise ExecutionError("validated intent has no order size")
        if close is None:
            raise ExecutionError(
                f"no current {intent.instrument.value} price for quote-sized order"
            )
        quantity = quote_amount / close
    return Order(
        id=f"{run_id}:order:{counter:08d}",
        created_at=timestamp,
        instrument=intent.instrument,
        side=intent.side,
        order_type=intent.order_type,
        quantity=quantity,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
        take_profit_price=intent.take_profit_price,
        group_id=intent.group_id,
        atomic_group=intent.atomic_group,
        reason=intent.reason,
        signal_ids=_merge_signal_ids(
            intent.signal_ids,
            context_signal_ids,
        ),
    )


def _signal_observation_ids(
    ranked_signals: tuple[RankedSignal, ...],
) -> tuple[str, ...]:
    signal_ids: list[str] = []
    for ranked in ranked_signals:
        for contributor in ranked.contributors:
            if contributor.observation_id not in signal_ids:
                signal_ids.append(contributor.observation_id)
    return tuple(signal_ids)


def _merge_signal_ids(
    explicit: tuple[str, ...],
    context_signal_ids: tuple[str, ...],
) -> tuple[str, ...]:
    merged: list[str] = []
    for signal_id in (*explicit, *context_signal_ids):
        if signal_id not in merged:
            merged.append(signal_id)
    return tuple(merged)


def _process_open_orders(
    *,
    timestamp: datetime,
    bars: Mapping[InstrumentKind, Mapping[str, object]],
    portfolio: Portfolio,
    fill_model: BarFillModel,
    orders: list[Order],
    fills: list[Fill],
    warnings: list[str],
    run_id: str,
    next_fill_counter: int,
) -> int:
    open_orders = [
        order
        for order in orders
        if order.status
        in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
    ]
    atomic_groups: dict[str, list[Order]] = defaultdict(list)
    ordinary: list[Order] = []
    for order in open_orders:
        if order.atomic_group and order.group_id is not None:
            atomic_groups[order.group_id].append(order)
        else:
            ordinary.append(order)

    for order in ordinary:
        if _is_expired(order, timestamp):
            _replace_order(orders, _closed_order(order, OrderStatus.CANCELLED))
            continue
        candidate = _evaluate(
            fill_model,
            order,
            bars.get(order.instrument),
            timestamp,
        )
        if candidate is None:
            continue
        next_fill_counter += 1
        fill = candidate.model_copy(
            update={"id": f"{run_id}:fill:{next_fill_counter:08d}"}
        )
        try:
            portfolio.apply_fill(fill)
        except ExecutionError as error:
            _replace_order(orders, _closed_order(order, OrderStatus.REJECTED))
            warnings.append(f"order {order.id} rejected: {error}")
            continue
        fills.append(fill)
        _replace_order(orders, _closed_order(order, OrderStatus.FILLED))

    for group_id in sorted(atomic_groups):
        members = atomic_groups[group_id]
        if any(_is_expired(order, timestamp) for order in members):
            for order in members:
                _replace_order(
                    orders,
                    _closed_order(order, OrderStatus.CANCELLED),
                )
            continue
        candidates = [
            _evaluate(
                fill_model,
                order,
                bars.get(order.instrument),
                timestamp,
            )
            for order in members
        ]
        if any(candidate is None for candidate in candidates):
            continue
        group_fills: list[Fill] = []
        counter = next_fill_counter
        for candidate in candidates:
            if candidate is None:
                raise AssertionError("atomic candidate unexpectedly missing")
            counter += 1
            group_fills.append(
                candidate.model_copy(
                    update={"id": f"{run_id}:fill:{counter:08d}"}
                )
            )
        preflight = copy.deepcopy(portfolio)
        try:
            for fill in group_fills:
                preflight.apply_fill(fill)
        except ExecutionError as error:
            for order in members:
                _replace_order(
                    orders,
                    _closed_order(order, OrderStatus.REJECTED),
                )
            warnings.append(f"atomic group {group_id} rejected: {error}")
            continue
        for fill in group_fills:
            portfolio.apply_fill(fill)
            fills.append(fill)
        for order in members:
            _replace_order(orders, _closed_order(order, OrderStatus.FILLED))
        next_fill_counter = counter
    return next_fill_counter


def _evaluate(
    fill_model: BarFillModel,
    order: Order,
    bar: Mapping[str, object] | None,
    timestamp: datetime,
) -> Fill | None:
    if bar is None:
        return None
    if order.stop_price is not None and order.take_profit_price is not None:
        bracket = fill_model.evaluate_bracket(order, bar, timestamp)
        return bracket[0] if bracket else None
    return fill_model.evaluate(order, bar, timestamp)


def _closed_order(order: Order, status: OrderStatus) -> Order:
    remaining = Decimal("0") if status is OrderStatus.FILLED else order.remaining_quantity
    return order.model_copy(
        update={
            "status": status,
            "remaining_quantity": remaining,
        }
    )


def _replace_order(orders: list[Order], replacement: Order) -> None:
    for index, order in enumerate(orders):
        if order.id == replacement.id:
            orders[index] = replacement
            return
    raise ExecutionError(f"order {replacement.id} is not registered")


def _is_expired(order: Order, timestamp: datetime) -> bool:
    return order.expires_at is not None and timestamp >= order.expires_at


def _apply_available_funding(
    *,
    bundle: MarketBundle,
    portfolio: Portfolio,
    timestamp: datetime,
    previous_timestamp: datetime | None,
    fallback_price: Decimal,
    run_id: str,
    next_counter: int,
) -> int:
    dataset = bundle.auxiliary.get("funding")
    if dataset is None or "rate" not in dataset.frame.columns:
        return next_counter
    lower_bound = (
        dataset.frame.index <= pd.Timestamp(timestamp)
        if previous_timestamp is None
        else (
            (dataset.frame.index > pd.Timestamp(previous_timestamp))
            & (dataset.frame.index <= pd.Timestamp(timestamp))
        )
    )
    for pandas_timestamp, row in dataset.frame.loc[lower_bound].iterrows():
        mark_value = row.get("mark_price", row.get("close", fallback_price))
        price = _decimal(mark_value, "funding mark price")
        rate = _decimal(row["rate"], "funding rate")
        current = portfolio.mark(
            cast(pd.Timestamp, pandas_timestamp).to_pydatetime(),
            {InstrumentKind.PERPETUAL: price},
        )
        quantity = current.position(InstrumentKind.PERPETUAL).quantity
        if quantity == 0:
            continue
        next_counter += 1
        portfolio.apply_funding(
            FundingEvent(
                id=f"{run_id}:funding:{next_counter:08d}",
                timestamp=cast(pd.Timestamp, pandas_timestamp).to_pydatetime(),
                amount=-(quantity * price * rate),
                rate=rate,
            )
        )
    return next_counter


def _bar_mapping(row: pd.Series) -> Mapping[str, object]:
    return cast(Mapping[str, object], row.to_dict())


def _instrument_bars(
    bundle: MarketBundle,
    timestamp: pd.Timestamp,
    primary_bar: Mapping[str, object],
) -> Mapping[InstrumentKind, Mapping[str, object]]:
    bars: dict[InstrumentKind, Mapping[str, object]] = {
        InstrumentKind.SPOT: primary_bar,
    }
    perpetual = bundle.auxiliary.get("perpetual")
    if perpetual is not None and timestamp in perpetual.frame.index:
        row = perpetual.frame.loc[timestamp]
        if isinstance(row, pd.Series):
            bars[InstrumentKind.PERPETUAL] = _bar_mapping(row)
    return bars


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ExecutionError(f"{field} must be numeric") from error
    if not result.is_finite():
        raise ExecutionError(f"{field} must be finite")
    return result
