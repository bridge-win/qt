"""Run all Hub tests without importing the full repository's conftest.

The test images receive a read-only /tests mount, never credentials or state.
Copying the matching files also makes newly added Hub tests part of CI without
maintaining a second explicit filename list in Compose.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import shutil
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-root", type=Path, default=Path("/tests"))
    parser.add_argument("--native", action="store_true")
    args, pytest_args = parser.parse_known_args()
    if args.native:
        # Fail, rather than accepting the optional native test's importorskip.
        importlib.import_module("nautilus_trader")
        pattern = "test_agent_hub_research.py"
    else:
        importlib.import_module("polymarket")
        if importlib.metadata.version("polymarket-client") != "0.13.0":
            raise RuntimeError("The pinned production Polymarket SDK must be installed")
        importlib.import_module("opentelemetry.sdk")
        importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        if importlib.metadata.version("opentelemetry-sdk") != "1.45.1":
            raise RuntimeError("The pinned production OpenTelemetry SDK must be installed")
        pattern = "test_agent_hub_*.py"
    selected = sorted(args.tests_root.glob(pattern))
    if not selected or any(not path.is_file() or path.is_symlink() for path in selected):
        raise RuntimeError("Hub tests must be regular files in the mounted test directory")
    import pytest

    with tempfile.TemporaryDirectory(prefix="qt-hub-tests-") as directory:
        test_directory = Path(directory) / "tests"
        test_directory.mkdir()
        for path in selected:
            shutil.copyfile(path, test_directory / path.name)
        # Keep repository-relative config lookups meaningful in contract tests.
        # Production images contain only public Hub manifests at this location.
        config = Path(__file__).resolve().parents[1] / "config"
        if config.is_dir():
            shutil.copytree(config, Path(directory) / "config")
        print(f"Running {len(selected)} Hub test files; native={args.native}", flush=True)
        return int(
            pytest.main(
                [
                    str(test_directory),
                    "-q",
                    f"--confcutdir={test_directory}",
                    "-o",
                    f"cache_dir={directory}/.cache",
                    *pytest_args,
                ]
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
