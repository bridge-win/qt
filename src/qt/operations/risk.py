"""Ledger-based risk marks, fixed-capital loss limits and latched stops.

No external cash flows after initial funding are supported. A new policy does
not silently reset its high-water mark or daily loss history. All valuation is
conservative bid marking of actual spot inventory, never requested quantities.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, localcontext

from sqlalchemy import and_, or_, select, update

from qt.contracts import (
    DeploymentManifest,
    ExecutableMarketSnapshot,
    InstrumentSpec,
    content_hash,
    decimal_string,
)
from qt.persistence import Conflict, Repository
from qt.persistence.repository import insert_once
from qt.persistence.schema import (
    accounts,
    deployment_heads,
    ledger_entries,
    orders,
    portfolio_snapshots,
    risk_state,
)


class RiskBudgetExceededError(ValueError):
    pass


class StaleMarketObservationError(ValueError):
    """An out-of-order observer must retry using a current snapshot."""


def ensure_active_deployment(connection, tenant: str, account_id: str, deployment_id: str) -> None:
    """Called while holding the account lock; historical heads cannot change risk."""
    head = connection.execute(select(deployment_heads).where(
        deployment_heads.c.tenant_id == tenant, deployment_heads.c.account_id == account_id,
    ).with_for_update()).mappings().first()
    # Primitive/offline OMS tests may operate without a control-plane registry.
    # Production ControlService requires a signed, active head before dispatch.
    if head is not None and (head["status"] != "ACTIVE" or head["deployment_id"] != deployment_id):
        raise Conflict("deployment_is_not_the_active_account_head")


class RiskGuard:
    def __init__(self, repo: Repository):
        self.repo = repo

    @staticmethod
    def _balances(connection, account_id: str) -> dict[str, Decimal]:
        wallet: dict[str, Decimal] = {}
        with localcontext() as ctx:
            ctx.prec = 80
            for asset, amount in connection.execute(select(
                ledger_entries.c.asset, ledger_entries.c.amount,
            ).where(ledger_entries.c.account_id == account_id, ledger_entries.c.bucket == "wallet")):
                wallet[asset] = wallet.get(asset, Decimal(0)) + amount
        return wallet

    @staticmethod
    def _reasons(account, state, wallet, deployment, instrument, market, now):
        reasons = []
        if (market.venue != deployment.venue or market.instrument != instrument.instrument
                or market.instrument_spec_hash != instrument.content_hash()
                or market.fee_schedule_hash not in deployment.fee_schedule_hashes):
            reasons.append("market_or_instrument_version_mismatch")
        if not deployment.valid_from_ns <= now < deployment.valid_until_ns:
            reasons.append("deployment_expired_or_not_effective")
        if (not market.book_valid or not 0 <= now - market.asof_ns <= 1_000_000_000
                or instrument.effective_from_ns > now
                or (instrument.effective_to_ns is not None and now >= instrument.effective_to_ns)):
            reasons.append("untrusted_or_stale_market")
        if (account["quote_asset"] != instrument.quote_asset
                or account["capital"] != deployment.risk_budget.capital
                or deployment.risk_budget.currency != account["quote_asset"]):
            reasons.append("valuation_currency_or_capital_mismatch")
        if any(asset not in {instrument.base_asset, instrument.quote_asset} and amount != 0
               for asset, amount in wallet.items()) or any(value < 0 for value in wallet.values()):
            reasons.append("untrusted_multiasset_or_negative_inventory")
        if state and state["policy_hash"] != deployment.risk_policy_hash:
            reasons.append("risk_policy_changed_without_review")
        if state and now < state["last_mark_ns"]:
            reasons.append("risk_mark_time_regression")
        return reasons

    def evaluate_locked(self, connection, account, deployment, instrument, market, now) -> dict:
        state = connection.execute(select(risk_state).where(
            risk_state.c.account_id == account["account_id"], risk_state.c.tenant_id == account["tenant_id"],
        ).with_for_update()).mappings().first()
        wallet = self._balances(connection, account["account_id"])
        with localcontext() as ctx:
            ctx.prec = 80
            reasons = self._reasons(account, state, wallet, deployment, instrument, market, now)
            if not reasons and state and market.asof_ns < state["market"]["asof_ns"]:
                # Independent observers can wait behind a newer account mark.
                # Discard that stale observation, without replacing the newer
                # valuation or permanently stopping a healthy account.
                return {"safe_new_risk": False, "mode": account["mode"],
                        "reasons": ["superseded_market_observation"], "equity": None,
                        "wallet": wallet, "state": dict(state), "observation_discarded": True}
            if reasons:
                return {"safe_new_risk": False, "mode": "HALTED", "reasons": reasons,
                        "equity": None, "wallet": wallet, "state": dict(state) if state else None}
            equity = wallet.get(instrument.quote_asset, Decimal(0)) + wallet.get(instrument.base_asset, Decimal(0)) * market.bid
            if equity.copy_abs() >= Decimal("1e20"):
                return {"safe_new_risk": False, "mode": "HALTED", "reasons": ["unsupported_valuation_precision"],
                        "equity": None, "wallet": wallet, "state": dict(state) if state else None}
            equity = equity.quantize(Decimal("1e-18"), rounding=ROUND_FLOOR)
            day = datetime.fromtimestamp(now // 1_000_000_000, tz=timezone.utc).date().isoformat()
            high = max(account["capital"], state["high_water_equity"] if state else account["capital"], equity)
            if state is None:
                day_start = account["capital"]
            else:
                # A mark straddling UTC midnight includes the boundary gap in
                # the new day; it does not quietly erase overnight losses.
                day_start = state["day_start_equity"] if state["day_utc"] == day else state["last_equity"]
            daily_loss, drawdown = max(Decimal(0), day_start - equity), max(Decimal(0), high - equity)
            daily_limit = account["capital"] * min(Decimal("0.005"), deployment.risk_budget.maximum_daily_loss_fraction)
            drawdown_limit = account["capital"] * min(Decimal("0.03"), deployment.risk_budget.maximum_drawdown_fraction)
            if daily_loss >= daily_limit:
                reasons.append("maximum_daily_loss")
            if drawdown >= drawdown_limit:
                reasons.append("maximum_drawdown")
            pending = connection.execute(select(orders).where(
                orders.c.account_id == account["account_id"],
                orders.c.status.not_in(["FILLED", "REJECTED", "CANCELED", "EXPIRED"]),
            )).mappings().all()
            pending_exposure = sum(((row["quantity"] - row["filled_quantity"]) * Decimal(row["payload"]["intent"]["limit_price"])
                                    for row in pending if row["side"] == "buy"), Decimal(0))
            exposure = wallet.get(instrument.base_asset, Decimal(0)) * market.bid + pending_exposure
            if exposure > account["capital"] * min(Decimal("0.30"), deployment.risk_budget.maximum_position_fraction):
                reasons.append("maximum_position_exposure")
            if wallet.get(instrument.quote_asset, Decimal(0)) < account["reserved_quote"]:
                reasons.append("cash_reservation_exceeds_actual_wallet")
            if connection.execute(select(orders.c.intent_id).where(
                orders.c.account_id == account["account_id"], or_(
                    orders.c.status == "UNKNOWN", and_(
                        orders.c.status.in_(["SUBMITTING", "CANCEL_PENDING"]),
                        orders.c.updated_ns <= now - 5_000_000_000,
                    ),
                ),
            )).first():
                reasons.append("execution_requires_reconciliation")
            mode = "HALTED" if any(reason in reasons for reason in (
                "execution_requires_reconciliation", "cash_reservation_exceeds_actual_wallet",
            )) else "REDUCE_ONLY" if reasons else account["mode"]
            if account["mode"] == "HALTED":
                mode = "HALTED"
            return {
                "safe_new_risk": mode == "ACTIVE", "mode": mode, "reasons": reasons,
                "equity": equity, "daily_loss": daily_loss, "drawdown": drawdown,
                "daily_loss_limit": daily_limit, "drawdown_limit": drawdown_limit,
                "valuation_rounding": "floor_to_18_decimal_places",
                "day_utc": day, "day_start_equity": day_start, "high_water_equity": high,
                "wallet": wallet, "state": dict(state) if state else None,
            }

    def check(self, tenant: str, deployment: DeploymentManifest, instrument: InstrumentSpec,
              market: ExecutableMarketSnapshot, *, now_ns: int | None = None) -> dict:
        if now_ns is not None and (type(now_ns) is not int or now_ns < 0):
            raise ValueError("invalid_risk_clock")
        with self.repo.transaction() as connection, localcontext() as ctx:
            ctx.prec = 80
            account = connection.execute(select(accounts).where(
                accounts.c.tenant_id == tenant, accounts.c.account_id == deployment.account_id,
            ).with_for_update()).mappings().first()
            if account is None:
                raise KeyError("account_not_found")
            # Waiting for another account writer may take longer than a newer
            # mark. Sample the real clock only after owning the account lock;
            # explicit replay clocks retain strict regression detection.
            now = time.time_ns() if now_ns is None else now_ns
            ensure_active_deployment(connection, tenant, deployment.account_id, deployment.deployment_id)
            report = self.evaluate_locked(connection, account, deployment, instrument, market, now)
            mode, reasons = report["mode"], report["reasons"]
            old = report.pop("state")
            if report.get("observation_discarded"):
                return {**report, "latched_reasons": old["latched_reasons"], "persisted": False}
            latched = list(dict.fromkeys([*(old["latched_reasons"] if old else []), *reasons]))
            if report["equity"] is not None:
                # Amounts beyond NUMERIC(38,18) cannot be silently rounded into
                # passing risk evidence. Conservative decimal product is exact
                # for the supported venue precision; unsupported precision stops.
                values = {
                    "tenant_id": tenant, "account_id": deployment.account_id,
                    "policy_hash": deployment.risk_policy_hash, "day_utc": report["day_utc"],
                    "day_start_equity": report["day_start_equity"],
                    "high_water_equity": report["high_water_equity"],
                    "last_equity": report["equity"], "last_mark_ns": now,
                    "market": market.model_dump(mode="json"), "latched_reasons": latched,
                }
                insert_once(connection, risk_state, values)
                connection.execute(update(risk_state).where(risk_state.c.account_id == deployment.account_id).values(**values))
                payload = {key: decimal_string(value) if isinstance(value, Decimal) else value for key, value in report.items() if key != "wallet"}
                payload["wallet"] = {key: decimal_string(value) for key, value in report["wallet"].items()}
                identity = content_hash([tenant, deployment.account_id, now, market.content_hash(), payload])
                insert_once(connection, portfolio_snapshots, {
                    "snapshot_id": identity, "tenant_id": tenant, "account_id": deployment.account_id,
                    "observed_ns": now, "market_hash": market.content_hash(),
                    "payload": payload, "sha256": content_hash(payload),
                })
            elif old:
                connection.execute(update(risk_state).where(risk_state.c.account_id == deployment.account_id).values(latched_reasons=latched))
            if mode != account["mode"]:
                connection.execute(update(accounts).where(accounts.c.account_id == deployment.account_id).values(mode=mode))
                self.repo.event(connection, tenant, deployment.account_id, "risk_stop", {"mode": mode, "reasons": reasons})
            return {**report, "latched_reasons": latched, "persisted": True}

    def ensure_new_risk_locked(self, connection, account, deployment, instrument, market, now) -> None:
        report = self.evaluate_locked(connection, account, deployment, instrument, market, now)
        if report.get("observation_discarded"):
            raise StaleMarketObservationError("superseded_market_observation")
        if not report["safe_new_risk"]:
            raise RiskBudgetExceededError("risk_budget_or_health_stop:" + ",".join(report["reasons"]))

    def stop(self, tenant: str, account_id: str, reasons: list[str], *, mode: str = "HALTED",
             deployment_id: str | None = None) -> dict:
        """Latch an observed stop even if the market recovers before this commit."""
        if mode not in {"HALTED", "REDUCE_ONLY"} or not reasons:
            raise ValueError("invalid_risk_stop")
        with self.repo.transaction() as connection:
            account = connection.execute(select(accounts).where(
                accounts.c.tenant_id == tenant, accounts.c.account_id == account_id,
            ).with_for_update()).mappings().first()
            if account is None:
                raise KeyError("account_not_found")
            if deployment_id is not None:
                try:
                    ensure_active_deployment(connection, tenant, account_id, deployment_id)
                except Conflict:
                    return {"account_id": account_id, "status": "inactive", "persisted": False,
                            "reasons": ["not_current_active_deployment"]}
            previous = connection.execute(select(risk_state).where(risk_state.c.account_id == account_id)).mappings().first()
            latched = list(dict.fromkeys([*(previous["latched_reasons"] if previous else []), *reasons]))
            selected = "HALTED" if account["mode"] == "HALTED" else mode
            connection.execute(update(accounts).where(accounts.c.account_id == account_id).values(mode=selected))
            if previous:
                connection.execute(update(risk_state).where(risk_state.c.account_id == account_id).values(latched_reasons=latched))
            self.repo.event(connection, tenant, account_id, "risk_stop", {"mode": selected, "reasons": reasons})
        return {"account_id": account_id, "mode": selected, "reasons": reasons, "persisted": True}
