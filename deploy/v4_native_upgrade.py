"""Run isolated rc4/rc7 fixtures and record differences without promoting either."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from v4_config import read_private, validate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    validate(read_private(args.env_file))
    compose = ["docker", "--context", "orbstack", "compose", "--project-name", "qt-production-v4",
               "--project-directory", str(args.root), "--env-file", str(args.env_file),
               "-f", str(args.root / "deploy/v4-compose.yml"), "--profile", "candidate"]
    directory = args.root / "data/runtime/production-v4/native-parity" / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    paths = []
    for service, version, label in (("native-baseline-check", "2.0.0rc4", "baseline"),
                                    ("native-candidate", "2.0.0rc7", "candidate")):
        result = subprocess.run([*compose, "run", "--rm", "-T", "--no-deps", service,
                                 "--expected-version", version, "--output", "-"],
                                capture_output=True, text=True, check=False)
        diagnostic = directory / f"{label}.stderr.log"
        descriptor = os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(result.stderr)
        if result.returncode:
            descriptor = os.open(directory / "comparison.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"status": "blocked", "baseline_version": "2.0.0rc4",
                           "candidate_version": "2.0.0rc7", "failed_stage": f"{label}_fixture",
                           "economic_parity": None, "upgrade_accepted": False,
                           "reason": "native_adapter_compatibility_requires_independent_migration",
                           "diagnostic_file": diagnostic.name}, stream, sort_keys=True)
            raise RuntimeError(f"{label} fixture failed; upgrade remains unaccepted; private diagnostics: {diagnostic}")
        report = json.loads(result.stdout)
        path = directory / f"{label}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, sort_keys=True, allow_nan=False)
        paths.append(path)
    destination = directory / "comparison.json"
    subprocess.run([sys.executable, str(args.root / "deploy/v4_native_compare.py"),
                    "--compare", *map(str, paths), "--output", str(destination)], check=True)
    print(json.dumps({"comparison": str(destination), "upgrade_accepted": False}))


if __name__ == "__main__":
    main()
