"""Deterministic Paper/Shadow event loop, independent of model inference.

An event checkpoint records generated signals AND the sized immutable orders
before any venue effect. Restart recovery uses those original orders rather
than re-sizing them against a later balance or price. The account lease fences
both checkpoint commits and OMS submission.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, localcontext
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from qt.contracts import (
    DeploymentManifest,
    ExecutableMarketSnapshot,
    FeeSchedule,
    InstrumentSpec,
    MarketEvent,
    OrderIntent,
    SignalIntent,
    StrategyArtifact,
    StrategyState,
    content_hash,
)
from qt.control.service import ControlService
from qt.execution.oms import OMS
from qt.marketdata import BookView
from qt.operations.risk import RiskBudgetExceededError
from qt.persistence import Conflict
from qt.persistence.repository import insert_once
from qt.persistence.schema import accounts, artifacts, ledger_entries, orders, runtime_health
from qt.strategies.native_bridge import NativeEventStrategy

_TERMINAL = {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}
_UNCERTAIN = {"SUBMITTING", "UNKNOWN", "CANCEL_PENDING"}
_BAD_DATA = {"BOOK_INVALID", "SEQUENCE_GAP", "STALE", "CLOCK_INVALID", "RECONSTRUCTED_RECEIPT"}


class TradingRuntime:
    """Single-writer strategy actor for one signed deployment and spot pair.

    ``strategy`` must implement ``on_event`` and durable ``snapshot_state`` /
    ``restore_state`` hooks. Supply real bounded depth to a PaperAdapter via
    ``process(..., book=...)`` or prepare the adapter's book explicitly.
    Funded execution is deliberately unavailable in this runtime.
    """

    def __init__(
        self, service: ControlService, oms: OMS, strategy: Any, deployment_id: str,
        instrument_spec: InstrumentSpec, fees: FeeSchedule, adapter: Any, *,
        clock: Callable[[], int] = time.time_ns, maximum_data_age_ns: int = 1_000_000_000,
        owner: str | None = None,
    ) -> None:
        if maximum_data_age_ns <= 0:
            raise ValueError("maximum_data_age_must_be_positive")
        if not all(callable(getattr(strategy, name, None))
                   for name in ("on_event", "snapshot_state", "restore_state")):
            raise ValueError("strategy_requires_durable_state_hooks")
        self.service, self.oms, self.strategy = service, oms, strategy
        self.deployment_id, self.instrument, self.fees, self.adapter = (
            deployment_id, instrument_spec, fees, adapter,
        )
        self.clock, self.maximum_data_age_ns = clock, maximum_data_age_ns
        self.owner = owner or f"trading-{uuid.uuid4().hex}"
        self.tenant = service.tenant
        self._lease_token: str | None = None
        self._head_id: str | None = None
        self._restored = False
        self._verified_deployment_hash: str | None = None
        self._halt_reason: str | None = None
        self._lock = asyncio.Lock()

    def _lease(self, connection: Any, manifest: DeploymentManifest) -> dict[str, Any]:
        row = connection.execute(select(accounts).where(
            accounts.c.tenant_id == self.tenant, accounts.c.account_id == manifest.account_id,
        ).with_for_update()).mappings().first()
        if (row is None or row["lease_owner"] != self.owner or row["lease_token"] != self._lease_token
                or row["lease_until_ns"] <= self.clock()):
            raise Conflict("runtime_execution_lease_lost")
        return dict(row)

    def _put_fenced(self, manifest: DeploymentManifest, kind: str, identity: str, payload: dict[str, Any]) -> None:
        digest = content_hash(payload)
        with self.service.repository.transaction() as connection:
            self._lease(connection, manifest)
            insert_once(connection, artifacts, {
                "tenant_id": self.tenant, "kind": kind, "entity_id": identity,
                "payload": payload, "sha256": digest, "created_ns": self.clock(),
            })
            stored = connection.execute(select(artifacts.c.sha256).where(
                artifacts.c.tenant_id == self.tenant, artifacts.c.kind == kind,
                artifacts.c.entity_id == identity,
            )).scalar_one()
            if stored != digest:
                raise Conflict("runtime_checkpoint_identity_conflict")

    def _head(self) -> dict[str, Any] | None:
        with self.service.repository.transaction() as connection:
            row = connection.execute(select(runtime_health).where(
                runtime_health.c.tenant_id == self.tenant,
                runtime_health.c.name == f"state:{self.deployment_id}",
            )).mappings().first()
        return dict(row["payload"]) if row is not None else None

    def _restore(self) -> dict[str, Any] | None:
        head = self._head()
        self._head_id = head["step_id"] if head is not None else None
        if head is not None:
            step = self.service.repository.get(self.tenant, "trading_step", head["step_id"])["payload"]
            if step["deployment_id"] != self.deployment_id:
                raise Conflict("runtime_checkpoint_deployment_mismatch")
            self.strategy.restore_state(StrategyState.model_validate(step["state"]))
        self._restored = True
        return head

    def _checkpoint(self, manifest: DeploymentManifest, identity: str, payload: dict[str, Any]) -> None:
        with self.service.repository.transaction() as connection:
            self._lease(connection, manifest)
            head = connection.execute(select(runtime_health).where(
                runtime_health.c.tenant_id == self.tenant,
                runtime_health.c.name == f"state:{self.deployment_id}",
            ).with_for_update()).mappings().first()
            if (head["payload"]["step_id"] if head else None) != self._head_id:
                raise Conflict("runtime_checkpoint_head_changed")
            insert_once(connection, artifacts, {
                "tenant_id": self.tenant, "kind": "trading_step", "entity_id": identity,
                "payload": payload, "sha256": content_hash(payload), "created_ns": self.clock(),
            })
            found = connection.execute(select(artifacts.c.sha256).where(
                artifacts.c.tenant_id == self.tenant, artifacts.c.kind == "trading_step",
                artifacts.c.entity_id == identity,
            )).scalar_one()
            if found != content_hash(payload):
                raise Conflict("runtime_event_checkpoint_conflict")
            value = {"step_id": identity, "received_ts_ns": payload["event"]["received_ts_ns"],
                     "owner": self.owner, "ready": True, "healthy": True}
            insert_once(connection, runtime_health, {
                "tenant_id": self.tenant, "name": f"state:{self.deployment_id}",
                "observed_ns": self.clock(), "payload": value,
            })
            connection.execute(update(runtime_health).where(
                runtime_health.c.tenant_id == self.tenant,
                runtime_health.c.name == f"state:{self.deployment_id}",
            ).values(observed_ns=self.clock(), payload=value))
            self.service.repository.event(connection, self.tenant, self.deployment_id,
                                          "trading_event_checkpointed", {"step_id": identity})
        self._head_id = identity

    def _position(self, manifest: DeploymentManifest) -> tuple[Decimal, Decimal, bool]:
        with self.service.repository.transaction() as connection, localcontext() as context:
            context.prec = 80
            self._lease(connection, manifest)
            held = sum(connection.execute(select(ledger_entries.c.amount).where(
                ledger_entries.c.account_id == manifest.account_id,
                ledger_entries.c.asset == self.instrument.base_asset,
                ledger_entries.c.bucket == "wallet",
            )).scalars(), Decimal("0"))
            pending = connection.execute(select(orders).where(
                orders.c.account_id == manifest.account_id, orders.c.status.not_in(_TERMINAL),
            )).mappings().all()
            if any(row["status"] in _UNCERTAIN for row in pending):
                raise ValueError("account_requires_reconciliation")
            projected, pending_sell = held, Decimal("0")
            for row in pending:
                if row["instrument"] != self.instrument.instrument:
                    raise ValueError("runtime_multiinstrument_account_is_not_supported")
                remaining = row["quantity"] - row["filled_quantity"]
                projected += remaining if row["side"] == "buy" else -remaining
                if row["side"] == "sell":
                    pending_sell += row["reserved_base"]
            return held - pending_sell, projected, bool(pending)

    def _intents(
        self, manifest: DeploymentManifest, signals: list[SignalIntent],
        event: MarketEvent, market: ExecutableMarketSnapshot,
    ) -> list[OrderIntent]:
        available_base, projected, has_pending = self._position(manifest)
        planned: list[OrderIntent] = []
        with localcontext() as context:
            context.prec = 80
            for signal in signals:
                if (signal.deployment_id != self.deployment_id or signal.instrument != self.instrument.instrument
                        or signal.produced_at_ns > self.clock() or signal.valid_until_ns <= self.clock()
                        or signal.produced_at_ns < event.received_ts_ns):
                    raise ValueError("strategy_signal_identity_or_time_invalid")
                if signal.target_position < 0:
                    raise ValueError("spot_shorting_is_not_supported")
                delta = signal.target_position - projected
                if delta == 0:
                    continue
                side: Literal["buy", "sell"] = "buy" if delta > 0 else "sell"
                quantity = abs(delta)
                if side == "sell":
                    # Never sell unfilled buy quantity or duplicate reserved
                    # sell inventory when the target changes during execution.
                    quantity = min(quantity, available_base)
                    if self.fees.fee_asset == self.instrument.base_asset:
                        quantity /= 1 + max(Decimal("0"), self.fees.maker_rate, self.fees.taker_rate)
                if quantity <= 0:
                    if has_pending:
                        raise ValueError("opposing_pending_orders_require_cancellation")
                    continue
                quantity = (quantity / self.instrument.quantity_increment).to_integral_value(
                    rounding=ROUND_DOWN,
                ) * self.instrument.quantity_increment
                raw_price = market.ask if side == "buy" else market.bid
                price = (raw_price / self.instrument.price_increment).to_integral_value(
                    rounding=ROUND_CEILING if side == "buy" else ROUND_DOWN,
                ) * self.instrument.price_increment
                if quantity < self.instrument.minimum_quantity or quantity * price < self.instrument.minimum_notional:
                    continue
                identity = content_hash([self.tenant, self.deployment_id, signal.signal_id, 0])
                planned.append(OrderIntent(
                    intent_id=identity, account_id=manifest.account_id,
                    deployment_id=self.deployment_id, signal_id=signal.signal_id, venue=manifest.venue,
                    instrument=self.instrument.instrument, side=side, quantity=quantity, limit_price=price,
                    time_in_force="IOC", strategy_hash=manifest.strategy_hash,
                    policy_hash=manifest.risk_policy_hash, fee_schedule_hash=self.fees.content_hash(),
                    instrument_spec_hash=self.instrument.content_hash(),
                    risk_reservation_id=content_hash([identity, "risk_reservation"]),
                    created_at_ns=signal.produced_at_ns,
                    valid_until_ns=min(signal.valid_until_ns, manifest.valid_until_ns),
                    reduce_only=side == "sell",
                ))
                projected += quantity if side == "buy" else -quantity
                if side == "sell":
                    available_base -= quantity
        return planned

    def _halt(self, account_id: str, reason: str) -> dict[str, Any]:
        self._halt_reason = reason
        persisted = True
        try:
            self.oms.set_mode(self.tenant, account_id, "HALTED", reason)
        except Exception:
            persisted = False  # a database outage must still stop local sending
        return {"status": "halted", "reason": reason, "halt_persisted": persisted,
                "intent_ids": [], "execution_authorized": False}

    def _verify_strategy(self, manifest: DeploymentManifest) -> None:
        if self._verified_deployment_hash == manifest.content_hash():
            return
        if type(self.strategy) is not NativeEventStrategy:
            raise ValueError("runtime_requires_registered_shared_native_strategy")
        artifact = StrategyArtifact.model_validate(self.service.repository.get(
            self.tenant, "strategy", manifest.strategy_hash,
        )["payload"])
        self.strategy.verify_artifact(artifact)
        report = self.service.repository.get(
            self.tenant, "validation", manifest.validation_report_hash,
        )["payload"]
        spec = self.service.repository.get(
            self.tenant, "experiment_spec", report["experiment_hash"],
        )["payload"]
        if (spec.get("engine", {}).get("name") != "nautilus"
                or spec.get("strategy_version") != manifest.strategy_hash
                or spec.get("instrument_spec_hashes") != [self.instrument.content_hash()]
                or self.strategy.instrument.content_hash() != self.instrument.content_hash()
                or self.strategy.fees.content_hash() != self.fees.content_hash()
                or self.strategy.deployment_id != self.deployment_id):
            raise ValueError("runtime_validated_strategy_or_instrument_binding_mismatch")
        self._verified_deployment_hash = manifest.content_hash()

    async def _execute(self, manifest: DeploymentManifest, identity: str, payload: dict[str, Any],
                       market: ExecutableMarketSnapshot) -> dict[str, Any]:
        try:
            complete = self.service.repository.get(self.tenant, "trading_step_complete", identity)["payload"]
        except KeyError:
            complete = None
        if complete is not None:
            return {**complete, "status": "duplicate"}
        outcomes: list[dict[str, str]] = []
        for raw in payload["intents"]:
            intent = OrderIntent.model_validate(raw)
            try:
                existing = self.oms.get_order(self.tenant, intent.intent_id)
            except KeyError:
                existing = None
            if existing is None and self.clock() >= intent.valid_until_ns:
                outcomes.append({"intent_id": intent.intent_id, "status": "EXPIRED_UNSENT"})
                continue
            # The immutable intent is replayed exactly. Reserve never silently
            # overwrites it with a resized or repriced recovery request.
            try:
                self.oms.reserve(self.tenant, intent, instrument=self.instrument, fees=self.fees,
                                 deployment=manifest, market=market)
            except RiskBudgetExceededError as exc:
                # Keep a latched REDUCE_ONLY stop intact. Completing this
                # rejected step permits a subsequent protective sell signal.
                result: dict[str, Any] = {"status": "blocked", "step_id": identity, "reason": str(exc),
                          "intent_ids": [item["intent_id"] for item in outcomes], "orders": outcomes,
                          "mode": manifest.mode, "execution_authorized": False}
                self._put_fenced(manifest, "trading_step_complete", identity, result)
                return result
            result = await self.oms.submit(self.tenant, intent.intent_id, self.adapter,
                                           lease_token=self._lease_token or "")
            outcomes.append({"intent_id": intent.intent_id, "status": result["status"]})
            if result["status"] in _UNCERTAIN:
                return self._halt(manifest.account_id, "runtime_order_outcome_unknown")
            if manifest.mode == "paper" and result["status"] not in {"REJECTED", "EXPIRED"}:
                await self.oms.reconcile(self.tenant, manifest.account_id, self.adapter,
                                         venue=manifest.venue)
                outcomes[-1]["status"] = self.oms.get_order(self.tenant, intent.intent_id)["status"]
        result = {"status": "processed" if outcomes else "no_signal", "step_id": identity,
                  "intent_ids": [item["intent_id"] for item in outcomes], "orders": outcomes,
                  "mode": manifest.mode, "execution_authorized": manifest.mode == "paper"}
        self._put_fenced(manifest, "trading_step_complete", identity, result)
        return result

    async def process(
        self, event: MarketEvent, market: ExecutableMarketSnapshot, *, book: BookView | None = None,
    ) -> dict[str, Any]:
        """Validate, checkpoint, reserve, submit and reconcile one receipt-ordered event."""
        async with self._lock:
            if self._halt_reason is not None:
                return {"status": "halted", "reason": self._halt_reason, "intent_ids": [],
                        "execution_authorized": False}
            try:
                manifest = self.service.verify_deployment(self.deployment_id)
                self._verify_strategy(manifest)
            except (ValueError, KeyError) as exc:
                return {"status": "blocked", "reason": str(exc), "intent_ids": [],
                        "execution_authorized": False}
            if manifest.mode not in {"paper", "shadow"}:
                return self._halt(manifest.account_id, "runtime_funded_execution_not_enabled")
            if manifest.mode == "paper" and getattr(self.adapter, "execution_mode", None) != "paper":
                return self._halt(manifest.account_id, "runtime_requires_paper_adapter")
            now = self.clock()
            if (event.venue != manifest.venue or event.instrument != self.instrument.instrument
                    or market.venue != manifest.venue or market.instrument != self.instrument.instrument
                    or market.instrument_spec_hash != self.instrument.content_hash()
                    or market.fee_schedule_hash != self.fees.content_hash()
                    or not market.book_valid or not 0 <= now - market.asof_ns <= self.maximum_data_age_ns
                    or not 0 <= now - event.received_ts_ns <= self.maximum_data_age_ns
                    or event.exchange_ts_ns > event.received_ts_ns
                    or _BAD_DATA.intersection(event.quality_flags)):
                return self._halt(manifest.account_id, "runtime_market_data_untrusted_or_stale")
            try:
                token = self.oms.claim_account(self.tenant, manifest.account_id, owner=self.owner,
                                               ttl_seconds=300)
                if self._lease_token is not None and token != self._lease_token:
                    raise Conflict("runtime_execution_lease_changed_requires_restart")
                self._lease_token = token
                if manifest.mode == "paper" and book is not None:
                    self.adapter.update_book(book, market, self.instrument, self.fees)
                self.service.repository.health(self.tenant,
                    f"market:{market.venue}:{market.instrument}", market.model_dump(mode="json"))
                self.service.repository.health(self.tenant, f"trading:{self.deployment_id}",
                    {"healthy": True, "owner": self.owner, "received_ts_ns": event.received_ts_ns,
                     "mode": manifest.mode})
                if event.event_type not in self.strategy.accepted_event_types:
                    return {"status": "market_updated", "intent_ids": [], "mode": manifest.mode}
                if manifest.mode == "paper":
                    cursor = None
                    for _ in range(1000):
                        reconciled = await self.oms.reconcile(
                            self.tenant, manifest.account_id, self.adapter,
                            venue=manifest.venue, cursor=cursor,
                        )
                        if reconciled["complete"]:
                            break
                        cursor = reconciled["next_cursor"]["cursor"]
                    else:
                        raise ValueError("paper_reconciliation_page_limit_exceeded")
                head = self._restore() if not self._restored else self._head()
                if head is not None:
                    prior = self.service.repository.get(self.tenant, "trading_step", head["step_id"])["payload"]
                    recovered = await self._execute(manifest, head["step_id"], prior, market)
                    if recovered["status"] == "halted":
                        return recovered
                identity = content_hash([self.tenant, self.deployment_id, event.content_hash()])
                try:
                    previous = self.service.repository.get(self.tenant, "trading_step", identity)["payload"]
                except KeyError:
                    previous = None
                if previous is not None:
                    return await self._execute(manifest, identity, previous, market)
                if head is not None and event.received_ts_ns < head["received_ts_ns"]:
                    return self._halt(manifest.account_id, "runtime_event_receipt_order_regression")
                wallet = self.oms.balances(self.tenant, manifest.account_id)["wallet"]
                self.strategy.set_portfolio(
                    cash=wallet.get(self.instrument.quote_asset, Decimal("0")),
                    base_quantity=wallet.get(self.instrument.base_asset, Decimal("0")),
                    asof_ns=now,
                )
                signals = self.strategy.on_event(event, StrategyState())
                if not isinstance(signals, list) or any(not isinstance(item, SignalIntent) for item in signals):
                    raise ValueError("strategy_must_return_typed_signal_intents")
                intents = self._intents(manifest, signals, event, market) if signals else []
                payload = {"deployment_id": self.deployment_id, "event": event.model_dump(mode="json"),
                           "market": market.model_dump(mode="json"),
                           "signals": [item.model_dump(mode="json") for item in signals],
                           "intents": [item.model_dump(mode="json") for item in intents],
                           "book": book.model_dump(mode="json") if book is not None else None,
                           "state": self.strategy.snapshot_state().model_dump(mode="json")}
                self._checkpoint(manifest, identity, payload)
                return await self._execute(manifest, identity, payload, market)
            except Conflict as exc:
                # Losing the writer lease must not halt a new legitimate owner.
                if "lease" in str(exc) or "checkpoint_head" in str(exc):
                    return {"status": "blocked", "reason": str(exc), "intent_ids": []}
                return self._halt(manifest.account_id, str(exc))
            except (ValueError, KeyError) as exc:
                self._restored = False
                return self._halt(manifest.account_id, str(exc))
            except SQLAlchemyError as exc:
                return self._halt(manifest.account_id, "runtime_database_unavailable:" + type(exc).__name__)

    async def on_event(
        self, event: MarketEvent, market: ExecutableMarketSnapshot, *, book: BookView | None = None,
    ) -> dict[str, Any]:
        return await self.process(event, market, book=book)

    @classmethod
    def from_deployment(
        cls, service: ControlService, deployment_id: str,
    ) -> tuple[TradingRuntime, InstrumentSpec, FeeSchedule]:
        return runtime_for_deployment(service, deployment_id)


def runtime_for_deployment(
    service: ControlService, deployment_id: str,
) -> tuple[TradingRuntime, InstrumentSpec, FeeSchedule]:
    """Build only the exact validated native strategy with a paper-only adapter."""
    from qt.execution.simulated import PaperAdapter
    from qt.strategies.native_bridge import strategy_for_deployment

    manifest = service.verify_deployment(deployment_id)
    if manifest.mode not in {"paper", "shadow"}:
        raise ValueError("runtime_factory_funded_execution_not_enabled")
    strategy, instrument, fees = strategy_for_deployment(service, manifest)
    oms = OMS(service.repository)
    if manifest.mode == "paper":
        oms.create_account(service.tenant, manifest.account_id, capital=manifest.risk_budget.capital,
                           quote_asset=instrument.quote_asset)
    else:
        oms.balances(service.tenant, manifest.account_id)  # require a preexisting trusted shadow account
    adapter = PaperAdapter(service.repository, tenant_id=service.tenant)
    return (TradingRuntime(service, oms, strategy, deployment_id, instrument, fees, adapter),
            instrument, fees)
