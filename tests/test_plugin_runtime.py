from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest

import qt.workbench.plugin_runtime as plugin_runtime
from qt.workbench.plugin_runtime import (
    IsolatedPluginRuntime,
    PluginCancelledError,
    PluginRuntimeError,
    PluginRuntimeSettings,
    _artifact_relative_paths,
    _contained,
    _prepare_task_mounts,
    _read_result,
    _rebase_artifact_descriptors,
    _remove_owned_task_dir,
    _seal_input_mount,
    _validate_output_tree,
    _write_private,
    docker_command,
    validate_plugin_source,
)

VALID_PLUGIN = """
from decimal import Decimal
from btc_backtest.strategies.base import StrategyMetadata, StrategyContext
from btc_backtest.strategies.target_weight import TargetWeightStrategy

class LongOnly(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="plugin_long_only",
        version="1",
        description="A real plugin buy-and-hold strategy.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )

    def target_weight(self, context: StrategyContext) -> Decimal:
        return Decimal("1")

def build_strategy(parameters):
    return LongOnly(parameters)
"""


def _version(source: str = VALID_PLUGIN) -> dict[str, object]:
    return {
        "version_id": "plugin-version-1",
        "content": {
            "mode": "plugin",
            "plugin_api_version": "1",
            "source_code": source,
        },
    }


def test_plugin_validation_rejects_network_environment_and_host_path_imports() -> None:
    for source, dependency in (
        ("import socket\ndef build_strategy(parameters): pass\n", "socket"),
        ("import os\ndef build_strategy(parameters): pass\n", "os"),
        ("from pathlib import Path\ndef build_strategy(parameters): pass\n", "pathlib"),
    ):
        with pytest.raises(PluginRuntimeError, match=dependency):
            validate_plugin_source(source, _version(source))


def test_plugin_validation_accepts_documented_strategy_factory() -> None:
    assert validate_plugin_source(VALID_PLUGIN, _version()) == ("btc_backtest", "decimal")


def test_docker_command_has_no_network_host_environment_or_unrelated_mounts(tmp_path: Path) -> None:
    input_dir = tmp_path / "task" / "input"
    output_dir = tmp_path / "task" / "output"
    dataset = tmp_path / "parquet" / "ohlcv" / "okx_BTCUSDT_1h.parquet"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    dataset.parent.mkdir(parents=True)
    dataset.touch()
    command = docker_command(
        image="qt-plugin-runtime:test",
        container_name="qt-plugin-test",
        input_dir=input_dir,
        output_dir=output_dir,
        dataset_path=dataset,
        dataset_target="/data/ohlcv/okx_BTCUSDT_1h.parquet",
        settings=PluginRuntimeSettings(),
    )
    rendered = " ".join(command)
    assert "--network none" in rendered
    assert "--read-only" in rendered
    assert "--cap-drop ALL" in rendered
    assert "no-new-privileges" in rendered
    assert f"--user {os.getuid()}:{os.getgid()}" in rendered
    assert "docker.sock" not in rendered
    assert "--env" not in rendered
    assert str(input_dir.resolve()) in rendered
    assert str(output_dir.resolve()) in rendered
    assert str(dataset.resolve()) in rendered
    with pytest.raises(PluginRuntimeError, match="escapes"):
        _contained(tmp_path / "parquet", tmp_path / "unrelated" / "host.parquet")


def test_task_mount_permissions_are_sealed_only_after_host_populates_inputs(tmp_path: Path) -> None:
    task_dir = tmp_path / "plugin-task-fixture"
    task_dir.mkdir(mode=0o700)

    input_dir, output_dir = _prepare_task_mounts(task_dir)
    _write_private(input_dir / "control.json", b"{}", mode=0o400)
    _write_private(input_dir / "source.py", b"source", mode=0o400)
    _seal_input_mount(input_dir)

    assert stat.S_IMODE(task_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(input_dir.stat().st_mode) == 0o500
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((input_dir / "control.json").stat().st_mode) == 0o400
    with pytest.raises(PermissionError):
        _write_private(input_dir / "late.json", b"must not be writable")
    _remove_owned_task_dir(task_dir, tmp_path)
    assert not task_dir.exists()


def test_plugin_runtime_refuses_a_root_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(plugin_runtime.os, "getuid", lambda: 0)
    with pytest.raises(PluginRuntimeError, match="refuses a root host"):
        docker_command(
            image="sha256:" + "a" * 64,
            container_name="qt-plugin-root",
            input_dir=tmp_path / "input",
            output_dir=tmp_path / "output",
            dataset_path=tmp_path / "data.parquet",
            dataset_target="/data/ohlcv/fixture.parquet",
            settings=PluginRuntimeSettings(),
        )


def test_malicious_plugin_output_symlink_is_never_read_or_published(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("must-not-be-read", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.json").symlink_to(secret)
    runtime = IsolatedPluginRuntime(
        parquet_root=tmp_path / "parquet",
        artifact_root=tmp_path / "artifacts",
    )

    with pytest.raises(PluginRuntimeError, match="symlinks"):
        _validate_output_tree(output, max_bytes=1024 * 1024)
    with pytest.raises(PluginRuntimeError, match="safe result"):
        _read_result(output / "result.json")
    with pytest.raises(PluginRuntimeError, match="symlinks"):
        runtime._publish_output(output, "safe-run")
    assert not (tmp_path / "artifacts" / "plugins" / "safe-run").exists()


def test_plugin_output_file_count_is_bounded(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    for number in range(257):
        (output / f"artifact-{number}.txt").touch()

    with pytest.raises(PluginRuntimeError, match="256 file"):
        _validate_output_tree(output, max_bytes=1024 * 1024)


def test_plugin_output_rejects_fifo_hardlink_and_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo_output = tmp_path / "fifo-output"
    fifo_output.mkdir()
    os.mkfifo(fifo_output / "blocked")
    with pytest.raises(PluginRuntimeError, match="regular files"):
        _validate_output_tree(fifo_output, max_bytes=1024 * 1024)

    secret = tmp_path / "secret.txt"
    secret.write_text("host", encoding="utf-8")
    link_output = tmp_path / "link-output"
    link_output.mkdir()
    os.link(secret, link_output / "hard-link")
    with pytest.raises(PluginRuntimeError, match="hard-linked"):
        _validate_output_tree(link_output, max_bytes=1024 * 1024)

    socket_output = tmp_path / "socket-output"
    socket_output.mkdir()
    (socket_output / "socket-marker").touch()
    actual_stat = os.stat

    def socket_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        observed = actual_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "socket-marker" and kwargs.get("dir_fd") is not None:
            fields = list(observed)
            fields[0] = stat.S_IFSOCK | 0o600
            return os.stat_result(fields)
        return observed

    monkeypatch.setattr(plugin_runtime.os, "stat", socket_stat)
    with pytest.raises(PluginRuntimeError, match="regular files"):
        _validate_output_tree(socket_output, max_bytes=1024 * 1024)


def test_plugin_publication_is_atomic_after_safe_regular_file_copy(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.json").write_text('{"run_id":"published"}', encoding="utf-8")
    (output / "environment-manifest.json").write_text("{}", encoding="utf-8")
    (output / "native.txt").write_text("artifact", encoding="utf-8")
    runtime = IsolatedPluginRuntime(
        parquet_root=tmp_path / "parquet",
        artifact_root=tmp_path / "artifacts",
    )

    published = runtime._publish_output(output, "published")

    assert published == tmp_path / "artifacts" / "plugins" / "published"
    assert (published / "native.txt").read_text(encoding="utf-8") == "artifact"
    assert not list((tmp_path / "artifacts" / "plugins").glob(".published.publish-*"))


def test_plugin_artifact_descriptors_rebase_and_reject_host_paths(tmp_path: Path) -> None:
    root = tmp_path / "copied-output"
    artifact = root / "artifacts" / "run-1" / "orders.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("timestamp,side\n2024-01-01T00:00:00Z,BUY\n", encoding="utf-8")
    result = {
        "run_id": "run-1",
        "artifacts": [
            {
                "path": "/output/artifacts/run-1/orders.csv",
                "sha256": "plugin-controlled",
                "media_type": "text/plain",
            }
        ],
    }

    descriptors = _rebase_artifact_descriptors(root, _artifact_relative_paths(result))

    assert descriptors[0]["path"] == "artifacts/run-1/orders.csv"
    assert descriptors[0]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert descriptors[0]["media_type"] == "text/csv"
    with pytest.raises(PluginRuntimeError, match="escapes"):
        _artifact_relative_paths({"run_id": "run-1", "artifacts": [{"path": "/etc/passwd"}]})


class _WaitingProcess:
    returncode: int | None = None
    terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: int | None = None) -> int:
        del timeout
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def test_cancellation_stops_only_the_named_plugin_container(tmp_path: Path) -> None:
    process = _WaitingProcess()

    def fake_popen(command: list[str], **_: object) -> _WaitingProcess:
        assert "--name" in command
        assert "qt-plugin-owned" in command
        return process

    runtime = IsolatedPluginRuntime(
        parquet_root=tmp_path / "parquet",
        artifact_root=tmp_path / "artifacts",
        popen=fake_popen,  # type: ignore[arg-type]
    )
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    dataset = tmp_path / "dataset.parquet"
    input_dir.mkdir()
    output_dir.mkdir()
    dataset.touch()
    with pytest.raises(PluginCancelledError):
        runtime._run_container(
            container_name="qt-plugin-owned",
            input_dir=input_dir,
            output_dir=output_dir,
            dataset_path=dataset,
            dataset_key="fixture",
            image_digest="sha256:" + "a" * 64,
            progress=lambda _stage, _percent: None,
            cancelled=lambda: True,
        )
    assert process.terminated is True


def test_timeout_terminates_only_the_named_plugin_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _WaitingProcess()
    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr(plugin_runtime.time, "monotonic", lambda: next(monotonic_values))
    runtime = IsolatedPluginRuntime(
        parquet_root=tmp_path / "parquet",
        artifact_root=tmp_path / "artifacts",
        settings=PluginRuntimeSettings(wall_seconds=1),
        popen=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
    )
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    dataset = tmp_path / "dataset.parquet"
    input_dir.mkdir()
    output_dir.mkdir()
    dataset.touch()

    with pytest.raises(PluginRuntimeError, match="wall-time"):
        runtime._run_container(
            container_name="qt-plugin-timeout",
            input_dir=input_dir,
            output_dir=output_dir,
            dataset_path=dataset,
            dataset_key="fixture",
            image_digest="sha256:" + "b" * 64,
            progress=lambda _stage, _percent: None,
            cancelled=lambda: False,
        )
    assert process.terminated is True


def test_entrypoint_passes_window_fingerprint_and_plugin_identity_to_native_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the container bootstrap without importing submitted code on the host."""

    entrypoint_path = Path("src/qt/workbench/plugin_image/entrypoint.py")
    spec = importlib.util.spec_from_file_location("plugin_entrypoint_test", entrypoint_path)
    assert spec is not None and spec.loader is not None
    entrypoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entrypoint)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    dataset_dir = tmp_path / "data" / "ohlcv"
    input_dir.mkdir()
    output_dir.mkdir()
    dataset_dir.mkdir(parents=True)
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": range(5),
            "high": range(1, 6),
            "low": range(5),
            "close": range(5),
            "volume": [1.0] * 5,
        },
        index=index,
    )
    frame.to_parquet(dataset_dir / "fixture.parquet")
    source = b"def build_strategy(parameters): raise AssertionError('must not execute on host')\n"
    fingerprint = _file_sha256(dataset_dir / "fixture.parquet")
    control = {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "strategy_version_id": "version-a",
        "parameter_overrides": {"threshold": 3},
        "experiment": {"from": index[2].isoformat(), "to": index[3].isoformat()},
        "dataset": {"key": "fixture", "fingerprint": fingerprint},
        "environment": {"image": "fixture", "image_digest": "sha256:" + "a" * 64},
    }
    (input_dir / "control.json").write_text(json.dumps(control), encoding="utf-8")
    (input_dir / "source.py").write_bytes(source)
    captured: dict[str, object] = {}

    class _Runner:
        def run(self, **kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(engine_version="native-test")

    class _Executor:
        def __init__(self, *_args: object) -> None:
            pass

        def _write_artifacts(self, run_id: str, *_args: object) -> SimpleNamespace:
            return SimpleNamespace(run_id=run_id)

    class _Strategy:
        metadata = SimpleNamespace(id="plugin-test")

    def result_payload(
        summary: SimpleNamespace,
        native: SimpleNamespace,
        dataset: dict[str, object],
        run_spec: dict[str, object],
    ) -> dict[str, object]:
        captured["result_dataset"] = dataset
        captured["result_spec"] = run_spec
        return {"run_id": summary.run_id, "engine": native.engine_version, "decision_traces": []}

    monkeypatch.setattr(entrypoint, "INPUT", input_dir)
    monkeypatch.setattr(entrypoint, "OUTPUT", output_dir)
    monkeypatch.setattr(entrypoint, "DATASET_PATH", dataset_dir)
    monkeypatch.setattr(entrypoint, "_load_plugin", lambda *_args: _Strategy())
    monkeypatch.setattr(entrypoint, "_assumptions", lambda _spec: object())
    monkeypatch.setattr(entrypoint, "_initializer", lambda *_args: lambda: None)
    monkeypatch.setattr(entrypoint, "_instrument", lambda *_args: object())
    monkeypatch.setattr(entrypoint, "_run_id", lambda *_args: "plugin-window-run")
    monkeypatch.setattr(entrypoint, "NautilusDataFrameRunner", _Runner)
    monkeypatch.setattr(entrypoint, "NautilusResearchExecutor", _Executor)
    monkeypatch.setattr(entrypoint, "_result_payload", result_payload)

    entrypoint.main()

    assert captured["decision_start"] == index[2]
    assert list(cast(pd.DataFrame, captured["frame"]).index) == list(index[:4])
    run_spec = cast(dict[str, object], captured["result_spec"])
    assert run_spec["plugin_source_sha256"] == hashlib.sha256(source).hexdigest()
    assert run_spec["plugin_strategy_version_id"] == "version-a"
    assert run_spec["strategy_params"] == {"threshold": 3}
    result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert result["plugin"]["decision_start"] == index[2].isoformat()
    assert result["plugin"]["temporal_integrity"] == "unverified"
    assert result["plugin"]["verified_leaderboard_eligible"] is False
    assert result["plugin"]["metrics_integrity"] == "unverified_plugin_origin"


def test_actual_native_plugin_trade_and_window_when_docker_image_is_available(tmp_path: Path) -> None:
    if os.environ.get("QT_RUN_DOCKER_PLUGIN_TEST") != "1":
        pytest.skip("set QT_RUN_DOCKER_PLUGIN_TEST=1 after building the local plugin image")
    info = subprocess.run(["docker", "info"], check=False, capture_output=True, text=True)
    if info.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    image = "qt-plugin-runtime:py312-nautilus-2.0.0rc4"
    inspect = subprocess.run(["docker", "image", "inspect", image], check=False, capture_output=True, text=True)
    if inspect.returncode != 0:
        pytest.skip("build the pinned local plugin image before this integration test")

    parquet_root = tmp_path / "parquet"
    path = parquet_root / "ohlcv" / "okx_BTCUSDT_1h.parquet"
    path.parent.mkdir(parents=True)
    index = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    pd.DataFrame(
        {
            "open": [100.0 + number for number in range(len(index))],
            "high": [101.0 + number for number in range(len(index))],
            "low": [99.0 + number for number in range(len(index))],
            "close": [100.5 + number for number in range(len(index))],
            "volume": [10.0] * len(index),
        },
        index=index,
    ).to_parquet(path)
    runtime = IsolatedPluginRuntime(parquet_root=parquet_root, artifact_root=tmp_path / "artifacts")
    dataset = runtime.datasets.get("okx-btcusdt-1h")
    result = runtime.execute_plugin(
        _version(),
        {
            "dataset_id": "okx-btcusdt-1h",
            "dataset_fingerprint": dataset["fingerprint"],
            "assumptions": {"initial_cash": 10_000, "fee_bps": 0},
            "seed": 7,
            "market": "spot",
            "from": index[24].isoformat(),
            "to": index[-1].isoformat(),
        },
        {},
        lambda _stage, _percent: None,
        lambda: False,
    )
    assert result["engine"] == "nautilus_trader"
    assert result["decision_traces"]
    plugin_result = cast(dict[str, object], result["plugin"])
    assert plugin_result["decision_start"] == index[24].isoformat()
    published = tmp_path / "artifacts" / "plugins" / str(result["run_id"])
    assert (published / "result.json").is_file()
    orders = pd.read_csv(published / "artifacts" / str(result["run_id"]) / "orders.csv")
    fills = pd.read_csv(published / "artifacts" / str(result["run_id"]) / "fills.csv")
    assert not orders.empty
    assert not fills.empty
    decision_start = index[24]
    assert _native_event_times(orders).min() >= decision_start
    assert _native_event_times(fills).min() >= decision_start
    manifest = json.loads((published / "environment-manifest.json").read_text(encoding="utf-8"))
    isolation = cast(dict[str, object], manifest["container_isolation"])
    assert isolation["effective_uid"] == os.getuid() != 0
    assert isolation["rootfs_writable"] is False
    assert isolation["docker_socket_present"] is False
    assert isolation["network_interfaces"] == ["lo"]
    limits = cast(dict[str, object], isolation["cgroup_limits"])
    assert limits["memory_max"] == str(PluginRuntimeSettings().memory_mib * 1024 * 1024)
    assert limits["pids_max"] == str(PluginRuntimeSettings().pid_limit)
    assert limits["cpu_max"] == "100000 100000"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_event_times(frame: pd.DataFrame) -> pd.DatetimeIndex:
    for column in ("ts_event", "timestamp", "ts_init"):
        if column not in frame:
            continue
        values = frame[column]
        if pd.api.types.is_numeric_dtype(values):
            return pd.to_datetime(values, unit="ns", utc=True)
        return pd.to_datetime(values, utc=True)
    raise AssertionError("native report did not contain an event timestamp")
