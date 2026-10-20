"""Prepare only this Compose project's persistent directories before workers start."""

from __future__ import annotations

import os
from pathlib import Path

from qt.agent_hub.config import files_root, load_config, state_root


def main() -> None:
    tenant = load_config().tenant_id
    directories = (
        state_root(),
        state_root() / "heartbeats",
        files_root(),
        files_root() / tenant,
        files_root() / tenant / "documents",
        files_root() / tenant / "artifacts",
        files_root() / tenant / "imports",
    )
    for path in directories:
        path = Path(path)
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("persistent directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not path.is_dir():
            raise ValueError("persistent path must be a directory")
        os.chown(path, 10001, 10001)
    print("Agent Hub persistent directories ready.")


if __name__ == "__main__":
    main()
