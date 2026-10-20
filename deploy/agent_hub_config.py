"""Noninteractive personal setup explicitly authorized for the agent hub."""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
from pathlib import Path


def read_private(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("hub environment must be a private regular file")
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, raw = line.split("=", 1)
        values = shlex.split(raw)
        if len(values) != 1 or not key.startswith("QT_AGENT_HUB_"):
            raise ValueError("invalid private hub configuration")
        result[key] = values[0]
    return result


def initialize(path: Path, root: Path) -> None:
    if path.exists():
        values = read_private(path)
        changed = False
        if not values.get("QT_AGENT_HUB_EXECUTOR_TOKEN"):
            values["QT_AGENT_HUB_EXECUTOR_TOKEN"] = secrets.token_urlsafe(36)
            changed = True
        if not values.get("QT_AGENT_HUB_TRADING_SECRETS"):
            location = root.resolve() / "data" / "runtime" / "agent-hub-execution.json"
            location.parent.mkdir(parents=True, exist_ok=True)
            if not location.exists():
                fd = os.open(location, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write("{}\n")
            values["QT_AGENT_HUB_TRADING_SECRETS"] = str(location)
            changed = True
        if changed:
            with path.open("w") as stream:
                for key, value in values.items():
                    stream.write(f"{key}={shlex.quote(value)}\n")
        print("Existing private Agent Hub configuration preserved.")
        return
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if not auth.is_file() or auth.is_symlink():
        raise ValueError("local Codex auth.json missing; run codex login")
    imports = root / "data" / "agent-hub-imports"
    imports.mkdir(parents=True, exist_ok=True)
    seed = imports / "research-policy.md"
    if not seed.exists():
        seed.write_text(
            "# Personal research policy\n"
            "Use only time-available evidence. Record source, data version, costs and limitations.\n"
            "BTC and binary prediction contracts are different instruments.\n"
            "All automatic execution starts in paper mode. An unsupported forecast must HOLD.\n"
            "The model cannot change risk limits or access account credentials.\n"
        )
    values = {
        "QT_AGENT_HUB_PORT": "8878",
        "QT_AGENT_HUB_SERVICE_TOKEN": secrets.token_urlsafe(36),
        "QT_AGENT_HUB_EXECUTOR_TOKEN": secrets.token_urlsafe(36),
        "QT_AGENT_HUB_IMPORTS": str(imports.resolve()),
        "QT_AGENT_HUB_CODEX_AUTH": str(auth.resolve()),
    }
    execution = root.resolve() / "data" / "runtime" / "agent-hub-execution.json"
    execution.parent.mkdir(parents=True, exist_ok=True)
    if not execution.exists():
        fd = os.open(execution, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("{}\n")
    values["QT_AGENT_HUB_TRADING_SECRETS"] = str(execution)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for key, value in values.items():
            stream.write(f"{key}={shlex.quote(value)}\n")
    print("Private configuration created; credentials were not printed.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "check"])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.env_file, args.root)
    else:
        values = read_private(args.env_file)
        if len(values.get("QT_AGENT_HUB_SERVICE_TOKEN", "")) < 32:
            raise ValueError("hub access token is missing or too short")
        if (
            len(values.get("QT_AGENT_HUB_EXECUTOR_TOKEN", "")) < 32
            or values["QT_AGENT_HUB_EXECUTOR_TOKEN"] == values["QT_AGENT_HUB_SERVICE_TOKEN"]
        ):
            raise ValueError("independent executor token required; run hub-init")
        port = int(values.get("QT_AGENT_HUB_PORT", "0"))
        if not 1024 <= port <= 65535:
            raise ValueError("hub port is invalid")
        for key in ("QT_AGENT_HUB_IMPORTS", "QT_AGENT_HUB_CODEX_AUTH"):
            if not Path(values.get(key, "")).exists():
                raise ValueError(f"{key} path is missing")
        execution = Path(values.get("QT_AGENT_HUB_TRADING_SECRETS", ""))
        if execution.is_symlink() or not execution.is_file() or execution.stat().st_mode & 0o077:
            raise ValueError("private trading secret file required; run hub-init")
        print("Agent Hub private configuration valid.")


if __name__ == "__main__":
    main()
