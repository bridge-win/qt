"""Independent fail-closed watchdog for signed deployments.

This process never submits venue orders. Database failure is reported as an
unpersisted stop; the OMS independently requires successful DB/fencing checks
before every side effect, rather than trusting a cached green watchdog flag.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import and_, or_, select

from qt.contracts import ExecutableMarketSnapshot, InstrumentSpec
from qt.operations.risk import RiskGuard
from qt.persistence.schema import accounts, orders, runtime_health


class Watchdog:
    def __init__(self, service, *, now_ns: Callable[[], int] | None = None):
        self.service = service
        self.repo = service.repository
        self.tenant = service.tenant
        self.risk = RiskGuard(self.repo)
        self.now = now_ns or time.time_ns
        self.injected_clock = now_ns is not None

    def _stop(self, account_id: str, deployment_id: str, reasons: list[str]) -> dict:
        result = self.risk.stop(self.tenant, account_id, reasons, deployment_id=deployment_id)
        return {"deployment_id": deployment_id, "status": "stopped", **result}

    def _active_ids(self) -> list[str]:
        rows = self.service.active_deployments()
        return [row if isinstance(row, str) else row.get("deployment_id", row.get("entity_id")) for row in rows]

    def _instrument(self, manifest) -> InstrumentSpec:
        validation = self.repo.get(self.tenant, "validation", manifest.validation_report_hash)["payload"]
        spec = self.repo.get(self.tenant, "experiment_spec", validation["experiment_hash"])["payload"]
        matches = [InstrumentSpec.model_validate(self.repo.get(self.tenant, "instrument", identity)["payload"])
                   for identity in spec["instrument_spec_hashes"]]
        matches = [item for item in matches if item.venue == manifest.venue]
        if len(matches) != 1:
            raise ValueError("single_verified_instrument_required_for_spot_valuation")
        return matches[0]

    def check_deployment(self, deployment_id: str) -> dict:
        now = self.now()
        account_id = None
        try:
            if deployment_id not in self._active_ids():
                return {"deployment_id": deployment_id, "status": "inactive",
                        "reasons": ["not_current_active_deployment"], "persisted": False}
            # The stored payload is used only to identify which owned account to
            # stop if signature verification fails; it cannot authorize trading.
            raw = self.repo.get(self.tenant, "deployment", deployment_id)["payload"]
            account_id = raw.get("account_id")
            try:
                manifest = self.service.verify_deployment(deployment_id)
                instrument = self._instrument(manifest)
            except Exception:
                return self._stop(account_id, deployment_id, ["deployment_or_instrument_verification_failed"])
            with self.repo.transaction() as connection:
                account = connection.execute(select(accounts).where(
                    accounts.c.account_id == manifest.account_id,
                    accounts.c.tenant_id == self.tenant,
                )).mappings().first()
                health = {row["name"]: dict(row) for row in connection.execute(select(runtime_health).where(
                    runtime_health.c.tenant_id == self.tenant,
                    runtime_health.c.name.in_([
                        f"market:{manifest.venue}:{instrument.instrument}",
                        f"trading:{deployment_id}", f"account:{manifest.account_id}", "clock",
                    ]),
                )).mappings()}
                uncertain = connection.execute(select(orders.c.intent_id).where(
                    orders.c.account_id == manifest.account_id,
                    or_(orders.c.status == "UNKNOWN", and_(
                        orders.c.status.in_(["SUBMITTING", "CANCEL_PENDING"]),
                        orders.c.updated_ns <= now - 5_000_000_000,
                    )),
                )).first()
                pending = connection.execute(select(orders.c.intent_id).where(
                    orders.c.account_id == manifest.account_id,
                    orders.c.status.not_in(["FILLED", "REJECTED", "CANCELED", "EXPIRED"]),
                )).first()
                wallet = self.risk._balances(connection, manifest.account_id)
            # Observations may have been published while this read was queued.
            # Compare freshness against a clock sampled after obtaining them.
            now = self.now()
            if account is None:
                return {"deployment_id": deployment_id, "status": "blocked", "mode": "HALTED",
                        "reasons": ["account_not_initialized"], "persisted": False}
            reasons = []
            heartbeat = health.get(f"trading:{deployment_id}")
            if heartbeat is None and pending is None and not any(
                amount != 0 for asset, amount in wallet.items() if asset != account["quote_asset"]
            ):
                return {"deployment_id": deployment_id, "status": "awaiting_runtime",
                        "mode": account["mode"], "safe_new_risk": False, "persisted": False,
                        "reasons": ["awaiting_first_runtime_heartbeat_and_valid_market"]}
            if account["lease_until_ns"] <= now or not account["lease_token"]:
                reasons.append("execution_lease_lost")
            if uncertain:
                reasons.append("execution_requires_reconciliation")
            if (heartbeat is None or not 0 <= now - heartbeat["observed_ns"] <= 5_000_000_000
                    or heartbeat["payload"].get("healthy") is not True):
                reasons.append("trading_runtime_heartbeat_missing_or_unhealthy")
            market_row = health.get(f"market:{manifest.venue}:{instrument.instrument}")
            if market_row is None or not 0 <= now - market_row["observed_ns"] <= 1_000_000_000:
                reasons.append("market_health_missing_or_stale")
            if "clock" in health:
                clock = health["clock"]
                offset = clock["payload"].get("offset_ms")
                if (not isinstance(offset, (str, Decimal)) or not Decimal(offset).is_finite()
                        or abs(Decimal(offset)) > 50 or clock["payload"].get("healthy") is not True
                        or not 0 <= now - clock["observed_ns"] <= 60_000_000_000):
                    reasons.append("clock_untrusted")
            account_health = health.get(f"account:{manifest.account_id}")
            if account_health is not None:
                observations = account_health["payload"]
                differences = observations.get("differences", {})
                if (observations.get("reconciled") is not True
                        or not isinstance(differences, dict)
                        or any(not isinstance(value, (str, Decimal)) or not Decimal(value).is_finite()
                               or Decimal(value) != 0 for value in differences.values())):
                    reasons.append("account_reconciliation_difference")
            if reasons:
                return self._stop(manifest.account_id, deployment_id, reasons)
            try:
                market = ExecutableMarketSnapshot.model_validate(market_row["payload"])
            except ValueError:
                return self._stop(manifest.account_id, deployment_id, ["market_payload_invalid"])
            result = self.risk.check(self.tenant, manifest, instrument, market,
                                     now_ns=now if self.injected_clock else None)
            return {"deployment_id": deployment_id,
                    "status": "held" if result.get("observation_discarded") else
                    "healthy" if result["safe_new_risk"] else "stopped",
                    **result}
        except Exception as exc:
            # Logging only the exception type avoids exposing connection DSNs,
            # credentials or raw external payloads in reports.
            if account_id is not None:
                try:
                    return self._stop(account_id, deployment_id, ["watchdog_check_failed:" + type(exc).__name__])
                except Exception:
                    pass
            return {"deployment_id": deployment_id, "status": "unavailable", "mode": "HALTED",
                    "reasons": ["watchdog_state_unavailable"], "persisted": False,
                    "error_type": type(exc).__name__}

    def check(self) -> dict:
        try:
            identities = self._active_ids()
        except Exception as exc:
            return {"status": "unavailable", "mode": "HALTED", "persisted": False,
                    "error_type": type(exc).__name__, "deployments": []}
        results = [self.check_deployment(identity) for identity in identities]
        if any(item["status"] == "unavailable" for item in results):
            return {"status": "unavailable", "mode": "HALTED", "persisted": False,
                    "deployments": results, "automatic_reactivation": False}
        return {"status": "healthy" if results and all(item["status"] == "healthy" for item in results)
                else "idle" if not results else "awaiting_runtime" if all(item["status"] == "awaiting_runtime" for item in results)
                else "held" if all(item["status"] in {"healthy", "held"} for item in results)
                else "stopped", "deployments": results,
                "automatic_reactivation": False}


def run_once(service) -> dict:
    return Watchdog(service).check()
