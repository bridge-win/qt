# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from qt.legacy.btc_quant_evolution.evolution.candidate_space import (
    EvolutionCandidate,
    default_candidate_space,
)
from qt.legacy.btc_quant_evolution.evolution.optimizer import (
    build_evolution_request,
    write_candidate_config,
)
from qt.legacy.btc_quant_evolution.evolution.promotion_gate import (
    PromotionPolicy,
    evaluate_candidate_promotion,
)
from qt.legacy.btc_quant_evolution.evolution.scorer import rank_evolution_results
from qt.legacy.btc_quant_evolution.research_pipeline import run_research_plan


@dataclass(frozen=True)
class EvolutionSplit:
    name: Literal["train", "validate", "final_oos"]
    timerange: str

    def as_dict(self) -> dict[str, str]:
        start, end = _split_dates(self)
        return {
            "end": end.isoformat(),
            "name": self.name,
            "purpose": "candidate_selection" if self.name != "final_oos" else "promotion_gate_only",
            "start": start.isoformat(),
            "timerange": self.timerange,
        }


def default_pre_2026_splits() -> list[EvolutionSplit]:
    """Return an explicit legacy-window plan without making it an execution default."""
    return [
        EvolutionSplit("train", "20170817-20221231"),
        EvolutionSplit("validate", "20230101-20240630"),
        EvolutionSplit("final_oos", "20240701-20251231"),
    ]


def build_evolution_split_plan(start: date, end: date) -> tuple[EvolutionSplit, ...]:
    """Reserve the final two calendar years for validation and untouched final OOS."""
    if start >= end:
        raise ValueError("evolution window start must be before end")
    final_oos_start = _subtract_year(end)
    validation_start = _subtract_year(final_oos_start)
    if validation_start - start < timedelta(days=730):
        raise ValueError("evolution window requires at least two years of train data before validation")
    return (
        EvolutionSplit("train", _date_timerange(start, validation_start)),
        EvolutionSplit("validate", _date_timerange(validation_start, final_oos_start)),
        EvolutionSplit("final_oos", _date_timerange(final_oos_start, end)),
    )


def run_evolution(
    *,
    root_dir: Path,
    feature_matrix: Path,
    run_id: str,
    candidates: list[EvolutionCandidate],
    split_plan: Sequence[EvolutionSplit],
    evidence_bundle: Path | None = None,
) -> Path:
    root_dir = root_dir.resolve()
    feature_matrix_path = _feature_matrix_path(root_dir=root_dir, feature_matrix=feature_matrix)
    _validate_unique_candidate_ids(candidates)
    validated_split_plan = _validate_split_plan(split_plan)
    output_dir = root_dir / "user_data" / "research_runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_matrix_artifact = _file_artifact(root_dir=root_dir, path=feature_matrix_path)
    strategy_source_artifact = _file_artifact(
        root_dir=root_dir,
        path=root_dir / "user_data" / "strategies" / "BtcMultiSourceRegimeStrategy.py",
    )
    evidence_bundle_path = _optional_root_path(root_dir=root_dir, path=evidence_bundle)
    candidate_evidence = _load_candidate_evidence(evidence_bundle_path)
    evidence_bundle_artifact = _file_artifact(
        root_dir=root_dir,
        path=evidence_bundle_path or root_dir / "missing-candidate-evidence.json",
    )

    selection_results = []
    for candidate in candidates:
        candidate_config = write_candidate_config(
            root_dir=root_dir,
            run_id=run_id,
            candidate=candidate,
        )
        selection_results.append(
            _run_selection_candidate(
                root_dir=root_dir,
                feature_matrix=feature_matrix_path,
                candidate_config=candidate_config,
                run_id=run_id,
                candidate=candidate,
                selection_splits=validated_split_plan[:2],
                feature_matrix_artifact=feature_matrix_artifact,
                strategy_source_artifact=strategy_source_artifact,
                evidence_bundle_artifact=evidence_bundle_artifact,
            )
        )
    ranked = rank_evolution_results(selection_results)
    selected_selection = next(
        (
            candidate
            for candidate in ranked
            if isinstance(candidate.get("selection"), dict)
            and candidate["selection"].get("passed") is True
        ),
        None,
    )
    selected_candidate = None
    final_oos_evaluation_count = 0
    if selected_selection is not None:
        final_oos_evaluation_count = 1
        selected_candidate = _run_final_oos(
            root_dir=root_dir,
            feature_matrix=feature_matrix_path,
            run_id=run_id,
            selection_result=selected_selection,
            final_oos_split=validated_split_plan[2],
            candidate_evidence=candidate_evidence.get(str(selected_selection["candidate_id"])),
        )
    result_path = output_dir / "evolution_result.json"
    result_path.write_text(
        json.dumps(
            {
                "candidates": selection_results,
                "feature_matrix_artifact": feature_matrix_artifact,
                "final_oos_evaluation_count": final_oos_evaluation_count,
                "evidence_bundle_artifact": evidence_bundle_artifact,
                "run_id": run_id,
                "schema_version": 2,
                "selected_candidate": selected_candidate,
                "selection": {
                    "ranking": [str(candidate.get("candidate_id")) for candidate in ranked],
                    "selected_candidate_id": (
                        str(selected_selection.get("candidate_id"))
                        if selected_selection is not None
                        else None
                    ),
                    "split_names": ["train", "validate"],
                },
                "split_plan": [split.as_dict() for split in validated_split_plan],
                "strategy_source_artifact": strategy_source_artifact,
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    (output_dir / "evolution_ranking.json").write_text(
        json.dumps(
            {
                "ranking": [str(candidate.get("candidate_id")) for candidate in ranked],
                "selected_candidate_id": (
                    str(selected_selection.get("candidate_id"))
                    if selected_selection is not None
                    else None
                ),
                "selection_splits": [split.as_dict() for split in validated_split_plan[:2]],
            },
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    return result_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic multi-source walk-forward evolution.")
    parser.add_argument("--feature-matrix", required=True, type=Path)
    parser.add_argument("--strategy", default="BtcMultiSourceRegimeStrategy")
    parser.add_argument("--evidence-bundle", type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--run-id", default=_default_run_id())
    parser.add_argument("--root-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.strategy != "BtcMultiSourceRegimeStrategy":
        parser.error("--strategy must be BtcMultiSourceRegimeStrategy")

    result_path = run_evolution(
        root_dir=args.root_dir,
        feature_matrix=args.feature_matrix,
        run_id=args.run_id,
        candidates=default_candidate_space(),
        split_plan=build_evolution_split_plan(args.start, args.end),
        evidence_bundle=args.evidence_bundle,
    )
    print(result_path)
    result = _read_json(result_path)
    selected_candidate = result.get("selected_candidate") if isinstance(result, dict) else None
    return 0 if _promotion_passed(selected_candidate) else 1


def _run_selection_candidate(
    *,
    root_dir: Path,
    feature_matrix: Path,
    candidate_config: Path,
    run_id: str,
    candidate: EvolutionCandidate,
    selection_splits: Sequence[EvolutionSplit],
    feature_matrix_artifact: dict[str, object],
    strategy_source_artifact: dict[str, object],
    evidence_bundle_artifact: dict[str, object],
) -> dict[str, object]:
    reports: dict[str, dict[str, object]] = {}
    split_run_dirs: dict[str, str | None] = {}
    summaries: dict[str, dict[str, object] | None] = {}
    errors: list[str] = []
    for split in selection_splits:
        try:
            request = build_evolution_request(
                candidate=candidate,
                split=split,
                feature_matrix=feature_matrix,
                candidate_config=candidate_config,
                run_id=run_id,
            )
            run_dir = run_research_plan(request=request, root_dir=root_dir)
            report, split_summary, report_errors = _split_report(
                run_dir,
                require_bias=False,
            )
            reports[split.name] = report
            split_run_dirs[split.name] = str(run_dir)
            summaries[split.name] = split_summary
            errors.extend(f"{split.name}: {error}" for error in report_errors)
        except Exception as exc:
            split_run_dirs[split.name] = None
            summaries[split.name] = None
            errors.append(f"{split.name}: {exc}")

    passed = not errors and len(reports) == 2 and all(_split_passed(report) for report in reports.values())
    return {
        "candidate": asdict(candidate),
        "candidate_config_artifact": _file_artifact(root_dir=root_dir, path=candidate_config),
        "candidate_id": candidate.id,
        "errors": errors,
        "evidence_bundle_artifact": evidence_bundle_artifact,
        "feature_matrix_artifact": feature_matrix_artifact,
        "selection": {
            "errors": errors,
            "metrics": _selection_metrics(summaries),
            "passed": passed,
            "reports": reports,
            "split_run_dirs": split_run_dirs,
            "summaries": summaries,
        },
        "strategy_source_artifact": strategy_source_artifact,
    }


def _run_final_oos(
    *,
    root_dir: Path,
    feature_matrix: Path,
    run_id: str,
    selection_result: dict[str, object],
    final_oos_split: EvolutionSplit,
    candidate_evidence: dict[str, object] | None,
) -> dict[str, object]:
    candidate = EvolutionCandidate(**selection_result["candidate"])
    candidate_config_artifact = selection_result["candidate_config_artifact"]
    if not isinstance(candidate_config_artifact, dict) or not isinstance(candidate_config_artifact.get("path"), str):
        raise ValueError("selected candidate config artifact is malformed")
    candidate_config = Path(candidate_config_artifact["path"])
    selection = selection_result.get("selection")
    selection_reports = selection.get("reports") if isinstance(selection, dict) else None
    reports = dict(selection_reports) if isinstance(selection_reports, dict) else {}
    errors = list(selection.get("errors", [])) if isinstance(selection, dict) else []
    split_run_dirs = dict(selection.get("split_run_dirs", {})) if isinstance(selection, dict) else {}
    summaries = dict(selection.get("summaries", {})) if isinstance(selection, dict) else {}
    try:
        request = build_evolution_request(
            candidate=candidate,
            split=final_oos_split,
            feature_matrix=feature_matrix,
            candidate_config=candidate_config,
            run_id=run_id,
        )
        run_dir = run_research_plan(request=request, root_dir=root_dir)
        final_oos, summary, report_errors = _split_report(run_dir, require_bias=True)
        split_run_dirs["final_oos"] = str(run_dir)
        summaries["final_oos"] = summary
        errors.extend(f"final_oos: {error}" for error in report_errors)
    except Exception as exc:
        final_oos = {}
        summary = None
        split_run_dirs["final_oos"] = None
        summaries["final_oos"] = None
        errors.append(f"final_oos: {exc}")

    final_oos["walk_forward"] = {
        "passed": (
            isinstance(selection, dict)
            and selection.get("passed") is True
            and not any(error.startswith("final_oos:") for error in errors)
            and _split_passed(final_oos)
        )
    }
    for evidence_name in ("dry_run", "operational"):
        evidence = candidate_evidence.get(evidence_name) if isinstance(candidate_evidence, dict) else None
        if isinstance(evidence, dict):
            final_oos[evidence_name] = evidence
            if evidence.get("passed") is not True:
                errors.append(f"candidate: {evidence_name} evidence did not pass")
        else:
            errors.append(f"candidate: missing {evidence_name} evidence")
    reports["final_oos"] = final_oos
    promotion = evaluate_candidate_promotion(candidate.id, reports, PromotionPolicy())
    return {
        **selection_result,
        "errors": errors,
        "metrics": summary or {},
        "promotion": promotion,
        "split_run_dirs": split_run_dirs,
        "summaries": summaries,
    }


def _selection_metrics(summaries: dict[str, dict[str, object] | None]) -> dict[str, float]:
    train = summaries.get("train")
    validate = summaries.get("validate")
    available = [summary for summary in (train, validate) if isinstance(summary, dict)]
    metrics: dict[str, float] = {}
    for name in ("calmar", "profit_factor", "sortino", "total_trades"):
        values = [float(summary[name]) for summary in available if _is_number(summary.get(name))]
        if values:
            metrics[name] = min(values)
    drawdowns = [
        float(summary["max_drawdown_pct"])
        for summary in available
        if _is_number(summary.get("max_drawdown_pct"))
    ]
    if drawdowns:
        metrics["max_drawdown_pct"] = max(drawdowns)
    return metrics


def _split_report(
    run_dir: Path,
    *,
    require_bias: bool,
) -> tuple[dict[str, object], dict[str, object] | None, list[str]]:
    errors: list[str] = []
    summary = _read_report(run_dir / "summary.json", "summary", errors)
    report: dict[str, object] = {"summary": summary} if summary is not None else {}
    for name in ("robustness", "regime"):
        payload = _read_report(run_dir / f"{name}.json", name, errors)
        if payload is not None:
            report[name] = _evidence(payload, name)
    errors.extend(_split_gate_errors(report))
    if require_bias:
        bias, bias_errors = _parse_bias_evidence(run_dir)
        report["bias"] = bias
        errors.extend(bias_errors)
    return report, summary, errors


def _read_report(path: Path, name: str, errors: list[str]) -> dict[str, object] | None:
    if not path.is_file():
        errors.append(f"missing {name}.json")
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        errors.append(f"invalid {name}.json: {exc}")
        return None
    if not isinstance(payload, dict):
        errors.append(f"invalid {name}.json: expected object")
        return None
    return payload


def _evidence(payload: dict[str, object], name: str) -> dict[str, object]:
    if name == "robustness":
        gate = payload.get("robustness_gate")
    elif name == "regime":
        gate = payload.get("regime_gate")
    else:
        gate = payload
    return {"passed": isinstance(gate, dict) and gate.get("passed") is True}


def _split_gate_errors(report: dict[str, object]) -> list[str]:
    errors: list[str] = []
    summary = report.get("summary")
    production_gate = summary.get("production_gate") if isinstance(summary, dict) else None
    if not isinstance(production_gate, dict) or production_gate.get("passed") is not True:
        errors.append("summary production gate did not pass")
    for name in ("robustness", "regime"):
        evidence = report.get(name)
        if not isinstance(evidence, dict) or evidence.get("passed") is not True:
            errors.append(f"{name} gate did not pass")
    return errors


def _split_passed(report: dict[str, object]) -> bool:
    return not _split_gate_errors(report)


def _parse_bias_evidence(run_dir: Path) -> tuple[dict[str, object], list[str]]:
    lookahead_passed = _lookahead_passed(run_dir / "logs" / "lookahead_analysis.log")
    recursive_passed = _recursive_passed(run_dir / "logs" / "recursive_analysis.log")
    errors: list[str] = []
    if not lookahead_passed:
        errors.append("lookahead analysis reported bias or malformed output")
    if not recursive_passed:
        errors.append("recursive analysis reported non-zero or malformed variance")
    return {
        "lookahead_passed": lookahead_passed,
        "recursive_passed": recursive_passed,
    }, errors


def _lookahead_passed(path: Path) -> bool:
    table = _pipe_table(path, required_header="has_bias")
    if table is None:
        return False
    header, rows = table
    bias_index = header.index("has_bias")
    signals_index = header.index("total_signals") if "total_signals" in header else None
    if not rows:
        return False
    for row in rows:
        if len(row) <= bias_index or row[bias_index].strip().lower() not in {"no", "false", "0"}:
            return False
        if signals_index is not None:
            try:
                if int(row[signals_index]) <= 0:
                    return False
            except (IndexError, ValueError):
                return False
    return True


def _recursive_passed(path: Path) -> bool:
    table = _pipe_table(path, required_header="indicators")
    if table is None:
        return False
    _, rows = table
    if not rows:
        return False
    for row in rows:
        for value in row[1:]:
            normalized = value.strip()
            if normalized == "-":
                continue
            if not normalized.endswith("%"):
                return False
            try:
                if float(normalized.removesuffix("%")) != 0:
                    return False
            except ValueError:
                return False
    return True


def _pipe_table(path: Path, *, required_header: str) -> tuple[list[str], list[list[str]]] | None:
    if not path.is_file():
        return None
    lines = [_strip_ansi(line) for line in path.read_text(errors="replace").splitlines()]
    for index, line in enumerate(lines):
        header = _pipe_cells(line)
        if required_header not in header:
            continue
        rows: list[list[str]] = []
        for candidate in lines[index + 1 :]:
            cells = _pipe_cells(candidate)
            if not cells:
                if rows:
                    break
                continue
            if all(set(cell) <= {"-", "+", "="} for cell in cells):
                continue
            if len(cells) == len(header):
                rows.append(cells)
        return header, rows
    return None


def _pipe_cells(line: str) -> list[str]:
    if "|" not in line:
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def _feature_matrix_path(*, root_dir: Path, feature_matrix: Path) -> Path:
    resolved = feature_matrix.resolve() if feature_matrix.is_absolute() else (root_dir / feature_matrix).resolve()
    try:
        return resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("feature_matrix must be inside root_dir") from exc


def _optional_root_path(*, root_dir: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.resolve() if path.is_absolute() else (root_dir / path).resolve()
    try:
        resolved.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("evidence_bundle must be inside root_dir") from exc
    return resolved


def _load_candidate_evidence(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    payload = _read_json(path)
    if payload is None:
        raise ValueError(f"invalid or missing evidence bundle: {path}")
    candidates = payload.get("candidates")
    if not isinstance(candidates, dict):
        raise ValueError("evidence bundle must include a candidates object")
    result: dict[str, dict[str, object]] = {}
    for candidate_id, evidence in candidates.items():
        if not isinstance(candidate_id, str) or not candidate_id or not isinstance(evidence, dict):
            raise ValueError("evidence bundle candidates must map non-empty ids to objects")
        result[candidate_id] = evidence
    return result


def _validate_unique_candidate_ids(candidates: list[EvolutionCandidate]) -> None:
    ids = [candidate.id for candidate in candidates]
    duplicate_ids = sorted({candidate_id for candidate_id in ids if ids.count(candidate_id) > 1})
    if duplicate_ids:
        raise ValueError(f"duplicate candidate ids: {', '.join(duplicate_ids)}")


def _validate_split_plan(split_plan: Sequence[EvolutionSplit]) -> tuple[EvolutionSplit, ...]:
    splits = tuple(split_plan)
    if tuple(split.name for split in splits) != ("train", "validate", "final_oos"):
        raise ValueError("evolution split plan must contain train, validate, and final_oos in order")
    boundaries = [_split_dates(split) for split in splits]
    if any(start >= end for start, end in boundaries):
        raise ValueError("evolution split ranges must be non-empty")
    if boundaries[0][1] != boundaries[1][0] or boundaries[1][1] != boundaries[2][0]:
        raise ValueError("evolution split ranges must be contiguous and non-overlapping")
    return splits


def _split_dates(split: EvolutionSplit) -> tuple[date, date]:
    try:
        start_text, end_text = split.timerange.split("-", maxsplit=1)
        return (
            datetime.strptime(start_text, "%Y%m%d").date(),
            datetime.strptime(end_text, "%Y%m%d").date(),
        )
    except ValueError as exc:
        raise ValueError(f"invalid {split.name} timerange: {split.timerange}") from exc


def _date_timerange(start: date, end: date) -> str:
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


def _subtract_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _file_artifact(*, root_dir: Path, path: Path) -> dict[str, object]:
    absolute_path = path if path.is_absolute() else root_dir / path
    try:
        relative_path = absolute_path.relative_to(root_dir)
    except ValueError:
        relative_path = absolute_path
    if not absolute_path.is_file():
        return {
            "exists": False,
            "path": relative_path.as_posix(),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "exists": True,
        "path": relative_path.as_posix(),
        "sha256": _sha256(absolute_path),
        "size_bytes": absolute_path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else None


def _promotion_passed(candidate: object) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("promotion"), dict)
        and candidate["promotion"].get("passed") is True
    )


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())

