# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .schemas import ProviderHttpError, ProviderRateLimitError


def read_json(
    url: str,
    *,
    provider: str,
    timeout_seconds: int,
    headers: dict[str, str] | None = None,
) -> object:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - provider controls validated URL
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        retry_after = _retry_after_seconds(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        if exc.code == 429:
            raise ProviderRateLimitError(
                provider,
                retry_after_seconds=retry_after,
            ) from None
        raise ProviderHttpError(
            provider,
            exc.code,
            retry_after_seconds=retry_after,
        ) from None


def post_json(
    url: str,
    *,
    provider: str,
    timeout_seconds: int,
    headers: dict[str, str],
    payload: dict[str, object],
) -> object:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed provider URL
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        retry_after = _retry_after_seconds(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        if exc.code == 429:
            raise ProviderRateLimitError(
                provider,
                retry_after_seconds=retry_after,
            ) from None
        raise ProviderHttpError(
            provider,
            exc.code,
            retry_after_seconds=retry_after,
        ) from None


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
        seconds = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds

