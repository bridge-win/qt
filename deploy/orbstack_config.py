"""User-run private setup and validation. Never import or print secret values."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import stat
import subprocess
import sys
import warnings
from pathlib import Path

KEYS = (
    "QT_ORBSTACK_PORT", "QT_ORBSTACK_USERNAME", "QT_ORBSTACK_PASSWORD_HASH",
    "QT_WORKBENCH_ORIGIN_CLIENT_ID", "QT_WORKBENCH_ORIGIN_CLIENT_SECRET",
    "QT_BTC_MONITOR_VENUE", "QT_BTC_MONITOR_ENVIRONMENT",
    "QT_TELEGRAM_BOT_TOKEN", "QT_TELEGRAM_CHAT_ID",
)
PAIRS = {("binance", "production"), ("binance", "testnet"), ("okx", "production"), ("okx", "demo")}


class ConfigError(Exception):
    """A fixed error that never includes a private value or subprocess output."""


def read_private(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ConfigError("Create a regular private configuration file with the init command first.")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError("The configuration must belong to you and be private: run chmod 600 on it.")
    if info.st_size > 65_536:
        raise ConfigError("The private configuration file is unexpectedly large.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in KEYS or key in values:
            raise ConfigError("Use only the documented keys, once each, in the private configuration.")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif "'" in value or '"' in value or "$" in value:
            raise ConfigError("Use plain values or single-quoted values without embedded quotes.")
        values[key] = value
    return values


def validate(values: dict[str, str]) -> None:
    port = values.get("QT_ORBSTACK_PORT", "8877")
    if not port.isdecimal() or not 1024 <= int(port) <= 65535:
        raise ConfigError("The local port must be between 1024 and 65535.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", values.get("QT_ORBSTACK_USERNAME", "")):
        raise ConfigError("Set a local username using letters, digits, underscore or hyphen.")
    if not re.fullmatch(r"\$2[aby]\$(1[0-9]|2[0-9]|3[01])\$[./A-Za-z0-9]{53}", values.get("QT_ORBSTACK_PASSWORD_HASH", "")):
        raise ConfigError("Set the local Caddy bcrypt password hash using the init command.")
    for key in ("QT_WORKBENCH_ORIGIN_CLIENT_ID", "QT_WORKBENCH_ORIGIN_CLIENT_SECRET"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", values.get(key, "")):
            raise ConfigError("A fresh local origin-auth pair is required; use init.")
    pair = (values.get("QT_BTC_MONITOR_VENUE", "binance"), values.get("QT_BTC_MONITOR_ENVIRONMENT", "production"))
    if pair not in PAIRS:
        raise ConfigError("Unsupported public monitor venue/environment pair. See the example file.")
    token, chat = values.get("QT_TELEGRAM_BOT_TOKEN", ""), values.get("QT_TELEGRAM_CHAT_ID", "")
    if bool(token) != bool(chat):
        raise ConfigError("Telegram token and recipient chat ID must be configured together or both left blank.")
    if token and not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
        raise ConfigError("Telegram token format is invalid; enter a new token locally.")
    if chat and not re.fullmatch(r"-?[0-9]{1,20}", chat):
        raise ConfigError("Telegram chat ID must be the numeric recipient ID discovered locally.")


def initialize(path: Path, context: str) -> None:
    if path.exists() or path.is_symlink():
        raise ConfigError("The private configuration already exists. Edit it locally; init never overwrites it.")
    if not sys.stdin.isatty():
        raise ConfigError("Run init yourself in an interactive terminal. No credentials were created.")
    print(
        "This creates a private local Workbench login and fresh internal origin credentials. "
        "It does not set trading keys or Telegram credentials."
    )
    if input("Create this local login now? [y/N] ").strip().lower() != "y":
        raise ConfigError("Setup cancelled. No credentials were created.")
    username = input("Local username [qt]: ").strip() or "qt"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", username):
        raise ConfigError("Use letters, digits, underscore or hyphen for the local username.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("New local password (hidden, at least 16 characters): ")
    if len(password) < 16 or len(password.encode("utf-8")) > 72 or "\n" in password or "\r" in password:
        raise ConfigError("Use at least 16 characters and at most 72 UTF-8 bytes, without line breaks.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        if getpass.getpass("Repeat local password (hidden): ") != password:
            raise ConfigError("Passwords did not match. No credentials were saved.")
    # The user invokes setup. Password is stdin only, never an argument/env/log.
    result = subprocess.run(
        ["docker", "--context", context, "run", "--rm", "-i", "--network", "none",
         "caddy:2.11.7-alpine", "caddy", "hash-password"],
        input=password + "\n", text=True, capture_output=True, check=False, timeout=120,
    )
    password_hash = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"\$2[aby]\$14\$[./A-Za-z0-9]{53}", password_hash):
        raise ConfigError("Local password hashing failed. Check OrbStack; no credentials were saved.")
    values = {
        "QT_ORBSTACK_PORT": "8877", "QT_ORBSTACK_USERNAME": username,
        "QT_ORBSTACK_PASSWORD_HASH": password_hash,
        "QT_WORKBENCH_ORIGIN_CLIENT_ID": secrets.token_urlsafe(32),
        "QT_WORKBENCH_ORIGIN_CLIENT_SECRET": secrets.token_urlsafe(48),
        "QT_BTC_MONITOR_VENUE": "binance", "QT_BTC_MONITOR_ENVIRONMENT": "production",
        "QT_TELEGRAM_BOT_TOKEN": "", "QT_TELEGRAM_CHAT_ID": "",
    }
    validate(values)
    content = "# Private local QT settings. Never share or commit this file.\n" + "".join(
        f"{key}='{value}'\n" for key, value in values.items()
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    print("Private local login saved. External tokens remain blank; you can now run the up command.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "check", "port"])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--context", default="orbstack")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.env_file, args.context)
        else:
            values = read_private(args.env_file)
            validate(values)
            if args.command == "port":
                print(values.get("QT_ORBSTACK_PORT", "8877"))
        return 0
    except (EOFError, KeyboardInterrupt):
        print("Setup cancelled.", file=sys.stderr)
        return 1
    except (ConfigError, OSError, subprocess.SubprocessError, getpass.GetPassWarning):
        # Avoid rendering exception chains, paths, subprocess output or values.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, ConfigError) else "Local setup failed. No secret output was displayed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
