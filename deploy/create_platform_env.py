from __future__ import annotations

import argparse
import ipaddress
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO

Writer = Callable[[IO[str], str], None]
SecretFactory = Callable[[], str]

_ASSIGNMENT_PATTERN = re.compile(r"([A-Z][A-Z0-9_]*)=([^\r\n]*)")
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_PATH_COMPONENT_PATTERN = re.compile(
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
)
_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DOMAIN_LABEL_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)
_LOCAL_TAG_ALLOWLIST = frozenset({"qt-platform:local"})


def _write_all(stream: IO[str], content: str) -> None:
    stream.write(content)


def _parse_environment(content: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid environment assignment on line {line_number}")
        key, value = match.groups()
        if key in assignments:
            raise ValueError(f"duplicate environment assignment: {key}")
        assignments[key] = value
    return assignments


def _validate_registry_port(port: str) -> None:
    if not port.isascii() or not port.isdigit():
        raise ValueError("invalid image registry port")
    if not 1 <= int(port) <= 65535:
        raise ValueError("invalid image registry port")


def _validate_registry(registry: str) -> None:
    if registry.startswith("["):
        closing_bracket = registry.find("]")
        if closing_bracket < 0:
            raise ValueError("invalid image registry host")
        address = registry[1:closing_bracket]
        suffix = registry[closing_bracket + 1 :]
        if not address or "%" in address:
            raise ValueError("invalid image registry host")
        try:
            ipaddress.IPv6Address(address)
        except ipaddress.AddressValueError as error:
            raise ValueError("invalid image registry host") from error
        if suffix:
            if not suffix.startswith(":"):
                raise ValueError("invalid image registry host")
            _validate_registry_port(suffix[1:])
        return

    if "[" in registry or "]" in registry or registry.count(":") > 1:
        raise ValueError("invalid image registry host")
    host = registry
    port: str | None = None
    if ":" in registry:
        host, port = registry.rsplit(":", maxsplit=1)
        _validate_registry_port(port)
    if not host or ".." in host:
        raise ValueError("invalid image registry host")
    if host.lower() != "localhost" and "." not in host and port is None:
        raise ValueError("image registry must be qualified")
    if any(_DOMAIN_LABEL_PATTERN.fullmatch(label) is None for label in host.split(".")):
        raise ValueError("invalid image registry host")


def _validate_repository_name(name: str) -> bool:
    if not name or len(name) > 255:
        raise ValueError("invalid image repository name")
    components = name.split("/")
    if any(not component for component in components):
        raise ValueError("invalid image repository name")
    registry_qualified = len(components) >= 2 and (
        "." in components[0] or ":" in components[0] or components[0] == "localhost"
    )
    path_components = components
    if registry_qualified:
        _validate_registry(components[0])
        path_components = components[1:]
    if any(
        _PATH_COMPONENT_PATTERN.fullmatch(component) is None
        for component in path_components
    ):
        raise ValueError("invalid image repository name")
    return registry_qualified


def validate_image_reference(image_reference: str, environment: str) -> None:
    if environment not in {"staging", "production"}:
        raise ValueError("platform environment must be staging or production")
    if not image_reference or len(image_reference) > 512:
        raise ValueError("invalid image reference")
    if any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        or character in "=#"
        for character in image_reference
    ):
        raise ValueError("image reference contains forbidden characters")

    name_and_tag, digest_separator, digest = image_reference.partition("@")
    if digest_separator and "@" in digest:
        raise ValueError("invalid image digest")
    if digest_separator and _DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("image digest must be sha256 with 64 lowercase hex characters")

    last_component = name_and_tag.rsplit("/", maxsplit=1)[-1]
    tag: str | None = None
    name = name_and_tag
    if ":" in last_component:
        name, tag = name_and_tag.rsplit(":", maxsplit=1)
        if _TAG_PATTERN.fullmatch(tag) is None:
            raise ValueError("invalid image tag")
    registry_qualified = _validate_repository_name(name)

    if digest_separator and tag is not None:
        raise ValueError("canonical digest references must not include a tag")
    if not digest_separator and tag is None:
        raise ValueError("image reference must include an explicit tag or digest")
    if environment == "production":
        if not digest_separator or not registry_qualified:
            raise ValueError(
                "production image must be a registry-qualified name@sha256 digest"
            )
        return
    if tag is not None and image_reference not in _LOCAL_TAG_ALLOWLIST:
        raise ValueError("staging mutable image tag is not allowlisted")


def _replace_assignments(content: str, replacements: dict[str, str]) -> str:
    _parse_environment(content)
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
    validate_image_reference(image_reference, environment)
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
    if not stat.S_ISREG(output.lstat().st_mode):
        raise ValueError("protected environment output must be a regular file")
    content = output.read_text(encoding="utf-8")
    assignments = _parse_environment(content)
    try:
        environment = assignments["QT_PLATFORM_ENV"]
        current_image = assignments["QT_PLATFORM_IMAGE"]
    except KeyError as error:
        raise ValueError(f"environment file is missing: {error.args[0]}") from error
    validate_image_reference(current_image, environment)
    validate_image_reference(image_reference, environment)
    rendered = _replace_assignments(content, {"QT_PLATFORM_IMAGE": image_reference})
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(8)}.tmp")
    try:
        _exclusive_write(temporary, rendered, _write_all)
        if not stat.S_ISREG(output.lstat().st_mode):
            raise ValueError("protected environment output must be a regular file")
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
