"""Explicit R2 publication for immutable native research artifacts.

The Worker serves only private ``reports/`` keys.  A local artifact is never
advertised as downloadable until this publisher has completed the object-store
write and recorded the exact immutable key in the result summary.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, TypeAlias, cast

JsonDict: TypeAlias = dict[str, object]


class ObjectStoreClient(Protocol):
    def upload_fileobj(
        self, fileobj: BinaryIO, bucket: str, key: str, extra_args: Mapping[str, object]
    ) -> None: ...


class ReportPublicationError(RuntimeError):
    """An R2 upload could not be completed and must remain visibly unpublished."""


@dataclass(frozen=True)
class R2ReportSettings:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_environment(cls) -> R2ReportSettings | None:
        values = {
            "endpoint_url": os.environ.get("QT_R2_ENDPOINT_URL", "").strip(),
            "bucket": os.environ.get("QT_R2_REPORTS_BUCKET", "").strip(),
            "access_key_id": os.environ.get("QT_R2_ACCESS_KEY_ID", "").strip(),
            "secret_access_key": os.environ.get("QT_R2_SECRET_ACCESS_KEY", "").strip(),
        }
        if not any(values.values()):
            return None
        if not all(values.values()):
            raise ReportPublicationError("QT_R2 report publication credentials are incomplete")
        return cls(**values)


class R2ReportPublisher:
    """Publish completed local report files to the private R2 bucket only."""

    def __init__(
        self,
        settings: R2ReportSettings | None,
        *,
        artifact_root: Path,
        client_factory: Callable[[R2ReportSettings], ObjectStoreClient] | None = None,
    ) -> None:
        self.settings = settings
        self.artifact_root = artifact_root.resolve()
        self._client_factory = client_factory or _boto_client

    @property
    def configured(self) -> bool:
        return self.settings is not None

    def publish_result(self, result: Mapping[str, object]) -> JsonDict:
        """Copy verified artifacts and return an immutable publication manifest.

        Missing R2 configuration is a normal partial deployment state.  It is
        represented in every artifact record, never converted into a URL.
        """

        published = dict(result)
        run_id = _safe_run_id(_required_text(result, "run_id"))
        plugin_result = isinstance(result.get("plugin_runtime"), Mapping)
        artifacts = result.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ReportPublicationError("native result artifacts must be a list")
        records: list[JsonDict] = []
        names: set[str] = set()
        if self.settings is None:
            for artifact in artifacts:
                record = self._attest_artifact(
                    artifact, run_id, "r2_not_configured", plugin_result=plugin_result
                )
                _remember_name(names, _required_text(record, "name"))
                record.pop("_attested_path", None)
                records.append(record)
            published["artifacts"] = records
            published["report_publication"] = {"status": "not_configured", "bucket": None}
            return published
        client = self._client_factory(self.settings)
        for artifact in artifacts:
            record = self._attest_artifact(artifact, run_id, "pending", plugin_result=plugin_result)
            _remember_name(names, _required_text(record, "name"))
            path = Path(_required_text(record, "_attested_path"))
            key = f"reports/{run_id}/{_safe_name(_required_text(record, 'name'))}"
            try:
                with _open_verified_upload_file(
                    path,
                    expected_sha256=_required_text(record, "sha256"),
                    expected_size=_required_size(record),
                ) as handle:
                    client.upload_fileobj(
                        handle,
                        self.settings.bucket,
                        key,
                        {
                            "ContentType": str(record.get("media_type", "application/octet-stream")),
                            "Metadata": {"sha256": str(record.get("sha256", "")), "run-id": run_id},
                        },
                    )
            except Exception as error:
                raise ReportPublicationError(f"R2 upload failed for {record['name']}: {error}") from error
            record.pop("_attested_path", None)
            record.update({"publication_status": "published", "r2_key": key})
            records.append(record)
        published["artifacts"] = records
        published["report_publication"] = {
            "status": "published",
            "bucket": self.settings.bucket,
            "prefix": f"reports/{run_id}/",
        }
        return published

    def _attest_artifact(
        self,
        value: object,
        run_id: str,
        status: str,
        *,
        plugin_result: bool,
    ) -> JsonDict:
        """Allow only worker-created regular files beneath the configured root."""

        descriptor = _artifact_descriptor(value)
        name = _safe_name(_required_text(descriptor, "name"))
        source_path = _required_text(descriptor, "path")
        path = self._attested_path(source_path, run_id=run_id, plugin_result=plugin_result)
        expected = _required_text(descriptor, "sha256")
        if not _sha256_hex(expected):
            raise ReportPublicationError(f"artifact {name} has an invalid SHA-256 declaration")
        actual, actual_size = _sha256_file(path)
        if not hmac.compare_digest(actual, expected):
            raise ReportPublicationError(f"artifact {name} SHA-256 does not match the native result")
        declared_size = descriptor.get("size_bytes")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
            raise ReportPublicationError(f"artifact {name} has no valid size declaration")
        if declared_size != actual_size:
            raise ReportPublicationError(f"artifact {name} size does not match the native result")
        rows = descriptor.get("rows")
        if rows is not None and (not isinstance(rows, int) or isinstance(rows, bool) or rows < 0):
            raise ReportPublicationError(f"artifact {name} has an invalid row count")
        # Do not forward arbitrary plugin JSON or a host path into SQLite/API
        # output.  The private R2 key is the sole public artifact identity.
        record: JsonDict = {
            "name": name,
            "sha256": actual,
            "size_bytes": actual_size,
            "media_type": _safe_media_type(descriptor.get("media_type")),
            "rows": rows,
            "publication_status": status,
            "r2_key": None,
            "_attested_path": str(path),
        }
        return record

    def _attested_path(self, supplied: str, *, run_id: str, plugin_result: bool) -> Path:
        candidate = Path(supplied)
        if candidate.is_absolute():
            if plugin_result:
                raise ReportPublicationError("plugin artifact path must be a server-derived relative path")
            lexical = Path(os.path.abspath(candidate))
        else:
            if not plugin_result:
                raise ReportPublicationError("native artifact path must be absolute")
            if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
                raise ReportPublicationError("plugin artifact path is not safe")
            lexical = self.artifact_root / "plugins" / run_id / candidate
        try:
            relative = lexical.relative_to(self.artifact_root)
        except ValueError as error:
            raise ReportPublicationError("artifact path escapes configured artifact root") from error
        current = self.artifact_root
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as error:
                raise ReportPublicationError("artifact path is unavailable") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ReportPublicationError("artifact path may not traverse a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise ReportPublicationError("artifact path must be a regular file")
        resolved = current.resolve(strict=True)
        if self.artifact_root != resolved and self.artifact_root not in resolved.parents:
            raise ReportPublicationError("artifact path escapes configured artifact root")
        if plugin_result:
            expected = ("plugins", run_id, "artifacts", run_id)
            if relative.parts[:4] != expected:
                raise ReportPublicationError(f"plugin artifact does not belong to run {run_id}")
        elif relative.parts[:1] != (run_id,):
            raise ReportPublicationError(f"artifact does not belong to run {run_id}")
        return resolved


def _boto_client(settings: R2ReportSettings) -> ObjectStoreClient:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - depends on deployment extra
        raise ReportPublicationError("boto3 is required for configured R2 report publication") from error
    return cast(ObjectStoreClient, boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
    ))


def _artifact_descriptor(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReportPublicationError("native artifact must be an object")
    return value


def _required_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ReportPublicationError(f"artifact {key} is required")
    return item


def _safe_name(value: str) -> str:
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        raise ReportPublicationError("artifact name is not safe for an R2 key")
    return value


def _safe_run_id(value: str) -> str:
    if not value or len(value) > 128 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value):
        raise ReportPublicationError("run ID is not safe for an R2 key")
    return value


def _safe_media_type(value: object) -> str:
    if isinstance(value, str) and value in {
        "application/json",
        "text/csv",
        "application/vnd.apache.parquet",
    }:
        return value
    return "application/octet-stream"


def _sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def _sha256_file(path: Path) -> tuple[str, int]:
    descriptor = _open_regular_descriptor(path)
    try:
        return _hash_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _open_verified_upload_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> BinaryIO:
    """Return the exact descriptor whose bytes were just re-attested for upload."""

    descriptor = _open_regular_descriptor(path)
    try:
        actual_sha256, actual_size = _hash_descriptor(descriptor)
        if actual_size != expected_size or not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ReportPublicationError("artifact changed after initial attestation")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _open_regular_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        return os.open(path, flags)
    except OSError as error:
        raise ReportPublicationError("artifact cannot be opened safely") from error


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ReportPublicationError("artifact must be a non-hard-linked regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(descriptor)
    if _file_stamp(before) != _file_stamp(after):
        raise ReportPublicationError("artifact changed while being attested")
    return digest.hexdigest(), after.st_size


def _file_stamp(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _required_size(value: Mapping[str, object]) -> int:
    size = value.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ReportPublicationError("artifact size is required")
    return size


def _remember_name(seen: set[str], name: str) -> None:
    if name in seen:
        raise ReportPublicationError(f"duplicate artifact name for immutable report: {name}")
    seen.add(name)
