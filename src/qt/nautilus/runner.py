"""Low-level, real NautilusTrader dataframe execution for QT research."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import numpy as np
import pandas as pd
from btc_backtest.engine.models import InstrumentKind
from btc_backtest.strategies.base import Strategy

from qt.nautilus.adapter import (
    CausalStrategyBridge,
    NativeFillTransaction,
    NativeStrategyFactory,
    _native_portfolio_state,
)
from qt.nautilus.models import ExecutionAssumptions


class NautilusUnavailableError(RuntimeError):
    """Raised when the pinned native extension is not available on this host."""


class NativeRunCancelledError(RuntimeError):
    """Raised after the native event hook observes research cancellation."""


class NativeLedgerError(RuntimeError):
    """Raised when native fills and account balances do not reconcile."""


@dataclass(frozen=True)
class InstrumentDefinition:
    symbol: str = "BTCUSDT"
    venue: str = "QT"
    base_currency: str = "BTC"
    quote_currency: str = "USDT"
    kind: Literal["spot", "perpetual"] = "spot"
    price_precision: int = 2
    size_precision: int = 8
    price_increment: Decimal = Decimal("0.01")
    size_increment: Decimal = Decimal("0.00000001")
    timestamp_at_close: bool = False


@dataclass(frozen=True)
class NativeRun:
    """Native result reports retained as engine-owned dataframes."""

    engine_version: str
    started_at: datetime
    finished_at: datetime
    result: Mapping[str, object]
    metrics: Mapping[str, object]
    returns: Mapping[int, float]
    orders: pd.DataFrame
    fills: pd.DataFrame
    positions: pd.DataFrame
    account: pd.DataFrame
    traces: tuple[object, ...]
    initial_timestamp: datetime
    initial_equity: Decimal
    terminal_timestamp: datetime
    terminal_equity: Decimal
    account_invariants: Mapping[str, object]


class NautilusDataFrameRunner:
    """Run a normal QT strategy with Nautilus ``BacktestEngine`` only."""

    def run(
        self,
        *,
        frame: pd.DataFrame,
        strategy: Strategy,
        assumptions: ExecutionAssumptions,
        instrument: InstrumentDefinition | None = None,
        initialize: Callable[[], None] | None = None,
        decision_start: pd.Timestamp | None = None,
        cancelled: Callable[[], bool] | None = None,
        mark_quotes: pd.DataFrame | None = None,
    ) -> NativeRun:
        definition = instrument or InstrumentDefinition()
        _validate_frame(frame, assumptions)
        runtime = _runtime()
        if initialize is not None:
            initialize()
        prepared = _utc_frame(frame)
        prepared_quotes = (
            _validate_mark_quotes(mark_quotes)
            if mark_quotes is not None
            else None
        )
        native_instrument, bar_type = _build_instrument(
            runtime,
            prepared,
            definition,
            assumptions,
        )
        bridge = CausalStrategyBridge(strategy, strategy_parameters(strategy))
        instrument_kind = (
            InstrumentKind.PERPETUAL if definition.kind == "perpetual" else InstrumentKind.SPOT
        )
        factory = NativeStrategyFactory(
            bridge,
            frame=prepared,
            instrument_ids={instrument_kind: native_instrument},
            bar_types={instrument_kind: bar_type},
            quote_currency=runtime.Currency.from_str(definition.quote_currency),
            venue=runtime.Venue(definition.venue),
            initial_cash=assumptions.initial_cash,
            decision_start=decision_start,
            cancelled=cancelled,
            taker_fee=assumptions.taker_fee,
            subscribe_quotes=prepared_quotes is not None,
        )
        engine = runtime.BacktestEngine(
            runtime.BacktestEngineConfig(
                trader_id=runtime.TraderId.from_str("QT-RESEARCH-001"),
                bypass_logging=True,
            ),
        )
        venue = runtime.Venue(definition.venue)
        try:
            engine.add_venue(
                venue=venue,
                oms_type=runtime.OmsType.NETTING,
                account_type=(
                    runtime.AccountType.MARGIN
                    if definition.kind == "perpetual"
                    else runtime.AccountType.CASH
                ),
                base_currency=(
                    runtime.Currency.from_str(definition.quote_currency)
                    if definition.kind == "perpetual"
                    else None
                ),
                starting_balances=[
                    runtime.Money.from_str(
                        f"{assumptions.initial_cash} {definition.quote_currency}",
                    )
                ],
                default_leverage=assumptions.leverage,
                fill_model=_fill_model(runtime, assumptions),
                latency_model=runtime.StaticLatencyModel(
                    base_latency_nanos=assumptions.latency_nanos,
                ),
                bar_execution=True,
                bar_adaptive_high_low_ordering=True,
                trade_execution=True,
                queue_position=assumptions.queue_position,
                liquidity_consumption=assumptions.liquidity_consumption,
                liquidation_enabled=definition.kind == "perpetual",
                liquidation_trigger_ratio=assumptions.liquidation_trigger_ratio,
                liquidation_cancel_open_orders=True,
                allow_cash_borrowing=False,
            )
            engine.add_instrument(native_instrument)
            # Quotes are accepted only from the caller's separately attested
            # mark feed.  Never manufacture quote ticks from OHLCV closes.
            if prepared_quotes is not None:
                engine.add_data(_quote_ticks(runtime, prepared_quotes, native_instrument))
            engine.add_data(
                _bars(
                    runtime,
                    prepared,
                    native_instrument,
                    bar_type,
                    timestamp_at_close=definition.timestamp_at_close,
                )
            )
            engine.add_strategy(factory.create())
            engine.run()
            if factory.was_cancelled:
                raise NativeRunCancelledError("native research run cancelled during event dispatch")
            result = engine.get_result()
            orders = engine.generate_orders_report()
            fills = engine.generate_order_fills_report()
            positions = engine.generate_positions_report()
            account = engine.generate_account_report(venue)
            account_invariants = _assert_spot_ledger(
                engine.portfolio,
                venue=venue,
                instrument=native_instrument,
                quote_currency=runtime.Currency.from_str(definition.quote_currency),
                initial_cash=assumptions.initial_cash,
                fills=fills,
                fill_transactions=factory.native_fill_transactions,
                quote_precision=int(runtime.Currency.from_str(definition.quote_currency).precision),
                base_precision=int(native_instrument.base_currency.precision),
                kind=definition.kind,
            )
            terminal_timestamp = prepared.index[-1].to_pydatetime()
            _cash, terminal_equity, _realized_pnl, _unrealized_pnl, _positions = _native_portfolio_state(
                engine.portfolio,
                factory.instrument_ids,
                runtime.Currency.from_str(definition.quote_currency),
                venue,
                assumptions.initial_cash,
                terminal_timestamp,
                Decimal(str(prepared.iloc[-1]["close"])),
            )
            return NativeRun(
                engine_version=str(runtime.version),
                started_at=_native_timestamp(result.backtest_start),
                finished_at=_native_timestamp(result.backtest_end),
                result=dict(result.summary),
                metrics={
                    "pnls": dict(result.stats_pnls),
                    "returns": dict(result.stats_returns),
                    "general": dict(result.stats_general),
                    "costs": _native_costs(
                        fills,
                        assumptions,
                        quote_currency=definition.quote_currency,
                    ),
                    "liquidation": {
                        "status": (
                            "native_enabled"
                            if definition.kind == "perpetual"
                            else "not_applicable"
                        ),
                        "enabled": definition.kind == "perpetual",
                        "maintenance_margin": str(assumptions.maintenance_margin),
                        "trigger_ratio": str(assumptions.liquidation_trigger_ratio),
                        "cancel_open_orders": True,
                    },
                    "mark_data": {
                        "status": "attested_quotes"
                        if prepared_quotes is not None
                        else "unverified_missing",
                        "records": 0 if prepared_quotes is None else len(prepared_quotes),
                    },
                },
                returns=dict(result.returns_series),
                orders=orders,
                fills=fills,
                positions=positions,
                account=account,
                traces=bridge.traces,
                initial_timestamp=prepared.index[0].to_pydatetime(),
                initial_equity=assumptions.initial_cash,
                terminal_timestamp=terminal_timestamp,
                terminal_equity=terminal_equity,
                account_invariants=account_invariants,
            )
        finally:
            engine.dispose()


def strategy_parameters(strategy: Strategy) -> Mapping[str, object]:
    """Read immutable strategy parameters without inventing a parallel config."""
    raw = getattr(strategy, "parameters", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _validate_frame(frame: pd.DataFrame, assumptions: ExecutionAssumptions) -> None:
    if assumptions.data_requirement.value != "bars":
        raise ValueError(
            "queue/consumption assumptions require L2/L3 book plus trade-tick input; "
            "do not run them against OHLCV bars"
        )
    required = {"open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"OHLCV frame misses required columns: {', '.join(missing)}")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("OHLCV input requires a timezone-aware DatetimeIndex")
    if frame.empty or not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ValueError("OHLCV index must be non-empty, unique, and ascending")
    _interval_seconds(frame.index)
    prices = frame.loc[:, ["open", "high", "low", "close"]]
    if not np.isfinite(prices.to_numpy(dtype=float)).all():
        raise ValueError("OHLC prices must be finite")
    if not np.isfinite(frame["volume"].to_numpy(dtype=float)).all():
        raise ValueError("volume must be finite")
    if (prices <= 0).any().any() or (frame["volume"] < 0).any():
        raise ValueError("OHLC prices must be positive and volume non-negative")
    if (
        (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        | (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
    ).any():
        raise ValueError("OHLC rows must satisfy low <= open/close <= high")


def _utc_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy(deep=True)
    result.index = result.index.tz_convert(timezone.utc)
    return result


def _validate_mark_quotes(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate actual observable quotes before admitting them to Nautilus."""
    required = {"available_at", "bid", "ask", "bid_size", "ask_size"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("mark quote frame misses required columns: " + ", ".join(missing))
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("mark quote frame requires an observed_at DatetimeIndex")
    if frame.empty or not frame.index.is_unique:
        raise ValueError("mark quote frame must be non-empty with unique observations")
    result = frame.copy(deep=True)
    result.index = (
        result.index.tz_localize(timezone.utc)
        if result.index.tz is None
        else result.index.tz_convert(timezone.utc)
    )
    available = pd.to_datetime(result["available_at"], utc=True, errors="coerce")
    if available.isna().any() or (available < result.index).any():
        raise ValueError("mark quote availability must be aware and not precede observation")
    result["available_at"] = available
    for column in ("bid", "ask", "bid_size", "ask_size"):
        values = pd.to_numeric(result[column], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            raise ValueError(f"mark quote {column} must be finite")
        if (values <= 0).any():
            raise ValueError(f"mark quote {column} must be positive")
        result[column] = values
    if (result["bid"] > result["ask"]).any():
        raise ValueError("mark quote bid must not exceed ask")
    return result.sort_values("available_at", kind="stable")


def _fill_model(runtime: object, assumptions: ExecutionAssumptions) -> object:
    kwargs = {
        "prob_fill_on_limit": assumptions.limit_fill_probability,
        "prob_slippage": assumptions.slippage_probability,
        "random_seed": assumptions.seed,
    }
    if assumptions.one_tick_slippage:
        return runtime.OneTickSlippageFillModel(**kwargs)
    return runtime.DefaultFillModel(**kwargs)


def _native_costs(
    fills: pd.DataFrame,
    assumptions: ExecutionAssumptions,
    *,
    quote_currency: str,
) -> Mapping[str, object]:
    """Expose native fees without combining amounts from different currencies."""
    fees_by_currency: dict[str, Decimal] = {}
    if "commissions" in fills:
        for commissions in fills["commissions"]:
            for commission in commissions or ():
                amount, currency = str(commission).split(maxsplit=1)
                fees_by_currency[currency] = fees_by_currency.get(currency, Decimal("0")) + Decimal(
                    amount,
                )
    non_quote = sorted(currency for currency in fees_by_currency if currency != quote_currency)
    quote_fees = fees_by_currency.get(quote_currency, Decimal("0"))
    return {
        "fees": str(quote_fees) if not non_quote else None,
        "fees_by_currency": {
            currency: str(amount) for currency, amount in sorted(fees_by_currency.items())
        },
        "fee_quote_currency": quote_currency,
        "fee_conversion_status": "native_quote_only" if not non_quote else "unavailable",
        "fee_unavailable_reason": (
            None
            if not non_quote
            else "non-quote native commissions require explicit fill-time FX rates"
        ),
        "spread": None,
        "slippage": "one_tick" if assumptions.one_tick_slippage else "none",
        "funding": "0",
        "borrow": "0",
        "slippage_probability": assumptions.slippage_probability,
    }


def _assert_spot_ledger(
    portfolio: object,
    *,
    venue: object,
    instrument: object,
    quote_currency: object,
    initial_cash: Decimal,
    fills: pd.DataFrame,
    fill_transactions: Sequence[NativeFillTransaction],
    quote_precision: int,
    base_precision: int,
    kind: str,
) -> Mapping[str, object]:
    """Reject a run if Nautilus could not apply a cash-account fill event."""
    if kind != "spot":
        return {
            "status": "unavailable",
            "violations": [],
            "reason": "cash spot ledger reconciliation does not apply to margin instruments",
        }
    account = portfolio.account(venue=venue)
    if account is None:
        raise NativeLedgerError("native spot account is unavailable for reconciliation")
    expected_quote = _quantize_currency(initial_cash, quote_precision)
    expected_base = Decimal("0")
    if not fill_transactions and not fills.empty:
        raise NativeLedgerError(
            "native fills report is non-empty but raw fill transactions were not captured; "
            "aggregated rows cannot reconcile per-fill currency rounding or fees"
        )
    transactions = fill_transactions
    for transaction in transactions:
        quantity = _quantize_currency(transaction.quantity, base_precision)
        if quantity <= 0:
            continue
        notional = _quantize_currency(quantity * transaction.price, quote_precision)
        quote_fees, base_fees = _row_fees(
            () if transaction.commission is None else (transaction.commission,),
            quote_currency=quote_currency,
            base_currency=instrument.base_currency,
        )
        quote_fees = _quantize_currency(quote_fees, quote_precision)
        base_fees = _quantize_currency(base_fees, base_precision)
        if transaction.side == "BUY":
            expected_quote = _quantize_currency(
                expected_quote - notional - quote_fees,
                quote_precision,
            )
            expected_base = _quantize_currency(
                expected_base + quantity - base_fees,
                base_precision,
            )
        elif transaction.side == "SELL":
            expected_quote = _quantize_currency(
                expected_quote + notional - quote_fees,
                quote_precision,
            )
            expected_base = _quantize_currency(
                expected_base - quantity - base_fees,
                base_precision,
            )
        else:
            raise NativeLedgerError(f"unknown native spot fill side: {transaction.side}")
    quote_total = account.balance_total(quote_currency)
    base_total = account.balance_total(instrument.base_currency)
    actual_quote = Decimal("0") if quote_total is None else quote_total.as_decimal()
    actual_base = Decimal("0") if base_total is None else base_total.as_decimal()
    violations: list[str] = []
    if expected_quote < 0:
        violations.append("expected quote balance became negative")
    if expected_base < 0:
        violations.append("expected base balance became negative")
    if actual_quote != expected_quote:
        violations.append("quote account balance does not match native fills")
    if actual_base != expected_base:
        violations.append("base account balance does not match native fills")
    if violations:
        raise NativeLedgerError(
            "native spot ledger mismatch: "
            f"expected quote/base {expected_quote}/{expected_base}, "
            f"actual {actual_quote}/{actual_base}",
        )
    return {
        "status": "verified",
        "violations": [],
        "quote_currency": str(quote_currency),
        "base_currency": str(instrument.base_currency),
        "expected_quote_balance": str(expected_quote),
        "actual_quote_balance": str(actual_quote),
        "expected_base_balance": str(expected_base),
        "actual_base_balance": str(actual_base),
        "native_fill_transactions": len(transactions),
        "quote_precision": quote_precision,
        "base_precision": base_precision,
    }


def _quantize_currency(value: Decimal, precision: int) -> Decimal:
    if precision < 0:
        raise ValueError("currency precision must be non-negative")
    return value.quantize(Decimal("1").scaleb(-precision))


def _row_fees(
    commissions: object,
    *,
    quote_currency: object,
    base_currency: object,
) -> tuple[Decimal, Decimal]:
    quote_total = Decimal("0")
    base_total = Decimal("0")
    for commission in commissions or ():
        amount, currency = str(commission).split(maxsplit=1)
        if currency == str(quote_currency):
            quote_total += Decimal(amount)
        elif currency == str(base_currency):
            base_total += Decimal(amount)
        else:
            raise NativeLedgerError(
                "spot commission cannot be reconciled without fill-time FX: "
                f"{currency}, expected {quote_currency} or {base_currency}",
            )
    return quote_total, base_total


def _runtime() -> object:
    try:
        import nautilus_trader
        from nautilus_trader.backtest import BacktestEngine
        from nautilus_trader.config import BacktestEngineConfig
        from nautilus_trader.execution import (
            DefaultFillModel,
            OneTickSlippageFillModel,
            StaticLatencyModel,
        )
        from nautilus_trader.model import (
            AccountType,
            AggregationSource,
            Bar,
            BarType,
            CryptoPerpetual,
            Currency,
            CurrencyPair,
            InstrumentId,
            Money,
            OmsType,
            Price,
            Quantity,
            QuoteTick,
            Symbol,
            TraderId,
            Venue,
        )
    except ImportError as error:  # pragma: no cover - native host capability
        raise NautilusUnavailableError(
            "NautilusTrader 2.0.0rc4 is unavailable; QT will not substitute EventRunner",
        ) from error

    @dataclass(frozen=True)
    class Runtime:
        version: str
        BacktestEngine: object
        BacktestEngineConfig: object
        DefaultFillModel: object
        StaticLatencyModel: object
        AccountType: object
        AggregationSource: object
        Bar: object
        BarType: object
        Currency: object
        CurrencyPair: object
        CryptoPerpetual: object
        InstrumentId: object
        Money: object
        OmsType: object
        Price: object
        QuoteTick: object
        OneTickSlippageFillModel: object
        Quantity: object
        Symbol: object
        TraderId: object
        Venue: object

    return Runtime(
        version=nautilus_trader.__version__,
        BacktestEngine=BacktestEngine,
        BacktestEngineConfig=BacktestEngineConfig,
        DefaultFillModel=DefaultFillModel,
        StaticLatencyModel=StaticLatencyModel,
        AccountType=AccountType,
        AggregationSource=AggregationSource,
        Bar=Bar,
        BarType=BarType,
        Currency=Currency,
        CurrencyPair=CurrencyPair,
        CryptoPerpetual=CryptoPerpetual,
        InstrumentId=InstrumentId,
        Money=Money,
        OmsType=OmsType,
        Price=Price,
        QuoteTick=QuoteTick,
        OneTickSlippageFillModel=OneTickSlippageFillModel,
        Quantity=Quantity,
        Symbol=Symbol,
        TraderId=TraderId,
        Venue=Venue,
    )


def _build_instrument(
    runtime: object,
    frame: pd.DataFrame,
    definition: InstrumentDefinition,
    assumptions: ExecutionAssumptions,
) -> tuple[object, object]:
    first = int(frame.index[0].value)
    currency = runtime.Currency
    symbol = runtime.Symbol(definition.symbol)
    common = {
        "instrument_id": runtime.InstrumentId(symbol, runtime.Venue(definition.venue)),
        "raw_symbol": symbol,
        "base_currency": currency.from_str(definition.base_currency),
        "quote_currency": currency.from_str(definition.quote_currency),
        "price_precision": definition.price_precision,
        "size_precision": definition.size_precision,
        "price_increment": runtime.Price.from_str(str(definition.price_increment)),
        "size_increment": runtime.Quantity.from_str(str(definition.size_increment)),
        "ts_event": first,
        "ts_init": first,
        "maker_fee": assumptions.maker_fee,
        "taker_fee": assumptions.taker_fee,
        "margin_init": Decimal("1") if definition.kind == "perpetual" else None,
        "margin_maint": assumptions.maintenance_margin if definition.kind == "perpetual" else None,
    }
    instrument = (
        runtime.CryptoPerpetual(
            settlement_currency=currency.from_str(definition.quote_currency),
            is_inverse=False,
            **common,
        )
        if definition.kind == "perpetual"
        else runtime.CurrencyPair(**common)
    )
    bar_type = runtime.BarType.from_str(
        f"{instrument.id}-{_bar_spec(_interval_seconds(frame.index))}-LAST-EXTERNAL",
    )
    return instrument, bar_type


def _bars(
    runtime: object,
    frame: pd.DataFrame,
    instrument: object,
    bar_type: object,
    *,
    timestamp_at_close: bool,
) -> list[object]:
    interval_nanos = _interval_seconds(frame.index) * 1_000_000_000
    return [
        runtime.Bar(
            bar_type=bar_type,
            open=instrument.make_price(float(row.open)),
            high=instrument.make_price(float(row.high)),
            low=instrument.make_price(float(row.low)),
            close=instrument.make_price(float(row.close)),
            volume=instrument.make_qty(float(row.volume)),
            ts_event=int(timestamp.value),
            ts_init=(
                int(timestamp.value)
                if timestamp_at_close
                else int(timestamp.value) + interval_nanos
            ),
        )
        for timestamp, row in frame.iterrows()
    ]


def _quote_ticks(runtime: object, frame: pd.DataFrame, instrument: object) -> list[object]:
    """Translate already-attested quote rows without changing their timestamps."""
    return [
        runtime.QuoteTick(
            instrument_id=instrument.id,
            bid_price=instrument.make_price(float(row.bid)),
            ask_price=instrument.make_price(float(row.ask)),
            bid_size=instrument.make_qty(float(row.bid_size)),
            ask_size=instrument.make_qty(float(row.ask_size)),
            ts_event=int(timestamp.value),
            ts_init=int(row.available_at.value),
        )
        for timestamp, row in frame.iterrows()
    ]


def _interval_seconds(index: pd.DatetimeIndex) -> int:
    if len(index) < 2:
        return 60
    deltas = index.to_series().diff().dropna()
    if deltas.nunique() != 1:
        raise ValueError("OHLCV timestamps contain gaps or inconsistent intervals")
    seconds = int(deltas.iloc[0].total_seconds())
    if seconds <= 0:
        raise ValueError("OHLCV timestamps must have a positive interval")
    return seconds


def _bar_spec(seconds: int) -> str:
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}-DAY"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}-HOUR"
    if seconds % 60 == 0:
        return f"{seconds // 60}-MINUTE"
    return f"{seconds}-SECOND"


def _native_timestamp(timestamp: int | None) -> datetime:
    if timestamp is None:
        raise RuntimeError("native backtest result has no timestamp")
    return datetime.fromtimestamp(timestamp / 1_000_000_000, tz=timezone.utc)
