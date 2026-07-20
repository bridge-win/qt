from __future__ import annotations

import argparse
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO

Writer = Callable[[IO[str], str], None]
SecretFactory = Callable[[], str]


def _write_all(stream: IO[str], content: str) -> None:
    stream.write(content)


def _replace_assignments(content: str, replacements: dict[str, str]) -> str:
    pending = replacements.copy()
    rendered: list[str] = []
    for line in content.splitlines(keepends=True):
        key, separator, _value = line.partition("=")
        if separator and key in pending:
            newline = "\n" if line.endswith("\n") else ""
            rendered.append(f"{key}={pending.pop(key)}{newline}")
        else:
            rendered.append(line)
    if pending:
        missing = ", ".join(sorted(pending))
        raise ValueError(f"environment template is missing: {missing}")
    return "".join(rendered)


def _exclusive_write(path: Path, content: str, writer: Writer) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = None
            writer(stream, content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            path.unlink(missing_ok=True)
        raise


def create_platform_env(
    *,
    template: Path,
    output: Path,
    environment: str,
    image_reference: str,
    secret_factory: SecretFactory = lambda: secrets.token_urlsafe(48),
    writer: Writer = _write_all,
) -> None:
    password = secret_factory()
    if not password or any(character in password for character in ":@/\n\r"):
        raise ValueError("generated password is not URL-safe")
    content = template.read_text(encoding="utf-8")
    rendered = _replace_assignments(
        content,
        {
            "QT_PLATFORM_ENV": environment,
            "POSTGRES_PASSWORD": password,
            "QT_DATABASE_URL": (
                "postgresql+psycopg://qt_platform:"
                f"{password}@postgres:5432/qt_platform"
            ),
            "QT_PLATFORM_IMAGE": image_reference,
        },
    )
    _exclusive_write(output, rendered, writer)


def set_image_reference(*, output: Path, image_reference: str) -> None:
    content = output.read_text(encoding="utf-8")
    rendered = _replace_assignments(content, {"QT_PLATFORM_IMAGE": image_reference})
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(8)}.tmp")
    try:
        _exclusive_write(temporary, rendered, _write_all)
        os.replace(temporary, output)
        os.chmod(output, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or atomically update a protected platform environment file."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--template", type=Path, default=Path(".env.platform.example"))
    create.add_argument("--output", type=Path, default=Path(".env.platform"))
    create.add_argument(
        "--environment", choices=("staging", "production"), required=True
    )
    create.add_argument("--image-reference", required=True)

    set_image = subparsers.add_parser("set-image")
    set_image.add_argument("--output", type=Path, default=Path(".env.platform"))
    set_image.add_argument("--image-reference", required=True)
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            create_platform_env(
                template=arguments.template,
                output=arguments.output,
                environment=arguments.environment,
                image_reference=arguments.image_reference,
            )
        else:
            set_image_reference(
                output=arguments.output,
                image_reference=arguments.image_reference,
            )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Updated protected environment file: {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
