"""Signed immutable deployment history with a serialized active account head.

Changing a head never rewrites prior manifests, orders, fills or risk history.
This first release permits replacement/deactivation only when the account is
flat and has no outstanding execution/settlement, and forbids risk escalation.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select, update

from qt.contracts import DeploymentManifest, canonical_json, content_hash
from qt.persistence import Conflict, Repository
from qt.persistence.repository import insert_once
from qt.persistence.schema import accounts, artifacts, deployment_heads, ledger_entries, orders

_TERMINAL = {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}


class DeploymentRegistry:
    def __init__(self, repository: Repository, *, signing_key: bytes | None, policy_hash: str):
        self.repo, self.signing_key, self.policy_hash = repository, signing_key, policy_hash

    def _signature(self, manifest: DeploymentManifest, *, current: bool) -> None:
        if not self.signing_key or len(self.signing_key) < 32:
            raise Conflict("deployment_signing_key_not_configured")
        unsigned = manifest.model_dump(mode="json", exclude={"signature"})
        expected = hmac.new(self.signing_key, canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(manifest.signature, expected):
            raise Conflict("deployment_signature_invalid")
        if current and (not manifest.valid_from_ns <= time.time_ns() < manifest.valid_until_ns
                        or manifest.risk_policy_hash != self.policy_hash):
            raise Conflict("deployment_signature_expired_or_policy_changed")

    @staticmethod
    def _stored(connection: Any, tenant: str, identity: str) -> dict[str, Any]:
        row = connection.execute(select(artifacts).where(
            artifacts.c.tenant_id == tenant, artifacts.c.kind == "deployment",
            artifacts.c.entity_id == identity,
        )).mappings().first()
        if row is None:
            raise KeyError("deployment_not_found")
        if content_hash(row["payload"]) != row["sha256"]:
            raise Conflict("deployment_artifact_integrity_failure")
        return dict(row)

    @staticmethod
    def _flat(connection: Any, tenant: str, account_id: str) -> dict[str, Any] | None:
        account = connection.execute(select(accounts).where(
            accounts.c.tenant_id == tenant, accounts.c.account_id == account_id,
        ).with_for_update()).mappings().first()
        if account is None:
            if connection.execute(select(accounts.c.account_id).where(
                accounts.c.account_id == account_id,
            )).first() is not None:
                raise Conflict("deployment_account_is_not_owned_by_tenant")
            return None
        pending = connection.execute(select(orders.c.intent_id).where(
            orders.c.account_id == account_id,
            (orders.c.status.not_in(_TERMINAL)
             | orders.c.settlement_status.not_in({"NOT_REQUIRED", "CONFIRMED"})),
        )).first()
        if pending is not None or account["reserved_quote"] != 0:
            raise Conflict("deployment_change_requires_no_orders_or_unsettled_effects")
        wallet: dict[str, Decimal] = {}
        with localcontext() as context:
            context.prec = 80
            for asset, amount in connection.execute(select(ledger_entries.c.asset, ledger_entries.c.amount).where(
                ledger_entries.c.account_id == account_id, ledger_entries.c.bucket == "wallet",
            )):
                wallet[asset] = wallet.get(asset, Decimal("0")) + amount
        if any(value != 0 for asset, value in wallet.items() if asset != account["quote_asset"]):
            raise Conflict("deployment_change_requires_flat_inventory")
        if wallet.get(account["quote_asset"], Decimal("0")) < 0:
            raise Conflict("deployment_change_requires_reconciled_cash")
        return dict(account)

    @staticmethod
    def _no_escalation(previous: DeploymentManifest, manifest: DeploymentManifest) -> None:
        old, new = previous.risk_budget, manifest.risk_budget
        if old.currency != new.currency or any(
            getattr(new, name) > getattr(old, name)
            for name in ("capital", "maximum_position_fraction", "maximum_order_fraction",
                         "planned_loss_fraction", "maximum_drawdown_fraction",
                         "maximum_daily_loss_fraction", "maximum_leverage")
        ):
            raise Conflict("deployment_risk_increase_requires_separate_explicit_review")

    def activate(self, tenant: str, manifest: DeploymentManifest, *, reason: str = "deployment_create") -> dict[str, Any]:
        """Called only after ControlService has re-evaluated all validation gates."""
        self._signature(manifest, current=True)
        if not reason.strip() or len(reason) > 1024:
            raise ValueError("deployment_activation_reason_required")
        now = time.time_ns()
        body = manifest.model_dump(mode="json")
        digest = content_hash(body)
        with self.repo.transaction() as connection:
            # Same lock order as the OMS: account -> lifecycle head -> order.
            connection.execute(select(accounts.c.account_id).where(
                accounts.c.tenant_id == tenant, accounts.c.account_id == manifest.account_id,
            ).with_for_update()).first()
            insert_once(connection, deployment_heads, {
                "tenant_id": tenant, "account_id": manifest.account_id, "deployment_id": None,
                "status": "INACTIVE", "generation": 0, "updated_ns": now, "reason": "uninitialized",
            })
            head = connection.execute(select(deployment_heads).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.account_id == manifest.account_id,
            ).with_for_update()).mappings().one()
            existing = connection.execute(select(artifacts).where(
                artifacts.c.tenant_id == tenant, artifacts.c.kind == "deployment",
                artifacts.c.entity_id == manifest.deployment_id,
            )).mappings().first()
            if existing is not None:
                if existing["sha256"] != digest:
                    raise Conflict("immutable_deployment_identity_conflict")
                if head["deployment_id"] == manifest.deployment_id and head["status"] == "ACTIVE":
                    return dict(existing)
                raise Conflict("deployment_identity_is_historical_use_explicit_rollback")
            if head["deployment_id"] is not None:
                previous = DeploymentManifest.model_validate(
                    self._stored(connection, tenant, head["deployment_id"])["payload"],
                )
                self._signature(previous, current=False)
                self._no_escalation(previous, manifest)
            account = self._flat(connection, tenant, manifest.account_id)
            if account is None and manifest.mode != "paper":
                raise Conflict("shadow_or_funded_deployment_requires_existing_reconciled_account")
            if account is not None and (
                account["capital"] != manifest.risk_budget.capital
                or account["quote_asset"] != manifest.risk_budget.currency
            ):
                raise Conflict("deployment_capital_or_currency_requires_separate_account_review")
            self._signature(manifest, current=True)  # expiry may occur while waiting for the head lock
            insert_once(connection, artifacts, {
                "tenant_id": tenant, "kind": "deployment", "entity_id": manifest.deployment_id,
                "payload": body, "sha256": digest, "created_ns": now,
            })
            connection.execute(update(deployment_heads).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.account_id == manifest.account_id,
            ).values(deployment_id=manifest.deployment_id, status="ACTIVE", generation=head["generation"] + 1,
                     updated_ns=now, reason=reason))
            self.repo.event(connection, tenant, manifest.deployment_id, "deployment_activated", {
                "account_id": manifest.account_id, "previous_id": head["deployment_id"],
                "generation": head["generation"] + 1, "reason": reason,
            })
            return self._stored(connection, tenant, manifest.deployment_id)

    def is_active(self, tenant: str, deployment_id: str) -> bool:
        with self.repo.transaction() as connection:
            return connection.execute(select(deployment_heads.c.deployment_id).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.deployment_id == deployment_id,
                deployment_heads.c.status == "ACTIVE",
            )).first() is not None

    def verify(self, tenant: str, deployment_id: str) -> DeploymentManifest:
        with self.repo.transaction() as connection:
            row = self._stored(connection, tenant, deployment_id)
            manifest = DeploymentManifest.model_validate(row["payload"])
            self._signature(manifest, current=True)
            head = connection.execute(select(deployment_heads).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.account_id == manifest.account_id,
            )).mappings().first()
            if head is None or head["status"] != "ACTIVE" or head["deployment_id"] != deployment_id:
                raise Conflict("deployment_is_not_the_active_account_head")
        return manifest

    def read_signed(self, tenant: str, deployment_id: str) -> DeploymentManifest:
        """Read authentic historical rules without conferring execution authority."""
        with self.repo.transaction() as connection:
            manifest = DeploymentManifest.model_validate(self._stored(connection, tenant, deployment_id)["payload"])
            self._signature(manifest, current=False)
            return manifest

    def active(self, tenant: str) -> list[dict[str, Any]]:
        with self.repo.transaction() as connection:
            heads = connection.execute(select(deployment_heads).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.status == "ACTIVE",
            ).order_by(deployment_heads.c.account_id)).mappings().all()
            return [{"deployment_id": head["deployment_id"], "account_id": head["account_id"],
                     "generation": head["generation"],
                     "payload": self._stored(connection, tenant, head["deployment_id"])["payload"]}
                    for head in heads]

    def deactivate(self, tenant: str, deployment_id: str, *, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise ValueError("deployment_deactivation_reason_required")
        with self.repo.transaction() as connection:
            manifest = DeploymentManifest.model_validate(self._stored(connection, tenant, deployment_id)["payload"])
            self._signature(manifest, current=False)
            connection.execute(select(accounts.c.account_id).where(
                accounts.c.tenant_id == tenant, accounts.c.account_id == manifest.account_id,
            ).with_for_update()).first()
            head = connection.execute(select(deployment_heads).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.account_id == manifest.account_id,
            ).with_for_update()).mappings().first()
            if head is None or head["deployment_id"] != deployment_id:
                raise Conflict("only_the_current_head_can_be_deactivated")
            if head["status"] == "INACTIVE":
                return dict(head)
            self._flat(connection, tenant, manifest.account_id)
            values = {"status": "INACTIVE", "generation": head["generation"] + 1,
                      "updated_ns": time.time_ns(), "reason": reason}
            connection.execute(update(deployment_heads).where(
                deployment_heads.c.tenant_id == tenant, deployment_heads.c.account_id == manifest.account_id,
            ).values(**values))
            self.repo.event(connection, tenant, deployment_id, "deployment_deactivated", {"reason": reason})
            return {**dict(head), **values}
