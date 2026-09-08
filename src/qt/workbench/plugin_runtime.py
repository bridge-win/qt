"""Docker-only execution boundary for immutable research strategy plugins.

This module is worker-side infrastructure.  It is not imported by FastAPI and
never falls back to host-process plugin execution.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias, cast

from qt.lab.service import CancellationCheck, ProgressCallback
from qt.research.datasets import DatasetCatalog

JsonDict: TypeAlias = dict[str, object]
DockerRun: TypeAlias = Callable[..., subprocess.CompletedProcess[str]]

PLUGIN_API_VERSION = "1"
MAX_SOURCE_BYTES = 100_000
MAX_WALL_SECONDS = 15 * 60
MAX_MEMORY_MIB = 512
MAX_PIDS = 64
MAX_CPU = 1.0
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_FILES = 256
MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_READ_SECONDS = 5
_POLL_SECONDS = 0.2
_ALLOWED_IMPORT_ROOTS = frozenset({"btc_backtest", "collections", "decimal", "math", "typing"})


class PluginRuntimeError(RuntimeError):
    """A plugin or container failure that can safely be presented to an operator."""


class PluginCancelledError(PluginRuntimeError):
    """The common worker requested cancellation of this plugin container."""


@dataclass(frozen=True)
class PluginRuntimeSettings:
    image: str = "qt-plugin-runtime:py312-nautilus-2.0.0rc4"
    wall_seconds: int = 300
    memory_mib: int = MAX_MEMORY_MIB
    cpu_limit: float = MAX_CPU
    pid_limit: int = MAX_PIDS
    output_limit_bytes: int = MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if not 1 <= self.wall_seconds <= MAX_WALL_SECONDS:
            raise ValueError(f"wall_seconds must be between 1 and {MAX_WALL_SECONDS}")
        if not 64 <= self.memory_mib <= MAX_MEMORY_MIB:
            raise ValueError(f"memory_mib must be between 64 and {MAX_MEMORY_MIB}")
        if not 0 < self.cpu_limit <= MAX_CPU:
            raise ValueError(f"cpu_limit must be between 0 and {MAX_CPU}")
        if not 16 <= self.pid_limit <= MAX_PIDS:
            raise ValueError(f"pid_limit must be between 16 and {MAX_PIDS}")
        if not 1_024 * 1_024 <= self.output_limit_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError("output_limit_bytes must be between 1 MiB and 64 MiB")


class IsolatedPluginRuntime:
    """Run versioned plugins only in a tightly constrained Docker container."""

    def __init__(
        self,
        *,
        parquet_root: Path,
        artifact_root: Path,
        work_root: Path | None = None,
        settings: PluginRuntimeSettings | None = None,
        docker_run: DockerRun = subprocess.run,
        popen: type[subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.parquet_root = parquet_root.resolve()
        self.artifact_root = artifact_root.resolve()
        self.work_root = (work_root or artifact_root / "plugin-work").resolve()
        self.settings = settings or PluginRuntimeSettings()
        self._docker_run = docker_run
        self._popen = popen
        self.datasets = DatasetCatalog(self.parquet_root)

    def execute_plugin(
        self,
        strategy_version: Mapping[str, object],
        experiment: Mapping[str, object],
        parameter_overrides: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> JsonDict:
        """Execute one immutable plugin version with the common worker callbacks.

        The returned JSON is read from an output file created by the container;
        it is not calculated by this host process.
        """

        _host_nonroot_identity()
        progress("plugin_validate", 5)
        source, version_id = _plugin_source(strategy_version)
        imports = validate_plugin_source(source, strategy_version)
        _check_cancelled(cancelled)
        dataset_id = _required_text(experiment, "dataset_id")
        dataset = self.datasets.get(dataset_id)
        dataset_path = self.datasets.path_for(dataset_id).resolve()
        _contained(self.parquet_root, dataset_path)
        _verify_dataset_fingerprint(self.datasets, dataset_id, dataset, experiment)
        image_digest = self._image_digest()
        task_dir = self._new_task_dir()
        container_name = f"qt-plugin-{uuid.uuid4().hex}"
        try:
            input_dir, output_dir = _prepare_task_mounts(task_dir)
            control = _control_payload(
                source=source,
                strategy_version=strategy_version,
                experiment=experiment,
                parameter_overrides=parameter_overrides,
                dataset=dataset,
                image=self.settings.image,
                image_digest=image_digest,
                imports=imports,
            )
            encoded = _canonical_json(control).encode("utf-8")
            if len(encoded) > MAX_INPUT_BYTES:
                raise PluginRuntimeError("plugin control input exceeds 256 KiB")
            _write_private(input_dir / "control.json", encoded, mode=0o400)
            _write_private(input_dir / "source.py", source.encode("utf-8"), mode=0o400)
            _seal_input_mount(input_dir)
            progress("plugin_container", 15)
            self._run_container(
                container_name=container_name,
                input_dir=input_dir,
                output_dir=output_dir,
                dataset_path=dataset_path,
                dataset_key=_required_text(dataset, "key"),
                image_digest=image_digest,
                progress=progress,
                cancelled=cancelled,
            )
            progress("plugin_collect", 90)
            _verify_dataset_fingerprint(self.datasets, dataset_id, dataset, experiment)
            _validate_output_tree(output_dir, max_bytes=self.settings.output_limit_bytes)
            result = _read_result(output_dir / "result.json")
            manifest = _read_result(output_dir / "environment-manifest.json")
            artifact_paths = _artifact_relative_paths(result)
            _validate_artifact_files(output_dir, artifact_paths)
            runtime_metadata: JsonDict = {
                "image": self.settings.image,
                "image_digest": image_digest,
                "source_sha256": _sha256_bytes(source.encode("utf-8")),
                "strategy_version_id": version_id,
                "temporal_integrity": {
                    "status": "unverified",
                    "reason": "isolation does not prevent full-history reads or native-module monkeypatching",
                },
                "verified_leaderboard_eligible": False,
                "environment_manifest": manifest,
            }
            result["plugin_runtime"] = runtime_metadata
            published = self._publish_output(output_dir, str(result.get("run_id", "")))
            result["artifacts"] = _rebase_artifact_descriptors(published, artifact_paths)
            result["verification"] = {
                "status": "unverified_plugin_origin",
                "verified_leaderboard_eligible": False,
            }
            result["metrics_integrity"] = "unverified_plugin_origin"
            runtime_metadata["artifact_directory"] = str(published)
            progress("plugin_completed", 100)
        except PluginCancelledError:
            self._stop_own_container(container_name)
            self._mark_recoverable(task_dir, "cancelled")
            raise
        except Exception as error:
            self._stop_own_container(container_name)
            self._mark_recoverable(task_dir, f"failed: {type(error).__name__}")
            if isinstance(error, PluginRuntimeError):
                raise
            raise PluginRuntimeError(f"plugin container failed: {type(error).__name__}: {error}") from error
        else:
            _remove_owned_task_dir(task_dir, self.work_root)
            return result

    def build_image(self, *, context_root: Path) -> str:
        """Build the pinned local runtime image; never push or pull at execution time."""

        dockerfile = context_root / "src/qt/workbench/plugin_image/Dockerfile"
        if not dockerfile.is_file():
            raise PluginRuntimeError("plugin runtime Dockerfile is missing")
        command = [
            "docker", "build", "--pull=false", "--tag", self.settings.image,
            "--file", str(dockerfile), str(context_root.resolve()),
        ]
        completed = self._docker_run(command, check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise PluginRuntimeError(_docker_error("docker image build failed", completed))
        return self._image_digest()

    def _image_digest(self) -> str:
        completed = self._docker_run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.settings.image],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise PluginRuntimeError(
                "plugin runtime image is unavailable; build the pinned local image before executing plugins"
            )
        digest = completed.stdout.strip()
        if not digest.startswith("sha256:"):
            raise PluginRuntimeError("docker returned an invalid runtime image digest")
        return digest

    def _new_task_dir(self) -> Path:
        self.work_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="plugin-task-", dir=self.work_root))

    def _run_container(
        self,
        *,
        container_name: str,
        input_dir: Path,
        output_dir: Path,
        dataset_path: Path,
        dataset_key: str,
        image_digest: str,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> None:
        target = f"/data/ohlcv/{dataset_key}.parquet"
        command = docker_command(
            image=image_digest,
            container_name=container_name,
            input_dir=input_dir,
            output_dir=output_dir,
            dataset_path=dataset_path,
            dataset_target=target,
            settings=self.settings,
        )
        process = self._popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = time.monotonic()
        while process.poll() is None:
            if cancelled():
                process.terminate()
                _wait_for_process(process, timeout_seconds=5)
                raise PluginCancelledError("plugin execution cancelled")
            if time.monotonic() - started > self.settings.wall_seconds:
                process.terminate()
                _wait_for_process(process, timeout_seconds=5)
                raise PluginRuntimeError("plugin execution exceeded its wall-time limit")
            if _directory_size(output_dir) > self.settings.output_limit_bytes:
                process.terminate()
                _wait_for_process(process, timeout_seconds=5)
                raise PluginRuntimeError("plugin output exceeded the 64 MiB limit")
            elapsed = min(70, int((time.monotonic() - started) * 70 / self.settings.wall_seconds))
            progress("plugin_native_engine", 20 + elapsed)
            time.sleep(_POLL_SECONDS)
        if process.returncode != 0:
            raise PluginRuntimeError("plugin container exited unsuccessfully; inspect its recoverable task manifest")
        if _directory_size(output_dir) > self.settings.output_limit_bytes:
            raise PluginRuntimeError("plugin output exceeded the 64 MiB limit")

    def _stop_own_container(self, container_name: str) -> None:
        self._docker_run(
            ["docker", "rm", "--force", container_name],
            check=False,
            text=True,
            capture_output=True,
        )

    def _mark_recoverable(self, task_dir: Path, reason: str) -> None:
        if task_dir.exists():
            _write_private(task_dir / "RECOVERABLE_FAILURE.json", _canonical_json({"reason": reason}).encode("utf-8"))

    def _publish_output(self, output_dir: Path, run_id: str) -> Path:
        if not run_id or not _safe_run_id(run_id):
            raise PluginRuntimeError("container result is missing a path-safe run_id")
        target = self.artifact_root / "plugins" / run_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise PluginRuntimeError("refusing to overwrite existing plugin artifacts")
        _validate_output_tree(output_dir, max_bytes=self.settings.output_limit_bytes)
        staging = target.parent / f".{run_id}.publish-{uuid.uuid4().hex}"
        try:
            _copy_safe_tree(output_dir, staging, max_bytes=self.settings.output_limit_bytes)
            _validate_output_tree(staging, max_bytes=self.settings.output_limit_bytes)
            if target.exists():
                raise PluginRuntimeError("refusing to overwrite existing plugin artifacts")
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return target


def validate_plugin_source(source: str, strategy_version: Mapping[str, object]) -> tuple[str, ...]:
    """Validate entrypoint and allowed dependency names; this is not a sandbox."""

    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise PluginRuntimeError("plugin source exceeds the 100 KiB version limit")
    content = strategy_version.get("content")
    metadata = content if isinstance(content, Mapping) else strategy_version
    if str(metadata.get("plugin_api_version", "")) != PLUGIN_API_VERSION:
        raise PluginRuntimeError(f"unsupported plugin_api_version; expected {PLUGIN_API_VERSION}")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as error:
        raise PluginRuntimeError(f"plugin source has invalid syntax: {error.msg}") from error
    build_functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_strategy"]
    if len(build_functions) != 1 or isinstance(build_functions[0], ast.AsyncFunctionDef):
        raise PluginRuntimeError("plugin must define exactly one synchronous build_strategy(parameters) function")
    if len(build_functions[0].args.args) != 1:
        raise PluginRuntimeError("build_strategy must accept exactly one parameters argument")
    imports = _imports(tree)
    unknown = sorted(root for root in imports if root not in _ALLOWED_IMPORT_ROOTS)
    if unknown:
        raise PluginRuntimeError(f"plugin imports unavailable dependencies: {', '.join(unknown)}")
    return tuple(sorted(imports))


def docker_command(
    *,
    image: str,
    container_name: str,
    input_dir: Path,
    output_dir: Path,
    dataset_path: Path,
    dataset_target: str,
    settings: PluginRuntimeSettings,
) -> list[str]:
    """Return the complete no-network, non-root Docker invocation."""

    if not dataset_target.startswith("/data/ohlcv/") or not dataset_target.endswith(".parquet"):
        raise PluginRuntimeError("dataset target must be a fixed container parquet path")
    uid, gid = _host_nonroot_identity()
    return [
        "docker", "run", "--rm", "--pull=never", "--name", container_name,
        "--network", "none", "--read-only", "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(settings.pid_limit), "--memory", f"{settings.memory_mib}m",
        "--cpus", str(settings.cpu_limit), "--ulimit", f"fsize={settings.output_limit_bytes}:{settings.output_limit_bytes}",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--mount", f"type=bind,src={input_dir.resolve()},dst=/input,readonly",
        "--mount", f"type=bind,src={dataset_path.resolve()},dst={dataset_target},readonly",
        "--mount", f"type=bind,src={output_dir.resolve()},dst=/output",
        image,
    ]


def _plugin_source(strategy_version: Mapping[str, object]) -> tuple[str, str]:
    content = strategy_version.get("content")
    if not isinstance(content, Mapping) or str(content.get("mode")) != "plugin":
        raise PluginRuntimeError("strategy version is not an immutable plugin version")
    source = content.get("source_code")
    if not isinstance(source, str) or not source.strip():
        raise PluginRuntimeError("plugin version has no source_code")
    version_id = strategy_version.get("version_id")
    if not isinstance(version_id, str) or not version_id:
        raise PluginRuntimeError("plugin version is missing version_id")
    return source, version_id


def _control_payload(
    *, source: str,
    strategy_version: Mapping[str, object],
    experiment: Mapping[str, object],
    parameter_overrides: Mapping[str, object],
    dataset: Mapping[str, object],
    image: str,
    image_digest: str,
    imports: tuple[str, ...],
) -> JsonDict:
    return {
        "schema_version": 1,
        "source_sha256": _sha256_bytes(source.encode("utf-8")),
        "strategy_version_id": _required_text(strategy_version, "version_id"),
        "experiment": dict(experiment),
        "parameter_overrides": dict(parameter_overrides),
        "dataset": dict(dataset),
        "environment": {"image": image, "image_digest": image_digest, "imports": list(imports)},
    }


def _imports(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def _read_result(path: Path) -> JsonDict:
    try:
        parsed = json.loads(_read_regular_file(path, max_bytes=MAX_OUTPUT_BYTES).decode("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PluginRuntimeError("container produced invalid result JSON") from error
    if not isinstance(parsed, dict):
        raise PluginRuntimeError("container result must be an object")
    return cast(JsonDict, parsed)


def _required_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise PluginRuntimeError(f"{key} is required")
    return item


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contained(root: Path, path: Path) -> None:
    if root != path and root not in path.parents:
        raise PluginRuntimeError("path escapes the configured parquet root")


def _prepare_task_mounts(task_dir: Path) -> tuple[Path, Path]:
    """Create private task mounts while the host is still writing their inputs."""

    _host_nonroot_identity()
    task_dir.chmod(0o700)
    input_dir = task_dir / "input"
    output_dir = task_dir / "output"
    input_dir.mkdir(mode=0o700)
    output_dir.mkdir(mode=0o700)
    input_dir.chmod(0o700)
    output_dir.chmod(0o700)
    return input_dir, output_dir


def _seal_input_mount(input_dir: Path) -> None:
    """Make the fully populated input mount traversable/readable but immutable."""

    input_dir.chmod(0o500)


def _write_private(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _directory_size(path: Path) -> int:
    return _validate_output_tree(path, max_bytes=MAX_OUTPUT_BYTES)


def _validate_output_tree(root: Path, *, max_bytes: int) -> int:
    """Reject links and special files before any host read or publication."""

    try:
        root_descriptor = _open_directory(root)
    except OSError as error:
        raise PluginRuntimeError("plugin output directory is unavailable") from error
    total = 0
    files = 0
    pending = [root_descriptor]
    try:
        while pending:
            descriptor = pending.pop()
            try:
                for entry in os.scandir(descriptor):
                    try:
                        entry_stat = os.stat(
                            entry.name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        raise PluginRuntimeError("cannot inspect plugin output entry") from error
                    if stat.S_ISLNK(entry_stat.st_mode):
                        raise PluginRuntimeError("plugin output may not contain symlinks")
                    if stat.S_ISDIR(entry_stat.st_mode):
                        try:
                            pending.append(_open_directory(entry.name, dir_fd=descriptor))
                        except OSError as error:
                            raise PluginRuntimeError("cannot inspect plugin output directory") from error
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode):
                        raise PluginRuntimeError("plugin output may contain only regular files")
                    if entry_stat.st_nlink != 1:
                        raise PluginRuntimeError("plugin output may not contain hard-linked files")
                    files += 1
                    if files > MAX_OUTPUT_FILES:
                        raise PluginRuntimeError("plugin output exceeds the 256 file limit")
                    total += entry_stat.st_size
                    if total > max_bytes:
                        raise PluginRuntimeError("plugin output exceeded the configured size limit")
            finally:
                os.close(descriptor)
    except Exception:
        for descriptor in pending:
            os.close(descriptor)
        raise
    return total


def _open_directory(path: Path | str, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, dir_fd=dir_fd)


def _regular_file_flags() -> int:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        parent_descriptor = _open_directory(path.parent)
    except OSError as error:
        raise PluginRuntimeError("container did not produce a safe result file") from error
    try:
        return _read_regular_file_at(parent_descriptor, path.name, max_bytes=max_bytes)
    finally:
        os.close(parent_descriptor)


def _read_regular_file_at(directory_descriptor: int, name: str, *, max_bytes: int) -> bytes:
    try:
        descriptor = os.open(name, _regular_file_flags(), dir_fd=directory_descriptor)
    except OSError as error:
        raise PluginRuntimeError("container did not produce a safe result file") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_bytes:
            raise PluginRuntimeError("container did not produce a bounded result file")
        if file_stat.st_nlink != 1:
            raise PluginRuntimeError("container did not produce a safe result file")
        deadline = time.monotonic() + MAX_OUTPUT_READ_SECONDS
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            if time.monotonic() > deadline:
                raise PluginRuntimeError("container result read exceeded its deadline")
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
        raise PluginRuntimeError("container did not produce a bounded result file")
    finally:
        os.close(descriptor)


def _copy_safe_tree(source: Path, target: Path, *, max_bytes: int) -> None:
    """Copy verified artifacts using descriptor-relative reads only."""

    _validate_output_tree(source, max_bytes=max_bytes)
    target.mkdir(mode=0o750)
    try:
        root_descriptor = _open_directory(source)
    except OSError as error:
        raise PluginRuntimeError("plugin output directory is unavailable") from error
    copied = 0
    pending = [(root_descriptor, target)]
    try:
        while pending:
            descriptor, target_dir = pending.pop()
            try:
                for entry in os.scandir(descriptor):
                    try:
                        entry_stat = os.stat(
                            entry.name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        raise PluginRuntimeError("plugin output changed while being published") from error
                    child_target = target_dir / entry.name
                    if stat.S_ISDIR(entry_stat.st_mode):
                        child_target.mkdir(mode=0o750)
                        pending.append((_open_directory(entry.name, dir_fd=descriptor), child_target))
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode):
                        raise PluginRuntimeError("plugin output changed while being published")
                    if entry_stat.st_nlink != 1:
                        raise PluginRuntimeError("plugin output changed while being published")
                    content = _read_regular_file_at(
                        descriptor,
                        entry.name,
                        max_bytes=max_bytes - copied,
                    )
                    copied += len(content)
                    if copied > max_bytes:
                        raise PluginRuntimeError("plugin output exceeded the configured size limit")
                    _write_private(child_target, content, mode=0o640)
            finally:
                os.close(descriptor)
    except Exception:
        for descriptor, _ in pending:
            os.close(descriptor)
        raise


def _artifact_relative_paths(result: Mapping[str, object]) -> tuple[PurePosixPath, ...]:
    run_id = result.get("run_id")
    artifacts = result.get("artifacts")
    if not isinstance(run_id, str) or not _safe_run_id(run_id):
        raise PluginRuntimeError("container result is missing a path-safe run_id")
    if not isinstance(artifacts, list):
        raise PluginRuntimeError("container result artifacts must be a list")
    expected_root = PurePosixPath("/output/artifacts") / run_id
    paths: list[PurePosixPath] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise PluginRuntimeError("container result contains an invalid artifact descriptor")
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str):
            raise PluginRuntimeError("container artifact descriptor is missing a path")
        candidate = PurePosixPath(raw_path)
        if candidate == expected_root or expected_root not in candidate.parents:
            raise PluginRuntimeError("container artifact path escapes the output artifact root")
        relative = candidate.relative_to("/output")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise PluginRuntimeError("container artifact path is invalid")
        if relative in paths:
            raise PluginRuntimeError("container result contains duplicate artifact paths")
        paths.append(relative)
    return tuple(paths)


def _validate_artifact_files(root: Path, paths: Sequence[PurePosixPath]) -> None:
    for relative in paths:
        _describe_safe_artifact(root, relative)


def _rebase_artifact_descriptors(root: Path, paths: Sequence[PurePosixPath]) -> list[JsonDict]:
    return [_describe_safe_artifact(root, relative) for relative in paths]


def _describe_safe_artifact(root: Path, relative: PurePosixPath) -> JsonDict:
    target = root.joinpath(*relative.parts)
    _contained(root.resolve(), target.resolve())
    try:
        descriptor = os.open(target, _regular_file_flags())
    except OSError as error:
        raise PluginRuntimeError("declared plugin artifact is unavailable") from error
    digest = hashlib.sha256()
    total = 0
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise PluginRuntimeError("declared plugin artifact is not a safe regular file")
        deadline = time.monotonic() + MAX_OUTPUT_READ_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise PluginRuntimeError("plugin artifact hash exceeded its deadline")
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise PluginRuntimeError("declared plugin artifact exceeds the output limit")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return {
        "name": target.name,
        "path": relative.as_posix(),
        "sha256": digest.hexdigest(),
        "media_type": _artifact_media_type(target),
        "size_bytes": total,
        "rows": None,
        "metadata_source": "server_rebased",
    }


def _artifact_media_type(path: Path) -> str:
    if path.suffix == ".csv":
        return "text/csv"
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".parquet":
        return "application/vnd.apache.parquet"
    return "application/octet-stream"


def _verify_dataset_fingerprint(
    catalog: DatasetCatalog,
    dataset_id: str,
    dataset: Mapping[str, object],
    experiment: Mapping[str, object],
) -> None:
    expected = dataset.get("fingerprint")
    if not isinstance(expected, str) or not expected:
        raise PluginRuntimeError("ready dataset is missing a fingerprint")
    requested = experiment.get("dataset_fingerprint")
    if requested is not None and requested != expected:
        raise PluginRuntimeError("dataset fingerprint changed; submit a new immutable research request")
    refreshed = catalog.get(dataset_id)
    if refreshed.get("fingerprint") != expected:
        raise PluginRuntimeError("dataset fingerprint changed while preparing plugin execution")


def _wait_for_process(process: subprocess.Popen[bytes], *, timeout_seconds: int) -> None:
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def _check_cancelled(cancelled: CancellationCheck) -> None:
    if cancelled():
        raise PluginCancelledError("plugin execution cancelled")


def _safe_run_id(value: str) -> bool:
    return all(character.isalnum() or character in "_-" for character in value) and len(value) <= 128


def _host_nonroot_identity() -> tuple[int, int]:
    uid = os.getuid()
    gid = os.getgid()
    if uid <= 0 or gid <= 0:
        raise PluginRuntimeError(
            "plugin execution refuses a root host; configure a dedicated unprivileged worker account"
        )
    return uid, gid


def _remove_owned_task_dir(path: Path, work_root: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != work_root.resolve() or not resolved.name.startswith("plugin-task-"):
        raise PluginRuntimeError("refusing to remove a non-plugin task directory")
    input_dir = resolved / "input"
    try:
        input_stat = input_dir.lstat()
    except OSError as error:
        raise PluginRuntimeError("owned plugin task input is unavailable for cleanup") from error
    if not stat.S_ISDIR(input_stat.st_mode):
        raise PluginRuntimeError("owned plugin task input is not a directory")
    # The successful container has stopped before this path is reached.  Reopen
    # only this verified task's sealed input directory so its children can be
    # unlinked; never change dataset or output-mount permissions.
    input_dir.chmod(0o700)
    shutil.rmtree(resolved)


def _docker_error(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout).strip()
    return f"{prefix}: {detail[:1000] or 'no diagnostic output'}"
