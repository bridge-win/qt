"""Atomic immutable artifact bundles for backtest runs."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from btc_backtest import __version__
from btc_backtest.data.models import SHA256_PATTERN
from btc_backtest.engine.models import BacktestResult
from btc_backtest.reporting.metrics import PerformanceMetrics
from btc_backtest.validation.models import ValidationResult

SCHEMA_VERSION = "1"
RUN_JSON = "run.json"
EXPECTED_FILES = (
    RUN_JSON,
    "data_manifest.json",
    "equity.parquet",
    "positions.parquet",
    "orders.parquet",
    "fills.parquet",
    "trades.parquet",
    "signals.parquet",
    "metrics.json",
    "validation.json",
    "report.html",
)


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    engine_run_id: str = Field(min_length=1)
    created_at: datetime
    package_version: str
    git_revision: str | None = None
    strategy_id: str = Field(min_length=1)
    strategy_parameters: dict[str, object] = Field(default_factory=dict)
    seed: int | None = None
    intrabar_policy: str | None = None
    data_fingerprint: str = Field(pattern=SHA256_PATTERN)
    signal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    costs: dict[str, str] = Field(default_factory=dict)
    diagnostics: dict[str, object] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    files: dict[str, str]

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("run manifest timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("files")
    @classmethod
    def validate_file_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            for fingerprint in value.values()
        ):
            raise ValueError("artifact file fingerprints must be sha256")
        return dict(value)


class ArtifactBundle(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_dir: Path
    manifest: RunManifest


class ArtifactWriter:
    """Write complete run artifacts through an atomic directory rename."""

    def write(
        self,
        result: BacktestResult,
        metrics: PerformanceMetrics,
        validation: ValidationResult,
        root: Path,
    ) -> ArtifactBundle:
        root.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        run_id = _artifact_run_id(result, created_at)
        target = _unique_target(root, run_id)
        with TemporaryDirectory(dir=root, prefix=".artifact-") as temporary:
            staging = Path(temporary) / "bundle"
            staging.mkdir()
            _write_payload_files(staging, result, metrics, validation)
            partial_manifest = RunManifest(
                run_id=target.name,
                engine_run_id=result.run_id,
                created_at=created_at,
                package_version=__version__,
                git_revision=_git_revision(),
                strategy_id=result.strategy_id,
                strategy_parameters=_strategy_parameters(result),
                seed=_int_diagnostic(result.diagnostics, "seed"),
                intrabar_policy=_string_diagnostic(
                    result.diagnostics,
                    "intrabar_policy",
                ),
                data_fingerprint=_data_fingerprint(result),
                signal_fingerprint=_signal_fingerprint(result),
                costs=_costs(metrics),
                diagnostics=_json_safe_mapping(result.diagnostics),
                warnings=result.warnings,
                files=_file_hashes(staging),
            )
            bundle = ArtifactBundle(run_dir=staging, manifest=partial_manifest)
            from btc_backtest.reporting.html import render_html

            (staging / "report.html").write_text(
                render_html(bundle),
                encoding="utf-8",
            )
            manifest = partial_manifest.model_copy(
                update={"files": _file_hashes(staging)}
            )
            (staging / RUN_JSON).write_text(
                manifest.model_dump_json(indent=2),
                encoding="utf-8",
            )
            _verify_expected_files(staging)
            try:
                staging.replace(target)
            except OSError as exc:
                if target.exists():
                    raise FileExistsError(
                        f"artifact run directory already exists: {target}"
                    ) from exc
                raise
        return ArtifactBundle(run_dir=target, manifest=manifest)


def _write_payload_files(
    run_dir: Path,
    result: BacktestResult,
    metrics: PerformanceMetrics,
    validation: ValidationResult,
) -> None:
    (run_dir / "data_manifest.json").write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in result.data_manifests],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        metrics.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (run_dir / "validation.json").write_text(
        validation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    _equity_frame(result).to_parquet(run_dir / "equity.parquet", index=False)
    _positions_frame(result).to_parquet(
        run_dir / "positions.parquet",
        index=False,
    )
    _model_frame(result.orders).to_parquet(run_dir / "orders.parquet", index=False)
    _model_frame(result.fills).to_parquet(run_dir / "fills.parquet", index=False)
    _model_frame(result.trades).to_parquet(run_dir / "trades.parquet", index=False)
    _signals_frame(result).to_parquet(run_dir / "signals.parquet", index=False)


def _equity_frame(result: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": item.timestamp,
                "cash": str(item.cash),
                "equity": str(item.equity),
                "realized_pnl": str(item.realized_pnl),
                "unrealized_pnl": str(item.unrealized_pnl),
            }
            for item in result.snapshots
        ],
        columns=(
            "timestamp",
            "cash",
            "equity",
            "realized_pnl",
            "unrealized_pnl",
        ),
    )


def _positions_frame(result: BacktestResult) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for snapshot in result.snapshots:
        for position in snapshot.positions:
            rows.append(
                {
                    "timestamp": snapshot.timestamp,
                    "instrument": position.instrument.value,
                    "quantity": str(position.quantity),
                    "average_price": str(position.average_price),
                    "realized_pnl": str(position.realized_pnl),
                    "fees_paid": str(position.fees_paid),
                    "funding_pnl": str(position.funding_pnl),
                }
            )
    return pd.DataFrame(
        rows,
        columns=(
            "timestamp",
            "instrument",
            "quantity",
            "average_price",
            "realized_pnl",
            "fees_paid",
            "funding_pnl",
        ),
    )


def _model_frame(items: Sequence[BaseModel]) -> pd.DataFrame:
    rows = [item.model_dump(mode="json") for item in items]
    return pd.DataFrame(rows)


def _signals_frame(result: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame(
        [{"signal_id": signal_id} for signal_id in result.signal_ids],
        columns=("signal_id",),
    )


def _artifact_run_id(result: BacktestResult, created_at: datetime) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "created_at": created_at.isoformat(),
                "engine_run_id": result.run_id,
                "strategy_id": result.strategy_id,
                "data_fingerprint": _data_fingerprint(result),
                "signal_fingerprint": _signal_fingerprint(result),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{digest[:16]}"


def _unique_target(root: Path, run_id: str) -> Path:
    candidate = root / run_id
    if not candidate.exists():
        return candidate
    suffix = uuid.uuid4().hex[:8]
    return root / f"{run_id}-{suffix}"


def _git_revision() -> str | None:
    for directory in Path(__file__).resolve().parents:
        if not (directory / ".git").exists():
            continue
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
        return None
    return None


def _strategy_parameters(result: BacktestResult) -> dict[str, object]:
    value = result.diagnostics.get("strategy_parameters")
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    return {}


def _int_diagnostic(diagnostics: Mapping[str, object], key: str) -> int | None:
    value = diagnostics.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _string_diagnostic(diagnostics: Mapping[str, object], key: str) -> str | None:
    value = diagnostics.get(key)
    if isinstance(value, str):
        return value
    return None


def _costs(metrics: PerformanceMetrics) -> dict[str, str]:
    return {
        "total_fees": str(metrics.total_fees),
        "total_slippage": str(metrics.total_slippage),
        "total_funding": str(metrics.total_funding),
    }


def _json_safe_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    encoded = json.dumps(dict(mapping), default=str, sort_keys=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        return {}
    return cast(dict[str, object], decoded)


def _data_fingerprint(result: BacktestResult) -> str:
    if len(result.data_manifests) == 1:
        return result.data_manifests[0].normalized_sha256
    payload = tuple(item.normalized_sha256 for item in result.data_manifests)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _signal_fingerprint(result: BacktestResult) -> str:
    return hashlib.sha256(
        json.dumps(
            tuple(result.signal_ids),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_hashes(run_dir: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.iterdir())
        if path.is_file() and path.name != RUN_JSON
    }


def _verify_expected_files(run_dir: Path) -> None:
    present = {path.name for path in run_dir.iterdir()}
    missing = set(EXPECTED_FILES) - present
    extra = present - set(EXPECTED_FILES)
    if missing or extra:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise ValueError(
            f"artifact bundle file mismatch missing={sorted(missing)} "
            f"extra={sorted(extra)}"
        )
