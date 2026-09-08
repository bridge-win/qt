# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

from qt.legacy.btc_quant_evolution.evolution.walk_forward_evolution import (
    build_evolution_split_plan,
)
from qt.legacy.btc_quant_evolution.walk_forward import WalkForwardRequest, build_walk_forward_splits

PHASES = ("collect", "matrix", "research", "evolve", "dry-run", "live")
LEGACY_PRODUCTION_STRATEGY = "BtcMultiSourceRegimeStrategy"
PRODUCTION_STRATEGY_TIMEFRAMES = {
    "Sota": frozenset({"4h"}),
    LEGACY_PRODUCTION_STRATEGY: frozenset({"4h"}),
}
_SCHEMA_VERSION = 2
_PHASE_STATUSES = frozenset({"pending", "running", "passed", "failed", "blocked"})
_PHASE_RECORD_KEYS = frozenset(
    {"completed_at", "failure_reason", "input_fingerprint", "outputs", "started_at", "status"}
)
_ARTIFACT_KEYS = frozenset({"exists", "path", "sha256", "size_bytes"})
_HYPEROPT_DEFAULTS: dict[str, int | None] = {
    "hyperopt_epochs": 100,
    "hyperopt_max_splits": None,
    "hyperopt_min_train_trades": 5,
    "hyperopt_random_state": 42,
    "hyperopt_step_days": 90,
    "hyperopt_test_days": 90,
    "hyperopt_train_days": 365,
    "hyperopt_workers": 1,
}


@dataclass(frozen=True)
class ProductionRunConfig:
    root_dir: Path
    run_id: str
    through: str
    source_policy: str
    symbol: str
    timeframe: str
    start: str
    end: str
    git_revision: str
    strategy: str = "Sota"
    confirm_live: bool = False
    provider_fingerprint: str = ""
    hyperopt_epochs: int = 100
    hyperopt_random_state: int = 42
    hyperopt_train_days: int = 365
    hyperopt_test_days: int = 90
    hyperopt_step_days: int = 90
    hyperopt_min_train_trades: int = 5
    hyperopt_workers: int = 1
    hyperopt_max_splits: int | None = None

    def arguments(self) -> dict[str, str | bool | int | None]:
        return {
            "confirm_live": self.confirm_live,
            "end": self.end,
            "hyperopt_epochs": self.hyperopt_epochs,
            "hyperopt_max_splits": self.hyperopt_max_splits,
            "hyperopt_min_train_trades": self.hyperopt_min_train_trades,
            "hyperopt_random_state": self.hyperopt_random_state,
            "hyperopt_step_days": self.hyperopt_step_days,
            "hyperopt_test_days": self.hyperopt_test_days,
            "hyperopt_train_days": self.hyperopt_train_days,
            "hyperopt_workers": self.hyperopt_workers,
            "source_policy": self.source_policy,
            "start": self.start,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "through": self.through,
            "timeframe": self.timeframe,
        }

    def identity_arguments(self) -> dict[str, str | int | None]:
        arguments: dict[str, str | int | None] = {
            "end": self.end,
            "source_policy": self.source_policy,
            "start": self.start,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
        }
        arguments.update(self.hyperopt_settings())
        return arguments

    def hyperopt_settings(self) -> dict[str, int | None]:
        return {
            "hyperopt_epochs": self.hyperopt_epochs,
            "hyperopt_max_splits": self.hyperopt_max_splits,
            "hyperopt_min_train_trades": self.hyperopt_min_train_trades,
            "hyperopt_random_state": self.hyperopt_random_state,
            "hyperopt_step_days": self.hyperopt_step_days,
            "hyperopt_test_days": self.hyperopt_test_days,
            "hyperopt_train_days": self.hyperopt_train_days,
            "hyperopt_workers": self.hyperopt_workers,
        }

    def rolling_hyperopt_splits(self) -> list[dict[str, object]]:
        request = WalkForwardRequest(
            exchange="binance",
            pair=self.symbol,
            timeframe=self.timeframe,
            strategy=self.strategy,
            start=date.fromisoformat(self.start),
            end=date.fromisoformat(self.end),
            train_days=self.hyperopt_train_days,
            test_days=self.hyperopt_test_days,
            step_days=self.hyperopt_step_days,
        )
        splits = build_walk_forward_splits(request)
        if self.hyperopt_max_splits is not None:
            splits = splits[: self.hyperopt_max_splits]
        return [split.as_dict() for split in splits]

    def execution_plan(self) -> dict[str, object]:
        start = date.fromisoformat(self.start)
        end = date.fromisoformat(self.end)
        window = {
            "end": self.end,
            "semantics": "[start,end)",
            "start": self.start,
        }
        plan: dict[str, object] = {
            "collection_window": {
                "end": self.end,
                "start": (start - timedelta(days=1)).isoformat(),
                "warmup_days": 1,
            },
            "effective_matrix_window": dict(window),
            "requested_window": dict(window),
            "strategy": self.strategy,
        }
        if self.strategy == "Sota":
            rolling_splits = self.rolling_hyperopt_splits()
            plan.update(
                {
                    "evolution_engine": "rolling_hyperopt_walk_forward",
                    "rolling_hyperopt": {
                        "epochs": self.hyperopt_epochs,
                        "max_splits": self.hyperopt_max_splits,
                        "min_train_trades": self.hyperopt_min_train_trades,
                        "random_state": self.hyperopt_random_state,
                        "spaces": ["buy"],
                        "split_plan": rolling_splits,
                        "split_count": len(rolling_splits),
                        "step_days": self.hyperopt_step_days,
                        "test_days": self.hyperopt_test_days,
                        "train_days": self.hyperopt_train_days,
                        "workers": self.hyperopt_workers,
                    },
                }
            )
            return plan
        plan["evolution_split_plan"] = [
            split.as_dict() for split in build_evolution_split_plan(start, end)
        ]
        return plan


class ProductionManifestStore:
    def __init__(self, config: ProductionRunConfig) -> None:
        self.config = config
        self.root_dir = config.root_dir.resolve()
        self.run_dir = self.root_dir / "user_data" / "production_runs" / config.run_id
        self.manifest_path = self.run_dir / "manifest.json"

    @classmethod
    def create(cls, config: ProductionRunConfig) -> ProductionManifestStore:
        store = cls(config)
        if store.manifest_path.exists():
            raise FileExistsError(f"manifest already exists for run {config.run_id}")

        store.run_dir.mkdir(parents=True, exist_ok=True)
        created_at = _timestamp()
        store._write(
            {
                "arguments": config.arguments(),
                "created_at": created_at,
                "execution_plan": config.execution_plan(),
                "git_revision": config.git_revision,
                "highest_completed_phase": None,
                "phases": {
                    phase: {
                        "completed_at": None,
                        "failure_reason": None,
                        "input_fingerprint": None,
                        "outputs": [],
                        "started_at": None,
                        "status": "pending",
                    }
                    for phase in PHASES
                },
                "provider_fingerprint": config.provider_fingerprint,
                "run_id": config.run_id,
                "schema_version": _SCHEMA_VERSION,
                "status": "pending",
                "updated_at": created_at,
            }
        )
        return store

    def load(self) -> dict[str, object]:
        manifest = self._read()
        phases = _phases(manifest)
        persisted_arguments = manifest["arguments"]
        legacy_strategy_manifest = self._is_legacy_strategy_manifest(manifest)
        explicit_legacy_manifest = self._is_pre_hyperopt_explicit_legacy_manifest(manifest)
        legacy_hyperopt_manifest = legacy_strategy_manifest or explicit_legacy_manifest
        persisted_identity = {
            key: (
                LEGACY_PRODUCTION_STRATEGY
                if key == "strategy" and legacy_strategy_manifest
                else _HYPEROPT_DEFAULTS[key]
                if legacy_hyperopt_manifest
                and key in _HYPEROPT_DEFAULTS
                and key not in persisted_arguments
                else persisted_arguments.get(key)
            )
            for key in self.config.identity_arguments()
        }
        persisted_execution_plan = dict(manifest["execution_plan"])
        if legacy_strategy_manifest:
            persisted_execution_plan["strategy"] = LEGACY_PRODUCTION_STRATEGY
        if (
            persisted_identity != self.config.identity_arguments()
            or manifest["git_revision"] != self.config.git_revision
            or manifest["provider_fingerprint"] != self.config.provider_fingerprint
            or persisted_execution_plan != self.config.execution_plan()
        ):
            manifest["arguments"] = self.config.arguments()
            manifest["execution_plan"] = self.config.execution_plan()
            manifest["git_revision"] = self.config.git_revision
            manifest["provider_fingerprint"] = self.config.provider_fingerprint
            self._reset_phases_from(phases, "collect")
            manifest["highest_completed_phase"] = None
            manifest["status"] = "pending"
            manifest["updated_at"] = _timestamp()
            self._write(manifest)
            return manifest

        invocation_changed = manifest["arguments"] != self.config.arguments()
        if legacy_hyperopt_manifest:
            self._migrate_legacy_strategy_fingerprints(
                manifest,
                phases,
                strategy_was_explicit=explicit_legacy_manifest,
            )
            manifest["arguments"] = self.config.arguments()
            manifest["execution_plan"] = self.config.execution_plan()
            invocation_changed = True
        if invocation_changed:
            manifest["arguments"] = self.config.arguments()

        running_phase = next(
            (phase for phase in PHASES if _phase_record(phases, phase).get("status") == "running"),
            None,
        )
        if running_phase is not None:
            self._reset_phases_from(phases, running_phase)
            manifest["highest_completed_phase"] = _highest_completed_phase(phases)
            manifest["status"] = _manifest_status(phases)
            manifest["updated_at"] = _timestamp()
            self._write(manifest)
        elif invocation_changed:
            manifest["updated_at"] = _timestamp()
            self._write(manifest)
        return manifest

    @staticmethod
    def _is_legacy_strategy_manifest(manifest: dict[str, object]) -> bool:
        arguments = manifest["arguments"]
        execution_plan = manifest["execution_plan"]
        return (
            isinstance(arguments, dict)
            and isinstance(execution_plan, dict)
            and "strategy" not in arguments
            and "strategy" not in execution_plan
        )

    @staticmethod
    def _is_pre_hyperopt_explicit_legacy_manifest(manifest: dict[str, object]) -> bool:
        arguments = manifest["arguments"]
        execution_plan = manifest["execution_plan"]
        return (
            isinstance(arguments, dict)
            and isinstance(execution_plan, dict)
            and arguments.get("strategy") == LEGACY_PRODUCTION_STRATEGY
            and execution_plan.get("strategy") == LEGACY_PRODUCTION_STRATEGY
            and not any(key in arguments for key in _HYPEROPT_DEFAULTS)
            and "evolution_split_plan" in execution_plan
            and "rolling_hyperopt" not in execution_plan
        )

    def _migrate_legacy_strategy_fingerprints(
        self,
        manifest: dict[str, object],
        phases: dict[str, object],
        *,
        strategy_was_explicit: bool = False,
    ) -> None:
        for phase in PHASES:
            record = _phase_record(phases, phase)
            if record.get("status") != "passed":
                continue
            if record.get("input_fingerprint") != self._legacy_input_fingerprint(
                manifest,
                phase,
                strategy_was_explicit=strategy_was_explicit,
            ):
                continue
            record["input_fingerprint"] = self._input_fingerprint(manifest, phase)

    def _legacy_input_fingerprint(
        self,
        manifest: dict[str, object],
        phase: str,
        *,
        strategy_was_explicit: bool = False,
    ) -> str:
        arguments = self.config.identity_arguments()
        execution_plan = self.config.execution_plan()
        for key in _HYPEROPT_DEFAULTS:
            arguments.pop(key)
        if not strategy_was_explicit:
            arguments.pop("strategy")
            execution_plan.pop("strategy")
        payload = {
            "arguments": arguments,
            "execution_plan": execution_plan,
            "git_revision": self.config.git_revision,
            "provider_fingerprint": self.config.provider_fingerprint,
            "predecessor_output_hashes": self._predecessor_output_hashes(manifest, phase),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def start_phase(self, phase: str) -> dict[str, object]:
        manifest = self._read()
        self._require_known_phase(phase)
        phases = _phases(manifest)
        self._require_predecessors_reusable(manifest, phase)
        self._reset_phases_from(phases, phase)
        record = _phase_record(phases, phase)
        record.update(
            {
                "completed_at": None,
                "failure_reason": None,
                "input_fingerprint": self._input_fingerprint(manifest, phase),
                "outputs": [],
                "started_at": _timestamp(),
                "status": "running",
            }
        )
        manifest["highest_completed_phase"] = _highest_completed_phase(phases)
        manifest["status"] = "running"
        manifest["updated_at"] = _timestamp()
        self._write(manifest)
        return manifest

    def complete_phase(self, phase: str, outputs: Sequence[Path]) -> dict[str, object]:
        manifest = self._read()
        self._require_known_phase(phase)
        phases = _phases(manifest)
        record = _phase_record(phases, phase)
        if record.get("status") != "running":
            raise ValueError(f"{phase} is not running")

        record.update(
            {
                "completed_at": _timestamp(),
                "failure_reason": None,
                "outputs": sorted(
                    (_artifact_record(path, self.root_dir) for path in outputs),
                    key=lambda item: item["path"],
                ),
                "status": "passed",
            }
        )
        manifest["highest_completed_phase"] = _highest_completed_phase(phases)
        manifest["status"] = _manifest_status(phases)
        manifest["updated_at"] = _timestamp()
        self._write(manifest)
        return manifest

    def fail_phase(
        self,
        phase: str,
        reason: str,
        outputs: Sequence[Path] = (),
    ) -> dict[str, object]:
        return self._finish_unsuccessful_phase(phase, reason, "failed", outputs)

    def blocked_phase(
        self,
        phase: str,
        reason: str,
        outputs: Sequence[Path] = (),
    ) -> dict[str, object]:
        return self._finish_unsuccessful_phase(phase, reason, "blocked", outputs)

    def reusable_phase(self, phase: str) -> bool:
        manifest = self._read()
        self._require_known_phase(phase)
        return self._is_reusable_phase(manifest, phase)

    def attach_phase_outputs(self, phase: str, outputs: Sequence[Path]) -> dict[str, object]:
        manifest = self._read()
        self._require_known_phase(phase)
        phases = _phases(manifest)
        record = _phase_record(phases, phase)
        if record.get("status") != "passed":
            raise ValueError(f"{phase} is not passed")
        if not self._is_reusable_phase(manifest, phase):
            raise ValueError(f"{phase} is not reusable")

        additions = [_artifact_record(path, self.root_dir) for path in outputs]
        if any(artifact["exists"] is not True for artifact in additions):
            raise ValueError(f"cannot attach missing output to {phase}")
        existing_outputs = record.get("outputs")
        if not isinstance(existing_outputs, list):
            raise ValueError(f"manifest {phase} outputs must be a list")
        merged = {
            artifact["path"]: artifact
            for artifact in (*existing_outputs, *additions)
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
        }
        record["outputs"] = [merged[path] for path in sorted(merged)]
        phase_index = PHASES.index(phase)
        if phase_index + 1 < len(PHASES):
            self._reset_phases_from(phases, PHASES[phase_index + 1])
        manifest["highest_completed_phase"] = _highest_completed_phase(phases)
        manifest["status"] = _manifest_status(phases)
        manifest["updated_at"] = _timestamp()
        self._write(manifest)
        return manifest

    def _is_reusable_phase(self, manifest: dict[str, object], phase: str) -> bool:
        record = _phase_record(_phases(manifest), phase)
        if record.get("status") != "passed":
            return False
        if record.get("input_fingerprint") != self._input_fingerprint(manifest, phase):
            return False

        outputs = record.get("outputs")
        return isinstance(outputs, list) and all(self._artifact_matches(artifact) for artifact in outputs)

    def _finish_unsuccessful_phase(
        self,
        phase: str,
        reason: str,
        status: str,
        outputs: Sequence[Path],
    ) -> dict[str, object]:
        manifest = self._read()
        self._require_known_phase(phase)
        phases = _phases(manifest)
        record = _phase_record(phases, phase)
        if record.get("status") != "running":
            raise ValueError(f"{phase} is not running")
        record.update(
            {
                "completed_at": _timestamp(),
                "failure_reason": reason,
                "outputs": sorted(
                    (_artifact_record(path, self.root_dir) for path in outputs),
                    key=lambda item: item["path"],
                ),
                "status": status,
            }
        )
        manifest["highest_completed_phase"] = _highest_completed_phase(phases)
        manifest["status"] = _manifest_status(phases)
        manifest["updated_at"] = _timestamp()
        self._write(manifest)
        return manifest

    def _input_fingerprint(self, manifest: dict[str, object], phase: str) -> str:
        payload = {
            "arguments": self.config.identity_arguments(),
            "execution_plan": self.config.execution_plan(),
            "git_revision": self.config.git_revision,
            "provider_fingerprint": self.config.provider_fingerprint,
            "predecessor_output_hashes": self._predecessor_output_hashes(manifest, phase),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _predecessor_output_hashes(
        self,
        manifest: dict[str, object],
        phase: str,
    ) -> dict[str, list[dict[str, str | None]]]:
        phases = _phases(manifest)
        predecessor_hashes: dict[str, list[dict[str, str | None]]] = {}
        for predecessor in PHASES[: PHASES.index(phase)]:
            outputs = _phase_record(phases, predecessor).get("outputs")
            records: list[dict[str, str | None]] = []
            if isinstance(outputs, list):
                for artifact in outputs:
                    if not isinstance(artifact, dict):
                        records.append({"path": None, "sha256": None})
                        continue
                    path_text = artifact.get("path")
                    if not isinstance(path_text, str):
                        records.append({"path": None, "sha256": None})
                        continue
                    path = _path_within_root(self.root_dir, path_text)
                    records.append(
                        {
                            "path": path_text,
                            "sha256": _sha256(path) if path.is_file() else None,
                        }
                    )
            predecessor_hashes[predecessor] = records
        return predecessor_hashes

    def _artifact_matches(self, artifact: object) -> bool:
        if not isinstance(artifact, dict):
            return False
        path_text = artifact.get("path")
        expected_hash = artifact.get("sha256")
        expected_size = artifact.get("size_bytes")
        if artifact.get("exists") is not True or not isinstance(path_text, str):
            return False
        if not isinstance(expected_hash, str) or not isinstance(expected_size, int):
            return False
        try:
            path = _path_within_root(self.root_dir, path_text)
        except ValueError:
            return False
        return path.is_file() and path.stat().st_size == expected_size and _sha256(path) == expected_hash

    def _read(self) -> dict[str, object]:
        payload = json.loads(self.manifest_path.read_text())
        if not isinstance(payload, dict):
            raise ValueError("production manifest must be a JSON object")
        self._validate_manifest(payload)
        return payload

    def _write(self, manifest: dict[str, object]) -> None:
        temporary_path = self.manifest_path.with_name(f"{self.manifest_path.name}.tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.manifest_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _require_known_phase(phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown production phase: {phase}")

    def _require_predecessors_reusable(self, manifest: dict[str, object], phase: str) -> None:
        for predecessor in PHASES[: PHASES.index(phase)]:
            if not self._is_reusable_phase(manifest, predecessor):
                raise ValueError(f"{predecessor} is not reusable before {phase}")

    @staticmethod
    def _reset_phase(record: dict[str, object]) -> None:
        record.update(
            {
                "completed_at": None,
                "failure_reason": None,
                "input_fingerprint": None,
                "outputs": [],
                "started_at": None,
                "status": "pending",
            }
        )

    def _reset_phases_from(self, phases: dict[str, object], phase: str) -> None:
        for reset_phase in PHASES[PHASES.index(phase) :]:
            self._reset_phase(_phase_record(phases, reset_phase))

    def _validate_manifest(self, manifest: dict[str, object]) -> None:
        schema_version = manifest.get("schema_version")
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {manifest.get('schema_version')!r}")
        if manifest.get("run_id") != self.config.run_id:
            raise ValueError("manifest run_id does not match the configured run")
        if not isinstance(manifest.get("arguments"), dict):
            raise ValueError("manifest arguments must be an object")
        if not isinstance(manifest.get("git_revision"), str):
            raise ValueError("manifest git_revision must be a string")
        if not isinstance(manifest.get("provider_fingerprint"), str):
            raise ValueError("manifest provider_fingerprint must be a string")
        if not isinstance(manifest.get("execution_plan"), dict):
            raise ValueError("manifest execution_plan must be an object")
        if not isinstance(manifest.get("created_at"), str) or not isinstance(manifest.get("updated_at"), str):
            raise ValueError("manifest timestamps must be strings")
        if manifest.get("status") not in _PHASE_STATUSES:
            raise ValueError("manifest status is invalid")

        phases = _phases(manifest)
        if set(phases) != set(PHASES):
            raise ValueError("manifest phase keys are invalid")
        for phase in PHASES:
            self._validate_phase_record(phase, _phase_record(phases, phase))

        highest_completed_phase = manifest.get("highest_completed_phase")
        if highest_completed_phase is not None and highest_completed_phase not in PHASES:
            raise ValueError("manifest highest_completed_phase is invalid")

    @staticmethod
    def _validate_phase_record(phase: str, record: dict[str, object]) -> None:
        if set(record) != _PHASE_RECORD_KEYS:
            raise ValueError(f"manifest {phase} phase record is malformed")
        if record.get("status") not in _PHASE_STATUSES:
            raise ValueError(f"manifest {phase} phase status is invalid")
        for timestamp_key in ("started_at", "completed_at"):
            timestamp = record.get(timestamp_key)
            if timestamp is not None and not isinstance(timestamp, str):
                raise ValueError(f"manifest {phase} {timestamp_key} is invalid")
        if record.get("failure_reason") is not None and not isinstance(record.get("failure_reason"), str):
            raise ValueError(f"manifest {phase} failure_reason is invalid")
        if record.get("input_fingerprint") is not None and not isinstance(record.get("input_fingerprint"), str):
            raise ValueError(f"manifest {phase} input_fingerprint is invalid")
        outputs = record.get("outputs")
        if not isinstance(outputs, list):
            raise ValueError(f"manifest {phase} outputs must be a list")
        for artifact in outputs:
            ProductionManifestStore._validate_artifact_record(artifact)

    @staticmethod
    def _validate_artifact_record(artifact: object) -> None:
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
            raise ValueError("manifest artifact record is malformed")
        exists = artifact.get("exists")
        path = artifact.get("path")
        sha256 = artifact.get("sha256")
        size_bytes = artifact.get("size_bytes")
        if not isinstance(exists, bool) or not isinstance(path, str):
            raise ValueError("manifest artifact record is malformed")
        if exists:
            if not isinstance(sha256, str) or not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
                raise ValueError("manifest artifact record is malformed")
            return
        if sha256 is not None or size_bytes is not None:
            raise ValueError("manifest artifact record is malformed")


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(run_dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"production run is already locked: {run_dir}") from error
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _phases(manifest: dict[str, object]) -> dict[str, object]:
    phases = manifest.get("phases")
    if not isinstance(phases, dict):
        raise ValueError("production manifest has no phase records")
    return phases


def _phase_record(phases: dict[str, object], phase: str) -> dict[str, object]:
    record = phases.get(phase)
    if not isinstance(record, dict):
        raise ValueError(f"production manifest has no {phase} phase record")
    return record


def _highest_completed_phase(phases: dict[str, object]) -> str | None:
    completed = [phase for phase in PHASES if _phase_record(phases, phase).get("status") == "passed"]
    return completed[-1] if completed else None


def _manifest_status(phases: dict[str, object]) -> str:
    statuses = [_phase_record(phases, phase).get("status") for phase in PHASES]
    if "blocked" in statuses:
        return "blocked"
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    return "passed" if all(status == "passed" for status in statuses) else "pending"


def _artifact_record(path: Path, root_dir: Path) -> dict[str, str | int | bool | None]:
    resolved_path = path.resolve()
    try:
        relative_path = resolved_path.relative_to(root_dir)
    except ValueError as error:
        raise ValueError(f"artifact path is outside root directory: {path}") from error
    if not resolved_path.is_file():
        return {
            "exists": False,
            "path": relative_path.as_posix(),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "exists": True,
        "path": relative_path.as_posix(),
        "sha256": _sha256(resolved_path),
        "size_bytes": resolved_path.stat().st_size,
    }


def _path_within_root(root_dir: Path, path_text: str) -> Path:
    candidate = (root_dir / path_text).resolve()
    try:
        candidate.relative_to(root_dir)
    except ValueError as error:
        raise ValueError(f"artifact path is outside root directory: {path_text}") from error
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

