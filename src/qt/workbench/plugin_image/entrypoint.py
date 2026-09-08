"""Container-only plugin bootstrap; no network and no dependency installation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pandas as pd
from btc_backtest.strategies.base import Strategy

from qt.nautilus.executor import (
    NautilusResearchExecutor,
    _assumptions,
    _execution_window,
    _initializer,
    _instrument,
    _result_payload,
    _run_id,
)
from qt.nautilus.runner import NautilusDataFrameRunner

INPUT = Path("/input")
OUTPUT = Path("/output")
DATASET_PATH = Path("/data/ohlcv")
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def main() -> None:
    control = _object(json.loads((INPUT / "control.json").read_text(encoding="utf-8")))
    source = (INPUT / "source.py").read_bytes()
    if hashlib.sha256(source).hexdigest() != _text(control, "source_sha256"):
        raise ValueError("plugin source fingerprint does not match control manifest")
    strategy = _load_plugin(INPUT / "source.py", _object(control.get("parameter_overrides", {})))
    experiment = _object(control.get("experiment", {}))
    dataset = _object(control.get("dataset", {}))
    key = _text(dataset, "key")
    frame = _read_dataset(key, dataset)
    frame, decision_start = _execution_window(frame, experiment)
    run_spec = _plugin_run_spec(control, experiment)
    assumptions = _assumptions(run_spec)
    initialize = _initializer(strategy, run_spec, dataset, frame, assumptions)
    native = NautilusDataFrameRunner().run(
        frame=frame,
        strategy=strategy,
        assumptions=assumptions,
        instrument=_instrument(dataset, run_spec),
        initialize=initialize,
        decision_start=decision_start,
    )
    run_id = _run_id(run_spec, dataset, native.engine_version)
    executor = NautilusResearchExecutor(Path("/data"), OUTPUT / "artifacts")
    summary = executor._write_artifacts(run_id, native, strategy.metadata.id, assumptions)
    result = _result_payload(summary, native, dataset, run_spec)
    result["plugin"] = {
        "source_sha256": _text(control, "source_sha256"),
        "strategy_version_id": _text(control, "strategy_version_id"),
        "decision_start": decision_start.isoformat() if decision_start is not None else None,
        "temporal_integrity": "unverified",
        "verified_leaderboard_eligible": False,
        "metrics_integrity": "unverified_plugin_origin",
    }
    _write_json(OUTPUT / "result.json", result)
    environment = _object(control.get("environment", {}))
    _write_json(
        OUTPUT / "environment-manifest.json",
        {
            "image": _text(environment, "image"),
            "image_digest": _text(environment, "image_digest"),
            "imports": environment.get("imports", []),
            "source_sha256": _text(control, "source_sha256"),
            "dataset_fingerprint": dataset.get("fingerprint"),
            "experiment_sha256": _sha256_json(experiment),
            "parameter_overrides_sha256": _sha256_json(_object(control.get("parameter_overrides", {}))),
            "temporal_integrity": "unverified",
            "verified_leaderboard_eligible": False,
            "container_isolation": _container_isolation(),
        },
    )
    if _directory_size(OUTPUT) > MAX_OUTPUT_BYTES:
        raise RuntimeError("plugin output exceeded its 64 MiB limit")


def _load_plugin(path: Path, parameters: Mapping[str, object]) -> Strategy:
    spec = importlib.util.spec_from_file_location("submitted_plugin", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load plugin source")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build = getattr(module, "build_strategy", None)
    if not callable(build):
        raise ValueError("plugin must define build_strategy(parameters)")
    candidate = build(dict(parameters))
    if not isinstance(candidate, Strategy):
        raise ValueError("build_strategy must return a btc_backtest Strategy implementation")
    return cast(Strategy, candidate)


def _read_dataset(key: str, dataset: Mapping[str, object]) -> pd.DataFrame:
    path = DATASET_PATH / f"{key}.parquet"
    expected = _text(dataset, "fingerprint")
    actual = _file_sha256(path)
    if actual != expected:
        raise ValueError("mounted dataset fingerprint does not match immutable control manifest")
    frame = pd.read_parquet(path).sort_index()
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("research parquet requires a DatetimeIndex")
    frame.index = (
        frame.index.tz_localize("UTC")
        if frame.index.tz is None
        else frame.index.tz_convert("UTC")
    )
    return frame


def _plugin_run_spec(
    control: Mapping[str, object],
    experiment: Mapping[str, object],
) -> dict[str, object]:
    """Make native artifact identity include immutable code, version, and parameters."""

    spec = dict(experiment)
    spec["strategy_params"] = _object(control.get("parameter_overrides", {}))
    spec["plugin_source_sha256"] = _text(control, "source_sha256")
    spec["plugin_strategy_version_id"] = _text(control, "strategy_version_id")
    return spec


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(value), sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _directory_size(path: Path) -> int:
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("plugin control values must be objects")
    return cast(dict[str, object], value)


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} is required")
    return item


def _sha256_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(value), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _container_isolation() -> dict[str, object]:
    """Record observed container controls; this is evidence, not a policy source."""

    root_probe = Path("/.qt-plugin-rootfs-write-check")
    rootfs_writable = False
    try:
        root_probe.write_text("probe", encoding="utf-8")
        rootfs_writable = True
    except OSError:
        pass
    finally:
        if rootfs_writable:
            root_probe.unlink(missing_ok=True)
    return {
        "effective_uid": os.geteuid(),
        "effective_gid": os.getegid(),
        "rootfs_writable": rootfs_writable,
        "docker_socket_present": Path("/var/run/docker.sock").exists(),
        "network_interfaces": sorted(name for _index, name in socket.if_nameindex()),
        "cgroup_limits": {
            "memory_max": _read_cgroup_limit("memory.max"),
            "pids_max": _read_cgroup_limit("pids.max"),
            "cpu_max": _read_cgroup_limit("cpu.max"),
        },
    }


def _read_cgroup_limit(name: str) -> str | None:
    try:
        return (Path("/sys/fs/cgroup") / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None


if __name__ == "__main__":
    main()
