"""Private V4 setup. Values never appear in command output or tracked files."""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlsplit


def read_private(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("V4 environment must be a private regular file")
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        if not separator or not key.startswith(("QT_V4_", "QT_EXPERIMENT_IMAGE_DIGEST")):
            raise ValueError("invalid V4 configuration key")
        parsed = shlex.split(raw)
        if len(parsed) > 1 or key in values:
            raise ValueError("invalid V4 configuration value")
        values[key] = parsed[0] if parsed else ""
    return values


def initialize(path: Path) -> None:
    if path.exists():
        validate(read_private(path))
        print("Existing private V4 configuration preserved.")
        return
    password = secrets.token_urlsafe(36)
    values = {
        "QT_V4_PORT": "8890", "QT_V4_TENANT": "personal",
        "QT_V4_POSTGRES_PASSWORD": password,
        "QT_V4_DATABASE_URL": f"postgresql+psycopg://qt_v4_app:{password}@postgres:5432/qt_v4",
        "QT_V4_SERVICE_TOKEN": secrets.token_urlsafe(36),
        "QT_V4_SIGNING_KEY": secrets.token_urlsafe(48),
        "QT_V4_TEST_DB_PORT": "55439",
        "QT_V4_TEST_POSTGRES_PASSWORD": secrets.token_urlsafe(36),
        "QT_V4_MODEL_ID": "", "QT_V4_MODEL_ORIGIN": "http://host.docker.internal:8080",
        "QT_V4_DEPLOYMENT_ID": "", "QT_EXPERIMENT_IMAGE_DIGEST": "",
        "QT_V4_SYMBOL": "BTCUSDT", "QT_V4_BAR_INTERVAL": "1m",
    }
    validate(values)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for key, value in values.items():
            stream.write(f"{key}={shlex.quote(value)}\n")
    print("Private V4 configuration created. Model and funded deployment remain unconfigured.")


def validate(values: dict[str, str]) -> None:
    names = ("QT_V4_POSTGRES_PASSWORD", "QT_V4_SERVICE_TOKEN", "QT_V4_SIGNING_KEY",
             "QT_V4_TEST_POSTGRES_PASSWORD")
    if any(len(values.get(name, "")) < 32 for name in names):
        raise ValueError("V4 independent database, service and signing settings are required")
    if len({values[name] for name in names}) != len(names):
        raise ValueError("V4 secret settings must be independent")
    parsed = urlsplit(values.get("QT_V4_DATABASE_URL", ""))
    if (parsed.scheme != "postgresql+psycopg" or parsed.hostname != "postgres"
            or parsed.port != 5432 or parsed.username != "qt_v4_app" or parsed.path != "/qt_v4"
            or parsed.password != values["QT_V4_POSTGRES_PASSWORD"] or parsed.query or parsed.fragment):
        raise ValueError("V4 database URL must identify this stack's private PostgreSQL service")
    for name in ("QT_V4_PORT", "QT_V4_TEST_DB_PORT"):
        if not values.get(name, "").isdigit() or not 1024 <= int(values[name]) <= 65535:
            raise ValueError("V4 loopback service ports are invalid")
    if values["QT_V4_PORT"] == values["QT_V4_TEST_DB_PORT"]:
        raise ValueError("V4 service ports must be distinct")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "check"])
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.env_file)
    else:
        validate(read_private(args.env_file))
        print("Private V4 configuration valid.")


if __name__ == "__main__":
    main()
