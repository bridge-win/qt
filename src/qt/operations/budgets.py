"""Atomically reserve per-run/day/month spend before any paid model request."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update

from qt.persistence import Conflict, Repository
from qt.persistence.repository import insert_once
from qt.persistence.schema import model_calls, usage_budgets


class UsageLedger:
    def __init__(self, repository: Repository):
        self.repository = repository

    def reserve(self, tenant: str, run: str, call: str, request_hash: str, amount: Decimal,
                *, run_maximum: Decimal, day_maximum: Decimal = Decimal("5"),
                month_maximum: Decimal = Decimal("150")) -> dict:
        limits = (run_maximum, day_maximum, month_maximum)
        if (not amount.is_finite() or amount <= 0
                or any(not limit.is_finite() or limit <= 0 for limit in limits)
                or run_maximum > 1 or day_maximum > 5 or month_maximum > 150):
            raise ValueError("invalid_or_excessive_research_allocation")
        date = datetime.now(timezone.utc)
        periods = {f"day:{date:%Y-%m-%d}": day_maximum,
                   f"month:{date:%Y-%m}": month_maximum, f"run:{run}": run_maximum}
        with self.repository.transaction() as connection:
            # Lock budgets in stable order across all runs; this prevents both
            # overspend under concurrency and deadlocks across period buckets.
            locked = []
            for period, maximum in sorted(periods.items()):
                insert_once(connection, usage_budgets, {"tenant_id": tenant, "period": period,
                                                       "reserved": Decimal(0), "maximum": maximum})
                row = connection.execute(select(usage_budgets).where(
                    usage_budgets.c.tenant_id == tenant, usage_budgets.c.period == period,
                ).with_for_update()).mappings().one()
                locked.append(row)
            existing = connection.execute(select(model_calls).where(
                model_calls.c.tenant_id == tenant, model_calls.c.call_id == call,
            )).mappings().first()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise Conflict("model_call_identity_conflict")
                raise Conflict("model_call_already_reserved_do_not_repeat")
            for row in locked:
                if row["reserved"] + amount > min(row["maximum"], periods[row["period"]]):
                    raise ValueError("research_spend_budget_exhausted")
            for row in locked:
                connection.execute(update(usage_budgets).where(
                    usage_budgets.c.tenant_id == tenant, usage_budgets.c.period == row["period"],
                ).values(reserved=row["reserved"] + amount))
            values = {"tenant_id": tenant, "call_id": call, "run_id": run,
                      "request_hash": request_hash, "periods": sorted(periods),
                      "reserved": amount, "status": "RESERVED", "created_ns": time.time_ns()}
            connection.execute(model_calls.insert().values(**values))
            return values

    def complete(self, tenant: str, call: str, actual: Decimal) -> None:
        if actual < 0 or not actual.is_finite():
            raise ValueError("invalid_actual_cost")
        with self.repository.transaction() as connection:
            found = connection.execute(select(model_calls).where(
                model_calls.c.tenant_id == tenant, model_calls.c.call_id == call,
            )).mappings().one()
            budgets = []
            for period in sorted(found["periods"]):
                budgets.append(connection.execute(select(usage_budgets).where(
                    usage_budgets.c.tenant_id == tenant, usage_budgets.c.period == period,
                ).with_for_update()).mappings().one())
            row = connection.execute(select(model_calls).where(
                model_calls.c.tenant_id == tenant, model_calls.c.call_id == call,
            ).with_for_update()).mappings().one()
            if row["actual"] is not None:
                if row["actual"] != actual:
                    raise Conflict("actual_cost_is_immutable")
                return
            delta = actual - row["reserved"]
            for budget in budgets:
                connection.execute(update(usage_budgets).where(
                    usage_budgets.c.tenant_id == tenant, usage_budgets.c.period == budget["period"],
                ).values(reserved=budget["reserved"] + delta))
            connection.execute(update(model_calls).where(
                model_calls.c.tenant_id == tenant, model_calls.c.call_id == call,
            ).values(actual=actual, status="EXCEEDED" if delta > 0 else "COMPLETED"))
