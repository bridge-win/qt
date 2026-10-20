"""Offline, fixed-function experiment executor. No shell or user code evaluation."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class BacktestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    task: str = "backtest"
    prices: list[float] = Field(default_factory=list, max_length=10000)
    candles: list[list[float]] = Field(default_factory=list, max_length=2000)
    initial_cash: float = Field(default=1000, gt=0, le=1000000)
    fee_bps: float = Field(default=10, ge=0, le=1000)
    slippage_bps: float = Field(default=10, ge=0, le=1000)
    window: int = Field(default=20, ge=2, le=500)
    strategy: str = "moving_average"


def experiment(request: dict) -> dict:
    data = BacktestInput.model_validate(request)
    if data.task != "backtest":
        raise ValueError("unsupported sandbox task")
    input_hash = hashlib.sha256(json.dumps(data.model_dump(), sort_keys=True).encode()).hexdigest()
    if data.strategy == "btc_capitulation":
        from qt.btc_agent.backtest import run_backtest
        from qt.btc_agent.signals import Candle

        candles = [Candle(int(row[0]), *row[1:]) for row in data.candles if len(row) == 6]
        if len(candles) != len(data.candles):
            raise ValueError("invalid OHLCV shape")
        report = run_backtest(candles)
        return {
            "status": "completed",
            "input_hash": input_hash,
            "strategy": data.strategy,
            "report": report,
            "execution": "offline-fixed-function",
            "live_equivalence": False,
        }
    if data.strategy != "moving_average":
        raise ValueError("unsupported strategy")
    prices = data.prices
    if len(prices) < data.window + 2 or any(p <= 0 or not math.isfinite(p) for p in prices):
        raise ValueError("positive finite prices and sufficient warmup are required")
    cash, quantity, peak, drawdown, fees = data.initial_cash, 0.0, data.initial_cash, 0.0, 0.0
    curve, fills = [], []
    fee, slip = data.fee_bps / 10000, data.slippage_bps / 10000
    for index, price in enumerate(prices):
        # The previous close's decision executes on the next supplied price.
        if index > data.window:
            previous = prices[index - 1]
            average = sum(prices[index - data.window : index]) / data.window
            if previous > average and quantity == 0:
                fill = price * (1 + slip)
                notional = cash / (1 + fee)
                paid_fee = notional * fee
                quantity, cash = notional / fill, 0.0
                fees += paid_fee
                fills.append({"index": index, "side": "BUY", "price": fill, "fee": paid_fee})
            elif previous < average and quantity > 0:
                fill = price * (1 - slip)
                paid_fee = quantity * fill * fee
                cash, quantity = quantity * fill - paid_fee, 0.0
                fees += paid_fee
                fills.append({"index": index, "side": "SELL", "price": fill, "fee": paid_fee})
        equity = cash + quantity * price
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        curve.append({"index": index, "equity": equity})
    baseline = data.initial_cash / (1 + fee) / (prices[0] * (1 + slip)) * prices[-1]
    return {
        "status": "completed",
        "input_hash": input_hash,
        "strategy": data.strategy,
        "metrics": {
            "initial_cash": data.initial_cash,
            "final_equity": equity,
            "return_fraction": equity / data.initial_cash - 1,
            "max_drawdown_fraction": drawdown,
            "fees": fees,
            "baseline_final_equity": baseline,
            "fills_count": len(fills),
        },
        "fills": fills,
        "equity_curve": curve,
        "execution": "offline-fixed-function",
        "limitations": [
            "Supplied price series, not a full orderbook simulation",
            "No proof of forecast calibration or live profitability",
        ],
    }


def create_sandbox_app() -> FastAPI:
    app = FastAPI(title="QT offline experiment executor", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "execution": "fixed-function", "arbitrary_code": False}

    @app.post("/run")
    def run(body: BacktestInput, authorization: str = Header(default="")) -> dict:
        token = os.environ.get("QT_AGENT_HUB_EXECUTOR_TOKEN", "")
        if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "executor authentication required")
        try:
            return experiment(body.model_dump())
        except (ValueError, TypeError, IndexError):
            raise HTTPException(422, "invalid experiment input") from None

    return app
