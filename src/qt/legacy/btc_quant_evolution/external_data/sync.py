# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from qt.legacy.btc_quant_evolution.ohlcv_integrity import timeframe_to_seconds

from .providers import all_providers
from .registry import select_providers
from .schemas import (
    CredentialStatus,
    ExternalDataProvider,
    FeatureDomain,
    NormalizedRow,
    ProviderCapability,
    ProviderHttpError,
    ProviderPlanItem,
    ProviderRateLimitError,
    ProviderSelectionItem,
    ProviderSyncRequest,
)
from .storage import (
    ExternalDataArtifact,
    write_jsonl_rows,
    write_parquet_rows,
    write_provider_manifest,
)

_REQUIRED_DOMAINS: tuple[FeatureDomain, ...] = ("derivatives", "onchain", "sentiment")
_ALL_DOMAINS: tuple[FeatureDomain, ...] = ("liquidity", *_REQUIRED_DOMAINS)
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("retry max_attempts must be positive")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")


@dataclass(frozen=True)
class _WrittenArtifacts:
    raw: ExternalDataArtifact
    parquet: ExternalDataArtifact
    provider_manifest: Path


def sync_external_data(
    root_dir: Path,
    request: ProviderSyncRequest,
    run_id: str,
    env: Mapping[str, str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    retry_policy: RetryPolicy = RetryPolicy(),
) -> Path:
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("external data run_id must be a path-safe identifier")
    attempt_id = uuid.uuid4().hex
    run_scope = f"{run_id}/{attempt_id}"
    manifest_dir = root_dir / "user_data" / "external_data" / "manifests" / run_scope
    providers = all_providers()
    selection = select_providers(env, request.source_policy, providers=providers)
    selection_by_provider = {item.provider: item for item in selection.items}
    domain_outcomes = _initial_domain_outcomes(selection.items)
    dataset_results: list[dict[str, object]] = []
    paid_required_failures: list[str] = []

    for provider in providers:
        selection_item = selection_by_provider[provider.name]
        capabilities = tuple(provider.capabilities())
        try:
            plan_items = provider.plan_sync(request)
        except Exception as exc:
            plan_items = []
            planning_error = _redact_environment_values(str(exc), env) or "provider planning failed"
        else:
            planning_error = None
        if (
            request.source_policy == "paid-required"
            and selection_item.selected
            and selection_item.tier == "paid"
            and not plan_items
        ):
            paid_required_failures.append(f"{provider.name}: no plan items")
        if planning_error is not None and selection_item.selected:
            for capability in capabilities:
                item = _synthetic_plan_item(provider.name, capability.dataset, request)
                coverage = _coverage(item, [], required_fields=capability.required_fields)
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    [],
                    "failed",
                    planning_error,
                    run_scope,
                    coverage=coverage,
                )
                if capability.domain is None:
                    raise ValueError(f"provider capability has no domain: {provider.name}/{capability.dataset}")
                domain_outcomes[capability.domain][provider.name].append(False)
                dataset_results.append(
                    _dataset_result(root_dir, item, capability.domain, "failed", artifact, coverage)
                )
            continue

        planned_datasets = {item.dataset for item in plan_items}
        if selection_item.selected:
            omitted_reasons = _omitted_dataset_reasons(provider, request)
            for capability in capabilities:
                if capability.dataset in planned_datasets:
                    continue
                item = _synthetic_plan_item(provider.name, capability.dataset, request)
                reason = omitted_reasons.get(
                    capability.dataset,
                    "provider did not support the requested range for this dataset",
                )
                coverage = _coverage(item, [], required_fields=capability.required_fields)
                coverage["reason"] = reason
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    [],
                    "unavailable",
                    reason,
                    run_scope,
                    coverage=coverage,
                )
                if capability.domain is None:
                    raise ValueError(f"provider capability has no domain: {provider.name}/{capability.dataset}")
                domain_outcomes[capability.domain][provider.name].append(False)
                dataset_results.append(
                    _dataset_result(
                        root_dir,
                        item,
                        capability.domain,
                        "unavailable",
                        artifact,
                        coverage,
                    )
                )
        for item in plan_items:
            capability = _capability_for_item(provider, item)
            if capability.domain is None:
                raise ValueError(f"provider plan has no declared domain: {provider.name}/{item.dataset}")
            domain = capability.domain
            required_fields = capability.required_fields
            if not selection_item.selected:
                status = _skip_status(selection_item)
                coverage = _coverage(item, [], required_fields=required_fields)
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    [],
                    status,
                    selection_item.reason,
                    run_scope,
                    coverage=coverage,
                )
                dataset_results.append(_dataset_result(root_dir, item, domain, status, artifact, coverage))
                continue
            try:
                records = _fetch_with_retry(
                    provider,
                    item,
                    sleep=sleep,
                    retry_policy=retry_policy,
                )
                rows = provider.normalize(item, records)
            except ProviderRateLimitError as exc:
                status = "rate_limited"
                coverage = _coverage(item, [], required_fields=required_fields)
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    [],
                    status,
                    _redact_environment_values(str(exc), env),
                    run_scope,
                    coverage=coverage,
                )
            except ProviderHttpError as exc:
                status = "failed"
                coverage = _coverage(item, [], required_fields=required_fields)
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    [],
                    status,
                    _redact_environment_values(str(exc), env),
                    run_scope,
                    coverage=coverage,
                )
            except Exception as exc:
                status = "failed"
                coverage = _coverage(item, [], required_fields=required_fields)
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    [],
                    status,
                    _redact_environment_values(str(exc), env),
                    run_scope,
                    coverage=coverage,
                )
            else:
                coverage = _coverage(
                    item,
                    rows,
                    required_fields=required_fields,
                    max_missing_fraction=capability.max_missing_fraction,
                    max_consecutive_missing_intervals=capability.max_consecutive_missing_intervals,
                )
                status = "enabled" if coverage["accepted"] is True else "failed"
                message = None if status == "enabled" else str(coverage["reason"])
                artifact = _write_artifacts(
                    root_dir,
                    request.timeframe,
                    item,
                    rows,
                    status,
                    message,
                    run_scope,
                    coverage=coverage,
                )

            domain_outcomes[domain][provider.name].append(status == "enabled")
            dataset_results.append(_dataset_result(root_dir, item, domain, status, artifact, coverage))
            if request.source_policy == "paid-required" and selection_item.tier == "paid" and status != "enabled":
                paid_required_failures.append(f"{item.provider}/{item.dataset}: provider failed")

    domains = _domain_summary(domain_outcomes)
    failed_domains = [domain for domain in _REQUIRED_DOMAINS if domains[domain]["status"] == "failed"]
    failed_reasons = [f"required domain failed: {domain}" for domain in failed_domains]
    failed_reasons.extend(paid_required_failures)
    failed_reasons.extend(selection.errors)
    summary_path = manifest_dir / "sync-summary.json"
    _write_json_atomic(
        summary_path,
        {
            "attempt_id": attempt_id,
            "datasets": dataset_results,
            "degraded_domains": [
                domain for domain in _ALL_DOMAINS if domains[domain]["status"] == "degraded"
            ],
            "domains": domains,
            "failed_domains": failed_domains,
            "failed_reasons": failed_reasons,
            "paid_required_failures": paid_required_failures,
            "passed": not failed_domains and not paid_required_failures and not selection.errors,
            "providers": {
                "selected": [_selection_payload(item) for item in selection.items if item.selected],
                "skipped": [_selection_payload(item) for item in selection.items if not item.selected],
            },
            "requested": {
                "end": request.end.isoformat(),
                "start": request.start.isoformat(),
                "symbol": request.symbol,
                "timeframe": request.timeframe,
            },
            "run_id": run_id,
            "schema_version": 2,
            "selection_errors": list(selection.errors),
            "source_policy": request.source_policy,
            "successful_domains": [
                domain for domain in _ALL_DOMAINS if domains[domain]["status"] == "passed"
            ],
        },
    )
    return manifest_dir


def _initial_domain_outcomes(
    selection_items: Sequence[ProviderSelectionItem],
) -> dict[FeatureDomain, dict[str, list[bool]]]:
    outcomes: dict[FeatureDomain, dict[str, list[bool]]] = {domain: {} for domain in _ALL_DOMAINS}
    for selection_item in selection_items:
        if not selection_item.selected:
            continue
        for domain in selection_item.domains:
            outcomes[domain][selection_item.provider] = []
    return outcomes


def _capability_for_item(
    provider: ExternalDataProvider,
    item: ProviderPlanItem,
) -> ProviderCapability:
    capabilities = tuple(provider.capabilities())
    matching = [capability for capability in capabilities if capability.dataset == item.dataset]
    capability = matching[0] if len(matching) == 1 else capabilities[0] if len(capabilities) == 1 else None
    if capability is None:
        raise ValueError(f"provider plan does not map to a declared domain: {provider.name}/{item.dataset}")
    return capability


def _synthetic_plan_item(
    provider: str,
    dataset: str,
    request: ProviderSyncRequest,
) -> ProviderPlanItem:
    return ProviderPlanItem(
        provider=provider,
        dataset=dataset,
        symbol=request.symbol,
        start=request.start_as_datetime(),
        end=request.end_as_datetime(),
        params={},
    )


def _omitted_dataset_reasons(
    provider: ExternalDataProvider,
    request: ProviderSyncRequest,
) -> dict[str, str]:
    method = getattr(provider, "unsupported_datasets", None)
    if not callable(method):
        return {}
    reasons = method(request)
    if not isinstance(reasons, Mapping):
        raise ValueError(f"provider returned invalid unsupported dataset reasons: {provider.name}")
    return {
        str(dataset): str(reason)
        for dataset, reason in reasons.items()
        if isinstance(dataset, str) and isinstance(reason, str)
    }


def _skip_status(item: ProviderSelectionItem) -> CredentialStatus:
    if item.credential_status == "missing_credentials":
        return "missing_credentials"
    return "unavailable"


def _domain_summary(
    outcomes: Mapping[FeatureDomain, Mapping[str, Sequence[bool]]],
) -> dict[FeatureDomain, dict[str, object]]:
    summary: dict[FeatureDomain, dict[str, object]] = {}
    for domain in _ALL_DOMAINS:
        provider_outcomes = outcomes[domain]
        successful = sorted(provider for provider, results in provider_outcomes.items() if any(results))
        failed = sorted(provider for provider, results in provider_outcomes.items() if not results or not all(results))
        dataset_outcomes = [result for results in provider_outcomes.values() for result in results]
        if dataset_outcomes and all(dataset_outcomes):
            status = "passed"
        elif any(dataset_outcomes):
            status = "degraded"
        else:
            status = "failed"
        summary[domain] = {
            "failed_providers": failed,
            "selected_providers": sorted(provider_outcomes),
            "status": status,
            "successful_providers": successful,
        }
    return summary


def _dataset_result(
    root_dir: Path,
    item: ProviderPlanItem,
    domain: FeatureDomain,
    status: CredentialStatus,
    artifact: _WrittenArtifacts,
    coverage: dict[str, object],
) -> dict[str, object]:
    return {
        "artifact": _external_artifact_record(root_dir, artifact.raw),
        "artifacts": {
            "parquet": _external_artifact_record(root_dir, artifact.parquet),
            "provider_manifest": _file_artifact_record(root_dir, artifact.provider_manifest),
            "raw": _external_artifact_record(root_dir, artifact.raw),
        },
        "coverage": coverage,
        "dataset": item.dataset,
        "domain": domain,
        "provider": item.provider,
        "status": status,
    }


def _selection_payload(item: ProviderSelectionItem) -> dict[str, object]:
    return {
        "credential_status": item.credential_status,
        "domains": list(item.domains),
        "provider": item.provider,
        "reason": item.reason,
        "selected": item.selected,
        "tier": item.tier,
    }


def _redact_environment_values(message: str, env: Mapping[str, str]) -> str:
    values = {value for value in env.values() if value}
    for value in sorted(values, key=len, reverse=True):
        message = message.replace(value, "<redacted>")
    return message


def _write_artifacts(
    root_dir: Path,
    timeframe: str,
    item: ProviderPlanItem,
    rows: Sequence[NormalizedRow],
    status: CredentialStatus,
    message: str | None,
    run_id: str,
    coverage: dict[str, object] | None = None,
) -> _WrittenArtifacts:
    raw = write_jsonl_rows(
        root_dir,
        rows,
        provider=item.provider,
        dataset=item.dataset,
        symbol=item.symbol,
        run_id=run_id,
    )
    parquet = write_parquet_rows(
        root_dir,
        rows,
        provider=item.provider,
        dataset=item.dataset,
        symbol=item.symbol,
        timeframe=timeframe,
        run_id=run_id,
    )
    provider_manifest = write_provider_manifest(
        root_dir,
        run_id=run_id,
        artifact=raw,
        status=status,
        message=message,
        coverage=coverage or _coverage(item, rows),
        normalized_artifact=parquet,
    )
    return _WrittenArtifacts(raw, parquet, provider_manifest)


def _coverage(
    item: ProviderPlanItem,
    rows: Sequence[NormalizedRow],
    *,
    required_fields: Sequence[str] = (),
    max_missing_fraction: float = 0.0,
    max_consecutive_missing_intervals: int = 0,
) -> dict[str, object]:
    field_counts = {
        field: sum(
            1
            for row in rows
            if field in row.values and _is_non_null(row.values[field])
        )
        for field in required_fields
    }
    field_coverage = {
        field: count / len(rows) if rows else 0.0
        for field, count in field_counts.items()
    }
    missing_required_fields = sorted(
        field for field, count in field_counts.items() if count != len(rows)
    )
    if not rows:
        return {
            "accepted": False,
            "actual_available_end": None,
            "actual_start": None,
            "complete": False,
            "duplicate_row_count": 0,
            "expected_row_count": None,
            "field_non_null_counts": field_counts,
            "field_non_null_coverage": field_coverage,
            "missing_required_fields": list(required_fields),
            "missing_row_count": None,
            "missing_fraction": None,
            "max_consecutive_missing_intervals": None,
            "observed_row_count": 0,
            "reason": "provider returned no rows",
            "requested_end": item.end.isoformat(),
            "requested_start": item.start.isoformat(),
            "required_fields": list(required_fields),
            "unexpected_row_count": 0,
        }

    actual_start = min(row.timestamp for row in rows)
    actual_available_end = max(row.available_at for row in rows)
    period_text = item.params.get("period") or item.params.get("interval") or item.params.get("coverage_period")
    reason: str | None = None
    expected_row_count: int | None = None
    missing_row_count: int | None = None
    missing_fraction: float | None = None
    maximum_missing_run = 0
    duplicate_row_count = 0
    unexpected_row_count = 0
    if period_text is not None:
        period = timedelta(seconds=timeframe_to_seconds(period_text))
        expected = _expected_timestamps(item.start, item.end, period)
        expected_set = set(expected)
        observed = [_as_utc(row.timestamp) for row in rows]
        observed_in_range = [timestamp for timestamp in observed if item.start <= timestamp < item.end]
        observed_set = set(observed_in_range)
        missing = expected_set - observed_set
        duplicate_row_count = len(observed_in_range) - len(observed_set)
        unexpected_row_count = len(rows) - len(observed_in_range) + len(observed_set - expected_set)
        expected_row_count = len(expected)
        missing_row_count = len(missing)
        missing_fraction = missing_row_count / len(expected) if expected else 0.0
        maximum_missing_run = _max_consecutive_missing(expected, missing)

        if expected and expected[0] in missing:
            reason = "actual coverage does not reach requested start"
        elif expected and expected[-1] in missing:
            reason = "actual coverage does not reach requested end"
        elif duplicate_row_count:
            reason = f"duplicate intervals: {duplicate_row_count}"
        elif missing_row_count:
            reason = f"missing intervals: {missing_row_count}"
        elif unexpected_row_count:
            reason = f"unexpected intervals: {unexpected_row_count}"
    if reason is None and missing_required_fields:
        reason = "required field coverage incomplete: " + ", ".join(missing_required_fields)
    exact_complete = reason is None
    accepted = exact_complete
    if (
        reason is not None
        and missing_row_count
        and expected
        and expected[0] not in missing
        and expected[-1] not in missing
        and duplicate_row_count == 0
        and unexpected_row_count == 0
        and not missing_required_fields
        and missing_fraction is not None
        and missing_fraction <= max_missing_fraction
        and maximum_missing_run <= max_consecutive_missing_intervals
    ):
        accepted = True
        reason = (
            f"accepted sparse coverage: {missing_row_count} missing intervals; "
            f"fraction={missing_fraction:.6f}; max_consecutive={maximum_missing_run}"
        )
    return {
        "accepted": accepted,
        "actual_available_end": actual_available_end.isoformat(),
        "actual_start": actual_start.isoformat(),
        "complete": exact_complete,
        "duplicate_row_count": duplicate_row_count,
        "expected_row_count": expected_row_count,
        "field_non_null_counts": field_counts,
        "field_non_null_coverage": field_coverage,
        "missing_required_fields": missing_required_fields,
        "missing_row_count": missing_row_count,
        "missing_fraction": missing_fraction,
        "max_consecutive_missing_intervals": maximum_missing_run,
        "observed_row_count": len(rows),
        "reason": reason,
        "requested_end": item.end.isoformat(),
        "requested_start": item.start.isoformat(),
        "required_fields": list(required_fields),
        "unexpected_row_count": unexpected_row_count,
    }


def _fetch_with_retry(
    provider: ExternalDataProvider,
    item: ProviderPlanItem,
    *,
    sleep: Callable[[float], None],
    retry_policy: RetryPolicy,
) -> list[dict[str, object]]:
    for attempt in range(retry_policy.max_attempts):
        try:
            return provider.fetch(item)
        except ProviderRateLimitError as exc:
            if attempt + 1 >= retry_policy.max_attempts:
                raise
            fallback = retry_policy.base_delay_seconds * (2**attempt)
            requested = exc.retry_after_seconds if exc.retry_after_seconds is not None else fallback
            sleep(min(requested, retry_policy.max_delay_seconds))
    raise AssertionError("retry loop must return or raise")


def _external_artifact_record(
    root_dir: Path,
    artifact: ExternalDataArtifact,
) -> dict[str, object]:
    return {
        "path": artifact.path.relative_to(root_dir).as_posix(),
        "row_count": artifact.row_count,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
    }


def _file_artifact_record(root_dir: Path, path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root_dir).as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _is_non_null(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_timestamps(start: datetime, end: datetime, period: timedelta) -> list[datetime]:
    if period <= timedelta(0):
        raise ValueError("coverage period must be positive")
    timestamps: list[datetime] = []
    current = _as_utc(start)
    normalized_end = _as_utc(end)
    while current < normalized_end:
        timestamps.append(current)
        current += period
    return timestamps


def _max_consecutive_missing(expected: Sequence[datetime], missing: set[datetime]) -> int:
    maximum = 0
    current = 0
    for timestamp in expected:
        if timestamp in missing:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize external data locally for BTC research.")
    parser.add_argument("--source-policy", choices=("auto", "free-only", "paid-required"), default="auto")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    run_id = args.run_id or f"sync-{args.start.isoformat()}-{args.end.isoformat()}"
    request = ProviderSyncRequest(args.symbol, args.timeframe, args.start, args.end, args.source_policy)
    manifest_dir = sync_external_data(args.root_dir, request, run_id, os.environ)
    print(manifest_dir.as_posix())
    summary = json.loads((manifest_dir / "sync-summary.json").read_text())
    return 0 if summary.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

