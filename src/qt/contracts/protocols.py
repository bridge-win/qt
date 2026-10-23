"""Engine boundaries; model inference is deliberately absent from execution."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from qt.contracts.models import (
    CallBudget,
    CancelResult,
    ExecutableMarketSnapshot,
    ExperimentResult,
    ExperimentSpecV2,
    MarketEvent,
    ModelRequest,
    ModelResult,
    OrderIdentity,
    OrderIntent,
    OrderSnapshot,
    PortfolioSnapshot,
    ReconcileBatch,
    ReconcileCursor,
    RiskDecision,
    SignalIntent,
    StrategyState,
    SubmitResult,
)


class DatasetReader(Protocol):
    def events(self, dataset_id: str) -> Iterator[MarketEvent]: ...


class Strategy(Protocol):
    def on_event(self, event: MarketEvent, state: StrategyState) -> list[SignalIntent]: ...


class ExperimentEngine(Protocol):
    def run(self, spec: ExperimentSpecV2, data: DatasetReader) -> ExperimentResult: ...


class RiskEngine(Protocol):
    def reserve(
        self, signal: SignalIntent, portfolio: PortfolioSnapshot,
        market: ExecutableMarketSnapshot,
    ) -> RiskDecision: ...


class ExecutionAdapter(Protocol):
    async def submit(self, intent: OrderIntent) -> SubmitResult: ...
    async def query(self, identity: OrderIdentity) -> OrderSnapshot: ...
    async def cancel(self, identity: OrderIdentity) -> CancelResult: ...
    async def reconcile(self, cursor: ReconcileCursor) -> ReconcileBatch: ...


class ModelProvider(Protocol):
    async def invoke(self, request: ModelRequest, budget: CallBudget) -> ModelResult: ...
