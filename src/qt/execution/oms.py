"""Durable, deterministic spot OMS. External I/O never runs inside a DB transaction.

An uncertain external effect remains reserved until authoritative reconciliation.
The order row is the durable outbox: RESERVED can be sent once, SUBMITTING and
UNKNOWN can only be queried. No funded adapter is installed or enabled here.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any

from sqlalchemy import insert, select, update

from qt.contracts import (
    CancelResult,
    DeploymentManifest,
    ExecutableMarketSnapshot,
    FeeSchedule,
    InstrumentSpec,
    OrderIdentity,
    OrderIntent,
    OrderSnapshot,
    ReconcileCursor,
    SubmitResult,
    content_hash,
    decimal_string,
)
from qt.contracts.protocols import ExecutionAdapter
from qt.operations.risk import (
    RiskBudgetExceededError,
    RiskGuard,
    StaleMarketObservationError,
    ensure_active_deployment,
)
from qt.persistence import Conflict, Repository
from qt.persistence.repository import insert_once
from qt.persistence.schema import (
    accounts,
    fills,
    ledger_entries,
    ledger_transactions,
    orders,
    risk_state,
    runtime_health,
)

_ZERO = Decimal(0)
_QUANTUM = Decimal("0.000000000000000001")
_TERMINAL = {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}
_UNCERTAIN = {"SUBMITTING", "UNKNOWN", "CANCEL_PENDING"}
_MODES = {"ACTIVE", "REDUCE_ONLY", "HALTED", "EMERGENCY_EXIT"}


def _amount(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value <= 0):
        raise ValueError("amount_requires_finite_nonnegative_decimal")
    with localcontext() as ctx:
        ctx.prec = 80
        if value != value.quantize(_QUANTUM) or value >= Decimal("1e20"):
            raise ValueError("amount_exceeds_exact_numeric_precision")
    return value


def _ceil(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        return value.quantize(_QUANTUM, rounding=ROUND_CEILING)


def _text(value: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid_identity")
    return value


def _now(now_ns: int | None) -> int:
    value = time.time_ns() if now_ns is None else now_ns
    if type(value) is not int or value < 0:
        raise ValueError("invalid_clock")
    return value


def _day(now: int) -> str:
    return datetime.fromtimestamp(now // 1_000_000_000, tz=timezone.utc).date().isoformat()


class OMS:
    def __init__(
        self, repo: Repository, *,
        funded_gate: Callable[[DeploymentManifest, ExecutionAdapter], bool] | None = None,
        daily_entry_fraction: Decimal = Decimal("1"),
    ) -> None:
        self.repo = repo
        self.risk_guard = RiskGuard(repo)
        self.funded_gate = funded_gate
        self.daily_entry_fraction = _amount(daily_entry_fraction, positive=True)
        if self.daily_entry_fraction > 1:
            raise ValueError("daily_entry_fraction_must_not_exceed_one")

    @staticmethod
    def _account(connection, tenant: str, account_id: str) -> dict:
        row = connection.execute(select(accounts).where(
            accounts.c.tenant_id == tenant, accounts.c.account_id == account_id,
        ).with_for_update()).mappings().first()
        if row is None:
            raise KeyError("account_not_found")
        return dict(row)

    @staticmethod
    def _order(connection, tenant: str, identity: str) -> dict:
        row = connection.execute(select(orders).where(
            orders.c.tenant_id == tenant, orders.c.intent_id == identity,
        ).with_for_update()).mappings().first()
        if row is None:
            raise KeyError("order_not_found")
        if content_hash(row["payload"]) != row["sha256"]:
            raise Conflict("order_content_integrity_failure")
        return dict(row)

    @staticmethod
    def _wallet(connection, account_id: str) -> dict[str, Decimal]:
        # Do not ask SQLite to SUM decimal text through binary floating point.
        result: dict[str, Decimal] = {}
        with localcontext() as ctx:
            ctx.prec = 80
            for asset, amount in connection.execute(select(
                ledger_entries.c.asset, ledger_entries.c.amount,
            ).where(ledger_entries.c.account_id == account_id, ledger_entries.c.bucket == "wallet")):
                result[asset] = result.get(asset, _ZERO) + amount
        return result

    def _journal(self, connection, account: dict, kind: str, source: str,
                 entries: list[tuple[str, str, Decimal]], now: int) -> None:
        totals: dict[str, Decimal] = {}
        with localcontext() as ctx:
            ctx.prec = 80
            for asset, _bucket, amount in entries:
                totals[asset] = totals.get(asset, _ZERO) + amount
        if any(totals.values()):
            raise ValueError("unbalanced_asset_journal")
        transaction = content_hash([account["account_id"], kind, source])
        connection.execute(insert(ledger_transactions).values(
            transaction_id=transaction, account_id=account["account_id"],
            kind=kind, source_id=source, created_ns=now,
        ))
        for index, (asset, bucket, amount) in enumerate(entries):
            connection.execute(insert(ledger_entries).values(
                entry_id=content_hash([transaction, index]), transaction_id=transaction,
                account_id=account["account_id"], asset=asset, bucket=bucket, amount=amount,
            ))

    def create_account(self, tenant: str, account_id: str, *, capital: Decimal,
                       quote_asset: str = "USDT") -> dict:
        _text(tenant, maximum=80)
        _text(account_id, maximum=100)
        _text(quote_asset, maximum=32)
        _amount(capital, positive=True)
        now = time.time_ns()
        with self.repo.transaction() as connection, localcontext() as ctx:
            ctx.prec = 80
            # Stable account identity serializes concurrent creation on PG.
            insert_once(connection, accounts, {
                "account_id": account_id, "tenant_id": tenant, "quote_asset": quote_asset,
                "capital": capital, "mode": "ACTIVE", "reserved_quote": _ZERO,
                "daily_new_risk": _ZERO, "day_utc": _day(now),
                "lease_until_ns": 0, "reconciled_ns": now,
            })
            account = self._account(connection, tenant, account_id)
            if account["capital"] != capital or account["quote_asset"] != quote_asset:
                raise Conflict("account_identity_conflict")
            existing = connection.execute(select(ledger_transactions.c.transaction_id).where(
                ledger_transactions.c.account_id == account_id,
                ledger_transactions.c.kind == "funding",
            )).first()
            if existing is None:
                self._journal(connection, account, "funding", account_id, [
                    (quote_asset, "wallet", capital), (quote_asset, "equity", -capital),
                ], now)
                self.repo.event(connection, tenant, account_id, "account_created", {"capital": str(capital)})
        return account

    def get_order(self, tenant: str, intent_id: str) -> dict:
        with self.repo.transaction() as connection:
            row = self._order(connection, tenant, intent_id)
            row["fills"] = [dict(item) for item in connection.execute(select(fills).where(
                fills.c.intent_id == intent_id,
            ).order_by(fills.c.created_ns, fills.c.fill_id)).mappings()]
        return row

    def balances(self, tenant: str, account_id: str) -> dict:
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, account_id)
            wallet = self._wallet(connection, account_id)
            reserved: dict[str, Decimal] = {account["quote_asset"]: account["reserved_quote"]}
            with localcontext() as ctx:
                ctx.prec = 80
                for row in connection.execute(select(orders).where(
                    orders.c.account_id == account_id, orders.c.reserved_base != _ZERO,
                )).mappings():
                    base = row["payload"]["instrument"]["base_asset"]
                    reserved[base] = reserved.get(base, _ZERO) + row["reserved_base"]
                assets = wallet.keys() | reserved.keys()
                return {
                    "account_id": account_id, "mode": account["mode"],
                    "wallet": wallet, "reserved": reserved,
                    "available": {asset: wallet.get(asset, _ZERO) - reserved.get(asset, _ZERO) for asset in assets},
                }

    @staticmethod
    def _verify(intent, instrument, fees, deployment, market, now) -> None:
        if intent.venue != instrument.venue or intent.instrument != instrument.instrument:
            raise ValueError("instrument_identity_mismatch")
        if intent.instrument_spec_hash != instrument.content_hash():
            raise ValueError("instrument_version_mismatch")
        if intent.fee_schedule_hash != fees.content_hash() or intent.fee_schedule_hash not in deployment.fee_schedule_hashes:
            raise ValueError("fee_version_mismatch")
        if (intent.account_id != deployment.account_id or intent.venue != deployment.venue
                or intent.deployment_id != deployment.deployment_id
                or intent.strategy_hash != deployment.strategy_hash
                or intent.policy_hash != deployment.risk_policy_hash):
            raise ValueError("deployment_identity_mismatch")
        if (market.venue != intent.venue or market.instrument != intent.instrument
                or market.fee_schedule_hash != intent.fee_schedule_hash
                or market.instrument_spec_hash != intent.instrument_spec_hash):
            raise ValueError("market_version_mismatch")
        if not market.book_valid or not 0 <= now - market.asof_ns <= 1_000_000_000:
            raise ValueError("market_is_not_fresh_and_trusted")
        if not intent.created_at_ns <= now < intent.valid_until_ns:
            raise ValueError("order_not_current")
        for spec in (instrument, fees):
            if spec.effective_from_ns > now or (spec.effective_to_ns is not None and now >= spec.effective_to_ns):
                raise ValueError("specification_not_effective")
        if not deployment.valid_from_ns <= now < deployment.valid_until_ns:
            raise ValueError("deployment_not_current")
        if fees.venue != intent.venue or fees.fee_basis != "notional" or fees.funding_rules:
            raise ValueError("unsupported_fee_rules")
        if fees.fee_rules:
            raise ValueError("fee_rules_require_dedicated_verified_model")
        if instrument.instrument_type != "spot" or instrument.contract_multiplier != 1:
            raise ValueError("only_unlevered_spot_is_supported")
        if instrument.trading_sessions != ("24x7",):
            raise ValueError("unsupported_trading_session")
        if fees.fee_asset not in (instrument.base_asset, instrument.quote_asset):
            raise ValueError("unknown_fee_asset_funding")
        if max(fees.maker_rate, fees.taker_rate) >= 1:
            raise ValueError("invalid_fee_rate")
        with localcontext() as ctx:
            ctx.prec = 80
            if intent.limit_price % instrument.price_increment or intent.quantity % instrument.quantity_increment:
                raise ValueError("order_precision_mismatch")
            if intent.quantity < instrument.minimum_quantity or intent.quantity * intent.limit_price < instrument.minimum_notional:
                raise ValueError("order_below_venue_minimum")

    def reserve(self, tenant: str, intent: OrderIntent, *, instrument: InstrumentSpec,
                fees: FeeSchedule, deployment: DeploymentManifest,
                market: ExecutableMarketSnapshot, now_ns: int | None = None) -> dict:
        report = self.risk_guard.check(tenant, deployment, instrument, market, now_ns=now_ns)
        if report.get("observation_discarded"):
            raise StaleMarketObservationError("superseded_market_observation")
        if report["mode"] == "HALTED" or (report["mode"] != "ACTIVE" and intent.side == "buy"):
            raise RiskBudgetExceededError("risk_budget_or_health_stop:" + ",".join(report["reasons"]))
        try:
            return self._reserve(tenant, intent, instrument=instrument, fees=fees,
                                 deployment=deployment, market=market, now_ns=now_ns)
        except RiskBudgetExceededError as exc:
            self.risk_guard.stop(tenant, intent.account_id, [str(exc)], mode="REDUCE_ONLY")
            raise

    def _reserve(self, tenant: str, intent: OrderIntent, *, instrument: InstrumentSpec,
                 fees: FeeSchedule, deployment: DeploymentManifest,
                 market: ExecutableMarketSnapshot, now_ns: int | None = None) -> dict:
        _text(intent.intent_id)
        _text(intent.account_id, maximum=100)
        _text(intent.risk_reservation_id)
        envelope = {"intent": intent.model_dump(mode="json"),
                    "instrument": instrument.model_dump(mode="json"),
                    "fees": fees.model_dump(mode="json"),
                    "deployment": deployment.model_dump(mode="json")}
        digest = content_hash(envelope)
        with self.repo.transaction() as connection, localcontext() as ctx:
            ctx.prec = 80
            account = self._account(connection, tenant, intent.account_id)
            now = _now(now_ns)
            ensure_active_deployment(connection, tenant, intent.account_id, intent.deployment_id)
            existing = connection.execute(select(orders).where(orders.c.intent_id == intent.intent_id)).mappings().first()
            if existing is not None:
                if existing["tenant_id"] != tenant or existing["sha256"] != digest:
                    raise Conflict("order_identity_conflict")
                return dict(existing)
            self._verify(intent, instrument, fees, deployment, market, now)
            if intent.side == "buy":
                self.risk_guard.ensure_new_risk_locked(connection, account, deployment, instrument, market, now)
            if deployment.mode == "live_canary" and self.funded_gate is None:
                raise ValueError("funded_execution_not_verified")
            if (account["quote_asset"] != instrument.quote_asset
                    or deployment.risk_budget.currency != account["quote_asset"]
                    or deployment.risk_budget.capital != account["capital"]
                    or deployment.risk_budget.maximum_leverage > 1):
                raise ValueError("account_risk_currency_or_capital_mismatch")
            other = connection.execute(select(orders.c.intent_id).where(
                orders.c.account_id == intent.account_id,
                orders.c.deployment_id == intent.deployment_id,
                orders.c.signal_id == intent.signal_id, orders.c.leg_no == intent.leg_no,
            )).first()
            if other:
                raise Conflict("signal_leg_already_reserved")
            if connection.execute(select(orders.c.intent_id).where(
                orders.c.risk_reservation_id == intent.risk_reservation_id,
            )).first():
                raise Conflict("risk_reservation_identity_conflict")
            uncertain = connection.execute(select(orders.c.intent_id).where(
                orders.c.account_id == intent.account_id, orders.c.status.in_(_UNCERTAIN),
            )).first()
            if uncertain:
                raise ValueError("account_requires_reconciliation")
            if account["mode"] == "HALTED" or (
                account["mode"] in {"REDUCE_ONLY", "EMERGENCY_EXIT"} and intent.side != "sell"
            ):
                raise ValueError("risk_mode_blocks_new_order")
            if intent.reduce_only and intent.side != "sell":
                raise ValueError("spot_reduce_only_requires_owned_inventory_sale")
            wallet = self._wallet(connection, intent.account_id)
            if any(amount < 0 for amount in wallet.values()):
                raise ValueError("negative_wallet_requires_reconciliation")
            if any(asset not in {instrument.base_asset, instrument.quote_asset} and amount > 0
                   for asset, amount in wallet.items()):
                raise ValueError("multiasset_valuation_is_not_verified")
            rate = max(_ZERO, fees.maker_rate, fees.taker_rate)
            gross = intent.quantity * intent.limit_price
            if gross > account["capital"] * min(Decimal("0.02"), deployment.risk_budget.maximum_order_fraction):
                raise ValueError("maximum_order_notional_exceeded")
            reserved_quote = _ceil(gross * (1 + rate if fees.fee_asset == instrument.quote_asset else 1)) if intent.side == "buy" else _ZERO
            reserved_base = _ceil(intent.quantity * (1 + rate if fees.fee_asset == instrument.base_asset else 1)) if intent.side == "sell" else _ZERO
            pending = connection.execute(select(orders).where(orders.c.account_id == intent.account_id)).mappings().all()
            if intent.side == "buy":
                if wallet.get(account["quote_asset"], _ZERO) - account["reserved_quote"] < reserved_quote:
                    raise ValueError("insufficient_unreserved_cash")
                pending_buys = sum((
                    (row["quantity"] - row["filled_quantity"]) * Decimal(row["payload"]["intent"]["limit_price"])
                    for row in pending if row["side"] == "buy" and row["status"] not in _TERMINAL
                ), _ZERO)
                exposure = wallet.get(instrument.base_asset, _ZERO) * max(market.ask, intent.limit_price) + pending_buys + gross
                if exposure > account["capital"] * min(Decimal("0.30"), deployment.risk_budget.maximum_position_fraction):
                    raise ValueError("maximum_position_notional_exceeded")
                daily = account["daily_new_risk"] if account["day_utc"] == _day(now) else _ZERO
                if daily + gross > account["capital"] * self.daily_entry_fraction:
                    raise ValueError("daily_entry_budget_exhausted")
                connection.execute(update(accounts).where(accounts.c.account_id == intent.account_id).values(
                    daily_new_risk=daily + gross, day_utc=_day(now),
                ))
            else:
                base_reserved = sum((row["reserved_base"] for row in pending
                                     if row["payload"]["instrument"]["base_asset"] == instrument.base_asset), _ZERO)
                if wallet.get(instrument.base_asset, _ZERO) - base_reserved < reserved_base:
                    raise ValueError("insufficient_unreserved_owned_inventory")
            connection.execute(update(accounts).where(accounts.c.account_id == intent.account_id).values(
                reserved_quote=account["reserved_quote"] + reserved_quote,
            ))
            row = {
                "intent_id": intent.intent_id, "account_id": intent.account_id, "tenant_id": tenant,
                "deployment_id": intent.deployment_id, "signal_id": intent.signal_id, "leg_no": intent.leg_no,
                "venue": intent.venue, "instrument": intent.instrument, "side": intent.side,
                "quantity": intent.quantity, "filled_quantity": _ZERO,
                "reserved_quote": reserved_quote, "reserved_base": reserved_base,
                "risk_reservation_id": intent.risk_reservation_id,
                "status": "RESERVED", "settlement_status": "NOT_REQUIRED",
                "payload": envelope, "sha256": digest, "venue_order_id": None,
                "created_ns": now, "updated_ns": now, "last_reconciled_ns": 0,
            }
            insert_once(connection, orders, row)
            inserted = connection.execute(select(orders).where(orders.c.intent_id == intent.intent_id)).mappings().first()
            if inserted is None or inserted["tenant_id"] != tenant or inserted["sha256"] != digest:
                raise Conflict("order_or_reservation_identity_conflict")
            self.repo.event(connection, tenant, intent.intent_id, "order_reserved", {"sha256": digest})
        return row

    def claim_account(self, tenant: str, account_id: str, *, owner: str, ttl_seconds: int = 30) -> str:
        _text(owner)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("invalid_lease_ttl")
        now = time.time_ns()
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, account_id)
            if account["lease_until_ns"] > now and account["lease_owner"] != owner:
                raise Conflict("account_execution_lease_held")
            token = account["lease_token"] if account["lease_until_ns"] > now else uuid.uuid4().hex
            connection.execute(update(accounts).where(accounts.c.account_id == account_id).values(
                lease_owner=owner, lease_token=token, lease_until_ns=now + ttl_seconds * 1_000_000_000,
            ))
            self.repo.event(connection, tenant, account_id, "execution_lease_claimed", {"owner": owner})
        return token

    @staticmethod
    def _lease(account: dict, token: str, now: int) -> None:
        if account["lease_token"] != token or account["lease_until_ns"] <= now:
            raise Conflict("account_execution_lease_lost")

    def _release(self, connection, account: dict, order: dict, status: str, now: int) -> None:
        with localcontext() as ctx:
            ctx.prec = 80
            released = account["reserved_quote"] - order["reserved_quote"]
        connection.execute(update(accounts).where(accounts.c.account_id == account["account_id"]).values(
            reserved_quote=released,
        ))
        connection.execute(update(orders).where(orders.c.intent_id == order["intent_id"]).values(
            status=status, reserved_quote=_ZERO, reserved_base=_ZERO, updated_ns=now,
        ))

    def _uncertain(self, tenant: str, identity: str, reason: str) -> dict:
        first = self.get_order(tenant, identity)
        with self.repo.transaction() as connection:
            # Consistent account->order lock order across all writers.
            account = self._account(connection, tenant, first["account_id"])
            found = self._order(connection, tenant, identity)
            connection.execute(update(orders).where(orders.c.intent_id == identity).values(
                status="UNKNOWN" if found["status"] not in _TERMINAL else found["status"], updated_ns=time.time_ns(),
            ))
            connection.execute(update(accounts).where(accounts.c.account_id == account["account_id"]).values(mode="HALTED"))
            self.repo.event(connection, tenant, identity, "execution_uncertain", {"reason": reason})
        return self.get_order(tenant, identity)

    async def submit(self, tenant: str, intent_id: str, adapter: ExecutionAdapter, *,
                     lease_token: str, now_ns: int | None = None) -> dict:
        row = self.get_order(tenant, intent_id)
        market = None
        if row["status"] == "RESERVED" and _now(now_ns) < row["payload"]["intent"]["valid_until_ns"]:
            with self.repo.transaction() as connection:
                health = connection.execute(select(runtime_health).where(
                    runtime_health.c.tenant_id == tenant,
                    runtime_health.c.name == f"market:{row['venue']}:{row['instrument']}",
                )).mappings().first()
                state = connection.execute(select(risk_state).where(risk_state.c.account_id == row["account_id"])).mappings().first()
            raw = health["payload"] if health else state["market"] if state else None
            if raw is None:
                self.risk_guard.stop(tenant, row["account_id"], ["submission_has_no_trusted_market"], mode="HALTED")
                raise RiskBudgetExceededError("submission_has_no_trusted_market")
            try:
                market = ExecutableMarketSnapshot.model_validate(raw)
            except ValueError:
                self.risk_guard.stop(tenant, row["account_id"], ["submission_market_invalid"], mode="HALTED")
                raise RiskBudgetExceededError("submission_market_invalid") from None
            deployment = DeploymentManifest.model_validate(row["payload"]["deployment"])
            instrument = InstrumentSpec.model_validate(row["payload"]["instrument"])
            guard = self.risk_guard.check(tenant, deployment, instrument, market, now_ns=now_ns)
            if guard.get("observation_discarded"):
                raise StaleMarketObservationError("superseded_market_observation")
            if guard["mode"] == "HALTED" or (guard["mode"] != "ACTIVE" and row["side"] == "buy"):
                raise RiskBudgetExceededError("submission_risk_or_health_stop")
        try:
            return await self._submit(tenant, intent_id, adapter, lease_token=lease_token,
                                      now_ns=now_ns, checked_market=market)
        except RiskBudgetExceededError as exc:
            self.risk_guard.stop(tenant, row["account_id"], [str(exc)], mode="REDUCE_ONLY")
            raise

    async def _submit(self, tenant: str, intent_id: str, adapter: ExecutionAdapter, *,
                      lease_token: str, now_ns: int | None = None,
                      checked_market: ExecutableMarketSnapshot | None = None) -> dict:
        first = self.get_order(tenant, intent_id)
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, first["account_id"])
            now = _now(now_ns)
            row = self._order(connection, tenant, intent_id)
            ensure_active_deployment(connection, tenant, row["account_id"], row["deployment_id"])
            self._lease(account, lease_token, now)
            if row["status"] != "RESERVED":
                return {**row, "requires_reconciliation": row["status"] in _UNCERTAIN}
            intent = OrderIntent.model_validate(row["payload"]["intent"])
            deployment = DeploymentManifest.model_validate(row["payload"]["deployment"])
            if now >= intent.valid_until_ns:
                self._release(connection, account, row, "EXPIRED", now)
                self.repo.event(connection, tenant, intent_id, "order_expired_unsent", {})
                return {**row, "status": "EXPIRED", "reserved_quote": _ZERO, "reserved_base": _ZERO}
            if account["mode"] == "HALTED" or (account["mode"] != "ACTIVE" and intent.side != "sell"):
                raise ValueError("risk_mode_blocks_submission")
            if intent.side == "buy" and checked_market is not None:
                self.risk_guard.ensure_new_risk_locked(
                    connection, account, deployment,
                    InstrumentSpec.model_validate(row["payload"]["instrument"]), checked_market, now,
                )
            if connection.execute(select(orders.c.intent_id).where(
                orders.c.account_id == row["account_id"], orders.c.intent_id != intent_id,
                orders.c.status.in_(_UNCERTAIN),
            )).first():
                raise ValueError("account_requires_reconciliation")
            if deployment.mode == "shadow":
                return {**row, "sent": False, "shadow": True}
            adapter_mode = getattr(adapter, "execution_mode", None)
            if deployment.mode == "paper" and adapter_mode != "paper":
                raise ValueError("paper_deployment_requires_explicit_paper_adapter")
            if deployment.mode == "live_canary" and (
                adapter_mode != "live" or self.funded_gate is None or not self.funded_gate(deployment, adapter)
            ):
                raise ValueError("funded_execution_not_verified")
            changed = connection.execute(update(orders).where(
                orders.c.intent_id == intent_id, orders.c.status == "RESERVED",
            ).values(status="SUBMITTING", updated_ns=now))
            if changed.rowcount != 1:
                raise Conflict("submission_compare_and_swap_failed")
            self.repo.event(connection, tenant, intent_id, "order_submitting", {})
        try:
            # Recheck fencing immediately before the side effect. A stalled old
            # sender leaves SUBMITTING, which blocks all new account submissions.
            with self.repo.transaction() as connection:
                self._lease(self._account(connection, tenant, row["account_id"]), lease_token, time.time_ns())
            remaining_seconds = max(0.001, min(5, (intent.valid_until_ns - time.time_ns()) / 1_000_000_000))
            result = await asyncio.wait_for(adapter.submit(intent), timeout=remaining_seconds)
            if not isinstance(result, SubmitResult):
                raise ValueError("adapter_submit_result_must_be_typed")
            self._identity_matches(row, result.identity)
        except asyncio.CancelledError:
            self._uncertain(tenant, intent_id, "submission_cancelled")
            raise
        except Exception as exc:
            return self._uncertain(tenant, intent_id, type(exc).__name__)
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, row["account_id"])
            current = self._order(connection, tenant, intent_id)
            self._lease(account, lease_token, time.time_ns())
            if result.identity.venue_order_id is not None:
                connection.execute(update(orders).where(orders.c.intent_id == intent_id).values(
                    venue_order_id=result.identity.venue_order_id,
                ))
            # A fill/account event may have advanced the order while awaiting ACK.
            if current["status"] == "SUBMITTING":
                if result.status == "rejected":
                    self._release(connection, account, current, "REJECTED", time.time_ns())
                else:
                    connection.execute(update(orders).where(orders.c.intent_id == intent_id).values(
                        status="ACKNOWLEDGED" if result.status == "accepted" else "UNKNOWN",
                        venue_order_id=result.identity.venue_order_id, updated_ns=time.time_ns(),
                    ))
                if result.status == "unknown":
                    connection.execute(update(accounts).where(accounts.c.account_id == row["account_id"]).values(mode="HALTED"))
            self.repo.event(connection, tenant, intent_id, "submission_result", result.model_dump(mode="json"))
        return self.get_order(tenant, intent_id)

    @staticmethod
    def _identity_matches(row: dict, identity: OrderIdentity) -> None:
        if (identity.intent_id != row["intent_id"] or identity.account_id != row["account_id"]
                or identity.venue != row["venue"]
                or (row["venue_order_id"] is not None and identity.venue_order_id != row["venue_order_id"])):
            raise ValueError("adapter_order_identity_mismatch")

    async def cancel(self, tenant: str, intent_id: str, adapter: ExecutionAdapter, *, lease_token: str) -> dict:
        first = self.get_order(tenant, intent_id)
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, first["account_id"])
            row = self._order(connection, tenant, intent_id)
            self._lease(account, lease_token, time.time_ns())
            if row["status"] in _TERMINAL:
                return row
            if row["status"] == "RESERVED":
                self._release(connection, account, row, "CANCELED", time.time_ns())
                self.repo.event(connection, tenant, intent_id, "unsent_order_canceled", {})
                return {**row, "status": "CANCELED", "reserved_quote": _ZERO, "reserved_base": _ZERO}
            if row["status"] in {"SUBMITTING", "UNKNOWN", "CANCEL_PENDING"}:
                return {**row, "requires_reconciliation": True}
            connection.execute(update(orders).where(orders.c.intent_id == intent_id).values(
                status="CANCEL_PENDING", updated_ns=time.time_ns(),
            ))
            self.repo.event(connection, tenant, intent_id, "cancel_pending", {})
        try:
            result = await asyncio.wait_for(adapter.cancel(OrderIdentity(
                venue=row["venue"], account_id=row["account_id"], intent_id=intent_id,
                venue_order_id=row["venue_order_id"],
            )), timeout=5)
            if not isinstance(result, CancelResult):
                raise ValueError("adapter_cancel_result_must_be_typed")
            self._identity_matches(row, result.identity)
        except asyncio.CancelledError:
            self._uncertain(tenant, intent_id, "cancel_cancelled")
            raise
        except Exception as exc:
            return self._uncertain(tenant, intent_id, type(exc).__name__)
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, row["account_id"])
            current = self._order(connection, tenant, intent_id)
            self._lease(account, lease_token, time.time_ns())
            if current["status"] == "CANCEL_PENDING" and result.status == "confirmed":
                self._release(connection, account, current, "CANCELED", time.time_ns())
            elif current["status"] == "CANCEL_PENDING" and result.status == "rejected":
                connection.execute(update(orders).where(orders.c.intent_id == intent_id).values(
                    status="PARTIALLY_FILLED" if current["filled_quantity"] else "ACKNOWLEDGED",
                ))
            self.repo.event(connection, tenant, intent_id, "cancel_result", result.model_dump(mode="json"))
        return self.get_order(tenant, intent_id)

    def apply_fill(self, tenant: str, intent_id: str, *, venue_trade_id: str,
                   quantity: Decimal, price: Decimal, fee: Decimal, fee_asset: str,
                   base_asset: str, quote_asset: str,
                   settlement_status: str = "NOT_REQUIRED") -> dict:
        _text(venue_trade_id, maximum=200)
        for asset in (fee_asset, base_asset, quote_asset):
            _text(asset, maximum=32)
        _amount(quantity, positive=True)
        _amount(price, positive=True)
        _amount(fee)
        if settlement_status not in {"NOT_REQUIRED", "PENDING", "CONFIRMED", "FAILED", "REORGED"}:
            raise ValueError("invalid_settlement_status")
        first = self.get_order(tenant, intent_id)
        now = time.time_ns()
        with self.repo.transaction() as connection, localcontext() as ctx:
            ctx.prec = 80
            account = self._account(connection, tenant, first["account_id"])
            row = self._order(connection, tenant, intent_id)
            spec, fee_spec = row["payload"]["instrument"], row["payload"]["fees"]
            if base_asset != spec["base_asset"] or quote_asset != spec["quote_asset"]:
                raise ValueError("fill_asset_identity_mismatch")
            gross = (quantity * price).quantize(
                _QUANTUM, rounding=ROUND_CEILING if row["side"] == "buy" else ROUND_FLOOR,
            )
            _amount(gross, positive=True)
            fill_payload = {"intent_id": intent_id, "quantity": decimal_string(quantity),
                            "price": decimal_string(price), "fee": decimal_string(fee),
                            "fee_asset": fee_asset, "base_asset": base_asset, "quote_asset": quote_asset,
                            "cash_notional": decimal_string(gross),
                            "cash_rounding": "buyer_ceiling_seller_floor_18dp"}
            digest = content_hash(fill_payload)
            existing = connection.execute(select(fills).where(
                fills.c.venue == row["venue"], fills.c.account_id == row["account_id"],
                fills.c.venue_trade_id == venue_trade_id,
            )).mappings().first()
            if existing is not None:
                if existing["sha256"] != digest:
                    raise Conflict("venue_trade_identity_conflict")
                return dict(existing)
            if row["status"] == "RESERVED":
                raise ValueError("fill_for_never_submitted_order")
            filled = row["filled_quantity"] + quantity
            if filled > row["quantity"]:
                raise ValueError("fill_exceeds_order_quantity")
            fill_id = content_hash([row["venue"], row["account_id"], venue_trade_id])
            fill = {"fill_id": fill_id, "intent_id": intent_id, "venue": row["venue"],
                    "account_id": row["account_id"], "venue_trade_id": venue_trade_id,
                    "quantity": quantity, "price": price, "fee": fee, "fee_asset": fee_asset,
                    "sha256": digest, "created_ns": now}
            connection.execute(insert(fills).values(**fill))
            sign = Decimal(1) if row["side"] == "buy" else Decimal(-1)
            self._journal(connection, account, "trade", fill_id, [
                (base_asset, "wallet", quantity * sign), (base_asset, "counterparty", -quantity * sign),
                (quote_asset, "wallet", -gross * sign), (quote_asset, "counterparty", gross * sign),
                (fee_asset, "wallet", -fee), (fee_asset, "fee_expense", fee),
            ], now)
            remaining = (row["quantity"] - filled) / row["quantity"]
            active = row["status"] not in _TERMINAL
            original_intent = OrderIntent.model_validate(row["payload"]["intent"])
            fee_rate = max(_ZERO, Decimal(fee_spec["maker_rate"]), Decimal(fee_spec["taker_rate"]))
            new_quote = _ceil((row["quantity"] - filled) * original_intent.limit_price * (
                1 + fee_rate if fee_spec["fee_asset"] == quote_asset else 1
            )) if active and row["side"] == "buy" else _ZERO
            new_base = _ceil(row["quantity"] * remaining * (
                1 + fee_rate if fee_spec["fee_asset"] == base_asset else 1
            )) if active and row["side"] == "sell" else _ZERO
            status = "FILLED" if filled == row["quantity"] else (
                row["status"] if not active or row["status"] == "CANCEL_PENDING" else "PARTIALLY_FILLED"
            )
            connection.execute(update(orders).where(orders.c.intent_id == intent_id).values(
                filled_quantity=filled, reserved_quote=new_quote, reserved_base=new_base,
                status=status, settlement_status=settlement_status, updated_ns=now,
            ))
            account_reserved = account["reserved_quote"] - row["reserved_quote"] + new_quote
            connection.execute(update(accounts).where(accounts.c.account_id == row["account_id"]).values(
                reserved_quote=account_reserved,
            ))
            wallet = self._wallet(connection, row["account_id"])
            pending_base = sum((item["reserved_base"] for item in connection.execute(select(orders).where(
                orders.c.account_id == row["account_id"],
            )).mappings() if item["payload"]["instrument"]["base_asset"] == base_asset), _ZERO)
            breach = (
                any(value < 0 for value in wallet.values())
                or wallet.get(quote_asset, _ZERO) < account_reserved
                or wallet.get(base_asset, _ZERO) < pending_base
                or fee_asset != fee_spec["fee_asset"]
                or fee > (gross * fee_rate if fee_asset == quote_asset else quantity * fee_rate)
                or (price > original_intent.limit_price if row["side"] == "buy" else price < original_intent.limit_price)
            )
            if breach or settlement_status in {"FAILED", "REORGED"}:
                connection.execute(update(accounts).where(accounts.c.account_id == row["account_id"]).values(mode="HALTED"))
            self.repo.event(connection, tenant, intent_id, "fill_applied", {**fill_payload, "fill_id": fill_id})
        return fill

    def _snapshot(self, tenant: str, snapshot: OrderSnapshot) -> dict:
        first = self.get_order(tenant, snapshot.identity.intent_id)
        self._identity_matches(first, snapshot.identity)
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, first["account_id"])
            row = self._order(connection, tenant, first["intent_id"])
            if snapshot.quantity != row["quantity"] or snapshot.filled_quantity != row["filled_quantity"]:
                raise ValueError("snapshot_fill_ledger_difference_requires_fill_reconciliation")
            if snapshot.asof_ns < row["created_ns"] or snapshot.asof_ns > time.time_ns():
                raise ValueError("snapshot_time_is_not_causal")
            if snapshot.status in {"CREATED", "RESERVED", "SUBMITTING", "UNKNOWN"}:
                raise ValueError("snapshot_does_not_resolve_external_execution")
            if row["status"] in _TERMINAL and snapshot.status != row["status"]:
                raise ValueError("snapshot_cannot_regress_terminal_order")
            if snapshot.status == "FILLED" and snapshot.filled_quantity != row["quantity"]:
                raise ValueError("filled_snapshot_is_incomplete")
            if snapshot.status in _TERMINAL:
                self._release(connection, account, row, snapshot.status, time.time_ns())
            connection.execute(update(orders).where(orders.c.intent_id == row["intent_id"]).values(
                status=snapshot.status, venue_order_id=snapshot.identity.venue_order_id,
                settlement_status=snapshot.settlement_status, last_reconciled_ns=snapshot.asof_ns,
                updated_ns=time.time_ns(),
            ))
            if snapshot.settlement_status in {"FAILED", "REORGED"}:
                connection.execute(update(accounts).where(accounts.c.account_id == row["account_id"]).values(mode="HALTED"))
            self.repo.event(connection, tenant, row["intent_id"], "order_reconciled", snapshot.model_dump(mode="json"))
        return self.get_order(tenant, first["intent_id"])

    async def query(self, tenant: str, intent_id: str, adapter: ExecutionAdapter) -> dict:
        row = self.get_order(tenant, intent_id)
        try:
            result = await adapter.query(OrderIdentity(
                venue=row["venue"], account_id=row["account_id"], intent_id=intent_id,
                venue_order_id=row["venue_order_id"],
            ))
            return self._snapshot(tenant, result)
        except Exception as exc:
            return self._uncertain(tenant, intent_id, "query:" + type(exc).__name__)

    async def reconcile(self, tenant: str, account_id: str, adapter: ExecutionAdapter, *,
                        venue: str, cursor: str | None = None) -> dict:
        # Reconciliation ingestion is intentionally read-only at the venue.
        with self.repo.transaction() as connection:
            self._account(connection, tenant, account_id)
        batch = await adapter.reconcile(ReconcileCursor(venue=venue, account_id=account_id, cursor=cursor))
        if batch.next_cursor.account_id != account_id or batch.next_cursor.venue != venue:
            raise ValueError("reconciliation_identity_mismatch")
        for item in batch.fills:
            fields: dict[str, Any] = dict(item)
            for name in ("quantity", "price", "fee"):
                if not isinstance(fields[name], str):
                    raise ValueError("reconciled_money_requires_decimal_strings")
                fields[name] = Decimal(fields[name])
            intent_id = fields.pop("intent_id")
            order = self.get_order(tenant, intent_id)
            if order["account_id"] != account_id or order["venue"] != venue:
                raise ValueError("reconciled_fill_account_mismatch")
            self.apply_fill(tenant, intent_id, **fields)
        for snapshot in batch.orders:
            if snapshot.identity.account_id != account_id or snapshot.identity.venue != venue:
                raise ValueError("reconciled_order_account_mismatch")
            self._snapshot(tenant, snapshot)
        if batch.complete:
            self.mark_reconciled(tenant, account_id)
        return {"complete": batch.complete, "next_cursor": batch.next_cursor.model_dump(mode="json")}

    def mark_reconciled(self, tenant: str, account_id: str, *, now_ns: int | None = None) -> dict:
        now = _now(now_ns)
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, account_id)
            if connection.execute(select(orders.c.intent_id).where(
                orders.c.account_id == account_id, orders.c.status.in_(_UNCERTAIN),
            )).first():
                raise ValueError("unresolved_execution_states_remain")
            connection.execute(update(accounts).where(accounts.c.account_id == account_id).values(reconciled_ns=now))
            self.repo.event(connection, tenant, account_id, "account_reconciled", {})
        return {**account, "reconciled_ns": now}

    def set_mode(self, tenant: str, account_id: str, mode: str, reason: str) -> dict:
        if mode not in _MODES or not isinstance(reason, str) or not reason.strip():
            raise ValueError("invalid_risk_mode_or_reason")
        with self.repo.transaction() as connection:
            account = self._account(connection, tenant, account_id)
            if mode == "ACTIVE" and any(value < 0 for value in self._wallet(connection, account_id).values()):
                raise ValueError("negative_wallet_prevents_reactivation")
            if mode == "ACTIVE" and connection.execute(select(orders.c.intent_id).where(
                orders.c.account_id == account_id, orders.c.status.in_(_UNCERTAIN),
            )).first():
                raise ValueError("uncertain_orders_prevent_reactivation")
            connection.execute(update(accounts).where(accounts.c.account_id == account_id).values(mode=mode))
            self.repo.event(connection, tenant, account_id, "risk_mode_changed", {"mode": mode, "reason": reason})
        return {**account, "mode": mode}
