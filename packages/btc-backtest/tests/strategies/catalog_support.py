from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
from btc_backtest.data.models import (
    DataManifest,
    DataRequest,
    MarketBundle,
    MarketDataset,
)
from btc_backtest.engine.models import BacktestResult, BacktestSpec, InstrumentKind
from btc_backtest.engine.runner import EventRunner
from btc_backtest.strategies.registry import default_strategy_registry

UTC = timezone.utc
BAR_COUNT = 400


def catalog_bundle() -> MarketBundle:
    index = pd.date_range(
        "2024-01-01",
        periods=BAR_COUNT,
        freq="1h",
        tz="UTC",
    )
    trend = np.linspace(100.0, 150.0, 100)
    range_market = 150.0 + np.sin(np.linspace(0, 6 * np.pi, 100)) * 8
    crash = np.linspace(range_market[-1], 75.0, 50)
    rebound = np.linspace(75.0, 140.0, 100)
    final_range = 140.0 + np.sin(np.linspace(0, 4 * np.pi, 50)) * 5
    close = np.concatenate((trend, range_market, crash, rebound, final_range))
    open_values = np.r_[close[0], close[:-1]]
    spread = 1.5 + np.abs(np.sin(np.arange(BAR_COUNT) / 7))
    low = np.minimum(open_values, close) - spread
    high = np.maximum(open_values, close) + spread
    low[220] = close[219] * 0.82
    low[235] = close[234] * 0.88
    volume = 100 + 20 * np.sin(np.arange(BAR_COUNT) / 5)
    volume[200:250] *= 5
    spot = pd.DataFrame(
        {
            "open": open_values,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=index,
    )
    basis = 0.005 + np.sin(np.arange(BAR_COUNT) / 13) * 0.001
    perpetual_close = close * (1 + basis)
    perpetual_open = np.r_[perpetual_close[0], perpetual_close[:-1]]
    perpetual = pd.DataFrame(
        {
            "open": perpetual_open,
            "high": np.maximum(perpetual_open, perpetual_close) + spread,
            "low": np.minimum(perpetual_open, perpetual_close) - spread,
            "close": perpetual_close,
            "volume": volume * 1.4,
        },
        index=index,
    )
    funding_index = index[::8]
    funding_rates = np.where(
        funding_index < index[200],
        0.0002,
        np.where(funding_index < index[280], -0.0001, 0.00001),
    )
    funding = pd.DataFrame(
        {
            "rate": funding_rates,
            "funding_rate": funding_rates,
            "mark_price": perpetual.loc[funding_index, "close"].to_numpy(),
        },
        index=funding_index,
    )
    open_interest = pd.DataFrame(
        {
            "value": np.r_[
                np.linspace(1000, 1300, 200),
                np.linspace(1300, 700, 80),
                np.linspace(700, 1100, 120),
            ]
        },
        index=index,
    )
    long_short_ratio = pd.DataFrame(
        {"value": 1.2 + np.sin(np.arange(BAR_COUNT) / 31) * 0.4},
        index=index,
    )
    mvrv_values = np.where(
        np.arange(BAR_COUNT) < 200,
        1.5,
        np.where(np.arange(BAR_COUNT) < 280, -0.5, 0.8),
    )
    mvrv = pd.DataFrame(
        {
            "value": mvrv_values,
            "mvrv": mvrv_values,
            "valuation_zscore": mvrv_values,
        },
        index=index,
    )
    fear_values = np.where(
        np.arange(BAR_COUNT) < 200,
        55.0,
        np.where(np.arange(BAR_COUNT) < 280, 12.0, 45.0),
    )
    fear_greed = pd.DataFrame(
        {"value": fear_values, "fear_greed": fear_values},
        index=index,
    )
    macro = pd.DataFrame(
        {"vix": np.full(BAR_COUNT, 20.0), "dxy_z": np.zeros(BAR_COUNT)},
        index=index,
    )
    return MarketBundle(
        primary=_dataset("spot", spot),
        auxiliary={
            "perpetual": _dataset("perpetual", perpetual),
            "funding": _dataset("funding", funding),
            "open_interest": _dataset("open_interest", open_interest),
            "long_short_ratio": _dataset(
                "long_short_ratio",
                long_short_ratio,
            ),
            "mvrv": _dataset("mvrv", mvrv),
            "fear_greed": _dataset("fear_greed", fear_greed),
            "macro": _dataset("macro", macro),
        },
    )


def run_catalog(
    strategy_id: str,
    parameters: dict[str, object] | None = None,
) -> tuple[BacktestResult, MarketBundle]:
    bundle = catalog_bundle()
    registry = default_strategy_registry()
    strategy = registry.create(strategy_id, parameters or {})
    result = EventRunner().run(
        catalog_spec(strategy_id, parameters or {}),
        bundle,
        strategy,
    )
    return result, bundle


def catalog_spec(
    strategy_id: str,
    parameters: dict[str, object],
) -> BacktestSpec:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return BacktestSpec(
        strategy=strategy_id,
        strategy_params=parameters,
        data=DataRequest(
            provider="catalog-fixture",
            symbol="BTC/USD",
            timeframe="1h",
            start=start,
            end=start + timedelta(hours=BAR_COUNT),
        ),
        initial_cash=Decimal("10000"),
        fee_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        seed=17,
    )


def canonical_summary(
    result: BacktestResult,
    bundle: MarketBundle,
) -> dict[str, object]:
    first_action = result.orders[0].reason if result.orders else None
    last_action = result.orders[-1].reason if result.orders else None
    exposures: list[Decimal] = []
    spot_close = bundle.primary.frame["close"]
    perpetual_close = bundle.auxiliary["perpetual"].frame["close"]
    for snapshot in result.snapshots:
        timestamp = pd.Timestamp(snapshot.timestamp)
        spot = snapshot.position(InstrumentKind.SPOT)
        perpetual = snapshot.position(InstrumentKind.PERPETUAL)
        gross = (
            abs(spot.quantity) * Decimal(str(spot_close.loc[timestamp]))
            + abs(perpetual.quantity)
            * Decimal(str(perpetual_close.loc[timestamp]))
        )
        exposures.append(
            gross / snapshot.equity
            if snapshot.equity > 0
            else Decimal("0")
        )
    events = {
        "orders": [
            {
                "created_at": order.created_at.isoformat(),
                "instrument": order.instrument.value,
                "side": order.side.value,
                "type": order.order_type.value,
                "quantity": str(order.quantity),
                "limit": str(order.limit_price),
                "status": order.status.value,
                "reason": order.reason,
            }
            for order in result.orders
        ],
        "fills": [
            {
                "timestamp": fill.timestamp.isoformat(),
                "instrument": fill.instrument.value,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "reason": fill.reason,
            }
            for fill in result.fills
        ],
    }
    digest = hashlib.sha256(
        json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "final_equity": _fixed(result.snapshots[-1].equity),
        "order_count": len(result.orders),
        "fill_count": len(result.fills),
        "first_action": first_action,
        "last_action": last_action,
        "max_gross_exposure": _fixed(max(exposures, default=Decimal("0"))),
        "event_digest": digest,
    }


def _dataset(market: str, frame: pd.DataFrame) -> MarketDataset:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=BAR_COUNT)
    digest = hashlib.sha256(market.encode()).hexdigest()
    return MarketDataset(
        frame=frame,
        manifest=DataManifest(
            provider="catalog-fixture",
            market=market,
            symbol="BTC/USD",
            timeframe="1h",
            requested_start=start,
            requested_end=end,
            delivered_start=start,
            delivered_end=end,
            retrieved_at=end,
            real_data=True,
            raw_sha256=(digest,),
            normalized_sha256=digest,
        ),
    )


def _fixed(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")
