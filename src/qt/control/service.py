"""Application services bind immutable contracts to real queued work."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

from qt.contracts import (
    DatasetManifest,
    DeploymentManifest,
    ExperimentSpecV2,
    FeeSchedule,
    InstrumentSpec,
    StrategyArtifact,
    canonical_json,
    content_hash,
)
from qt.datasets import DatasetRegistry
from qt.experiments.registry import ExperimentRegistry
from qt.operations.gates import INTEGRITY, GatePolicy
from qt.operations.releases import DeploymentRegistry
from qt.persistence import Repository


def parse(model, value: dict):
    """Validate JSON wire semantics (JSON arrays -> immutable tuple fields)."""
    return model.model_validate_json(json.dumps(value, allow_nan=False))


class ControlService:
    def __init__(self, repository: Repository, data_root: Path, *, tenant: str = "personal",
                 signing_key: bytes | None = None):
        self.repository = repository
        self.datasets = DatasetRegistry(data_root)
        self.experiments = ExperimentRegistry(repository.engine)
        self.policy = GatePolicy()
        self.tenant = tenant
        self.signing_key = signing_key

    def register(self, kind: str, payload: dict) -> dict:
        models = {"strategy": StrategyArtifact, "instrument": InstrumentSpec,
                  "fees": FeeSchedule, "dataset": DatasetManifest}
        if kind not in models:
            raise ValueError("unsupported_registration_kind")
        contract = parse(models[kind], payload)
        if kind == "dataset":
            self.datasets.verify(contract)
            identity = contract.dataset_id
        else:
            identity = contract.content_hash()
        return self.repository.put(self.tenant, kind, contract.model_dump(mode="json"), identity)

    def create_experiment(self, payload: dict, key: str,
                          *, fence_job: tuple[str, str] | None = None) -> dict:
        spec = parse(ExperimentSpecV2, payload)
        strategy = self.repository.get(self.tenant, "strategy", spec.strategy_version)
        declared = strategy["payload"]["instruments"]
        if any(symbol not in declared for symbol in spec.instruments):
            raise ValueError("experiment_instrument_not_in_strategy")
        if len(spec.instrument_spec_hashes) != len(spec.instruments):
            raise ValueError("explicit_instrument_version_hashes_required")
        campaign = self.experiments.campaign(self.tenant, spec.campaign_id)
        if spec.dataset_ids != (campaign["dataset_id"],):
            raise ValueError("experiment_dataset_must_match_registered_campaign")
        for instrument_hash in spec.instrument_spec_hashes:
            self.repository.get(self.tenant, "instrument", instrument_hash)
        last_end = spec.split.test_end_ns + (spec.split.folds - 1) * (
            spec.split.test_end_ns - spec.split.validation_end_ns
        )
        for dataset_id in spec.dataset_ids:
            registered = self.repository.get(self.tenant, "dataset", dataset_id)
            self.datasets.verify(parse(DatasetManifest, registered["payload"]))
            self.experiments.guard_development_read(
                self.tenant, dataset_id=dataset_id,
                start_ns=spec.split.training_start_ns, end_ns=last_end + 1,
            )
        for fee_hash in spec.cost_model.fee_schedule_hashes:
            self.repository.get(self.tenant, "fees", fee_hash)
        identity = spec.content_hash()
        self.experiments.register_trial(self.tenant, spec.campaign_id, identity,
                                        spec.model_dump(mode="json"))
        self.repository.put(self.tenant, "experiment_spec", spec.model_dump(mode="json"), identity,
                            fence_job=fence_job)
        return self.repository.enqueue(self.tenant, "experiment", {"experiment_hash": identity}, key,
                                        fence_job=fence_job)

    def validation(self, experiment_hash: str, stage: str = "paper") -> dict:
        result = self.repository.get(self.tenant, "experiment_result", experiment_hash)
        spec = self.repository.get(self.tenant, "experiment_spec", experiment_hash)
        evidence = dict(result["payload"].get("gate_evidence", {}))
        # Forward acceptance is bound to this precise experiment. Ordinary HTTP
        # callers have no endpoint for manufacturing acceptance observations.
        try:
            observed = self.repository.get(self.tenant, "acceptance", experiment_hash)["payload"]
            if observed.get("experiment_hash") == experiment_hash:
                immutable_checks = {rule.name for rule in INTEGRITY}
                evidence.update({key: value for key, value in observed.get("observations", {}).items()
                                 if key not in immutable_checks})
        except KeyError:
            pass
        instruments = [self.repository.get(self.tenant, "instrument", identity)["payload"]
                       for identity in spec["payload"]["instrument_spec_hashes"]]
        prediction = any(item["instrument_type"] == "prediction"
                         or item["venue"] == "polymarket" for item in instruments)
        # Frequency and venue are immutable experiment inputs, not claims in a
        # caller-supplied performance report.
        low_frequency = max(spec["payload"]["timeframes_ns"]) >= 3_600_000_000_000
        verdict = self.policy.evaluate(stage, evidence, prediction_market=prediction,
                                       low_frequency=low_frequency)
        return {"experiment_hash": experiment_hash, "result_hash": result["sha256"],
                "strategy_hash": spec["payload"]["strategy_version"], **verdict}

    def save_validation(self, experiment_hash: str, stage: str = "paper") -> dict:
        verdict = self.validation(experiment_hash, stage)
        return self.repository.put(self.tenant, "validation", verdict)

    def create_deployment(self, payload: dict, key: str) -> dict:
        if not self.signing_key or len(self.signing_key) < 32:
            raise ValueError("deployment_signing_key_not_configured")
        # The caller supplies a manifest without a signature. All version and
        # evidence relationships are verified before the server signs it.
        identity = content_hash([self.tenant, "deployment", key])
        body = {**payload, "deployment_id": identity, "signature": "pending"}
        manifest = parse(DeploymentManifest, body)
        strategy = self.repository.get(self.tenant, "strategy", manifest.strategy_hash)
        report = self.repository.get(self.tenant, "validation", manifest.validation_report_hash)
        verdict = self.validation(report["payload"]["experiment_hash"], manifest.mode)
        if (not verdict["eligible"] or report["payload"] != verdict
                or verdict["policy_hash"] != manifest.risk_policy_hash
                or verdict["strategy_hash"] != manifest.strategy_hash):
            raise ValueError("deployment_acceptance_missing_stale_or_mismatched")
        if manifest.risk_budget.model_dump(mode="json") != strategy["payload"]["risk_budget"]:
            raise ValueError("deployment_risk_budget_must_match_validated_strategy")
        if manifest.valid_until_ns <= time.time_ns():
            raise ValueError("deployment_already_expired")
        spec = self.repository.get(self.tenant, "experiment_spec", verdict["experiment_hash"])["payload"]
        if set(manifest.fee_schedule_hashes) != set(spec["cost_model"]["fee_schedule_hashes"]):
            raise ValueError("deployment_fee_schedule_changed")
        if manifest.mode == "live_canary":
            try:
                acceptance = self.repository.get(self.tenant, "acceptance", verdict["experiment_hash"])["payload"]
            except KeyError:
                raise ValueError("live_forward_acceptance_required") from None
            if acceptance.get("deployment_binding_hash") != self.deployment_binding_hash(manifest):
                raise ValueError("live_account_model_or_configuration_not_forward_tested")
        for fee_hash in manifest.fee_schedule_hashes:
            fee = self.repository.get(self.tenant, "fees", fee_hash)["payload"]
            if fee["venue"] != manifest.venue:
                raise ValueError("deployment_fee_venue_mismatch")
        unsigned = manifest.model_dump(mode="json", exclude={"signature"})
        signature = hmac.new(self.signing_key, canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
        signed = {**unsigned, "signature": signature}
        # Logical deployment identity is client-visible and is also signed.
        if manifest.deployment_id != identity:
            raise ValueError("deployment_id_must_match_idempotency_identity")
        return self.deployments.activate(self.tenant, parse(DeploymentManifest, signed))

    @staticmethod
    def deployment_binding_hash(manifest: DeploymentManifest) -> str:
        """Bind forward evidence to actual account/configuration, across modes.

        Dates, mode, generated ID and signature may change on promotion. Capital,
        strategy, research identity, fees and risk policy may not silently change.
        """
        fields = {"strategy_hash", "provider", "model_id", "prompt_hash", "tools_hash",
                  "risk_policy_hash", "fee_schedule_hashes", "venue", "account_id", "risk_budget"}
        return content_hash(manifest.model_dump(mode="json", include=fields))

    def verify_deployment(self, deployment_id: str) -> DeploymentManifest:
        return self.deployments.verify(self.tenant, deployment_id)

    @property
    def deployments(self) -> DeploymentRegistry:
        return DeploymentRegistry(self.repository, signing_key=self.signing_key,
                                  policy_hash=self.policy.fingerprint())

    def active_deployments(self) -> list[dict]:
        return self.deployments.active(self.tenant)

    def is_active_deployment(self, deployment_id: str) -> bool:
        return self.deployments.is_active(self.tenant, deployment_id)

    def deactivate_deployment(self, deployment_id: str, *, reason: str) -> dict:
        return self.deployments.deactivate(self.tenant, deployment_id, reason=reason)

    def rollback_deployment(
        self, deployment_id: str, key: str, *, reason: str, valid_until_ns: int,
    ) -> dict:
        """Create a newly signed release from historical rules after fresh gates.

        The historical manifest remains immutable. The registry's flat-account
        and no-risk-increase rules apply to rollback just as to a new release.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rollback_reason_required")
        # Verify historical authenticity without granting it execution authority.
        historical = self.deployments.read_signed(self.tenant, deployment_id)
        body = historical.model_dump(mode="json", exclude={"deployment_id", "signature"})
        body.update(valid_from_ns=time.time_ns(), valid_until_ns=valid_until_ns)
        result = self.create_deployment(body, key)
        with self.repository.transaction() as connection:
            self.repository.event(connection, self.tenant, result["entity_id"], "deployment_rollback", {
                "source_deployment_id": deployment_id, "reason": reason,
            })
        return result

    def readiness(self) -> dict:
        results = self.repository.list(self.tenant, "experiment_result", limit=100)
        engineering = []
        strategy = []
        for result in results:
            identity = result["entity_id"]
            engineering.append(self.validation(identity, "paper"))
            strategy.append(self.validation(identity, "live_canary"))
        return {
            "schema_version": "2.0", "policy": self.policy.definition(),
            "engineering": {"status": "partial" if any(x["eligible"] for x in engineering) else "unverified",
                            "reason": "experiment_integrity_only_operational_soak_and_recovery_acceptance_required",
                            "experiments": engineering},
            "research": {"status": "unverified", "reason": "frozen_agent_ablation_dataset_required"},
            "strategy": {"status": "verified" if any(x["eligible"] for x in strategy) else "unverified",
                         "experiments": strategy},
            "economics": {"status": "unverified", "reason": "actual_cost_and_forward_profit_evidence_required"},
            "live_ready": any(x["eligible"] for x in strategy),
            "browser_acceptance": "deferred_by_user",
        }
