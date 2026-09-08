# Migrated with Python 3.10 UTC compatibility from btc_quant_evolution; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlencode

import pandas as pd
from pandas import DataFrame

from qt.legacy.btc_quant_evolution.external_data.http import read_json
from qt.legacy.btc_quant_evolution.external_data.schemas import ProviderHttpError

_STEP = timedelta(hours=4)
_BITSTAMP_ENDPOINT = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
_COLUMNS = ("date", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class ProxyHistoryRequest:
    start: datetime
    end: datetime
    binance_path: Path
    output_dir: Path

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("proxy history timestamps must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("proxy history start must be before end")
        if not _on_four_hour_grid(self.start) or not _on_four_hour_grid(self.end):
            raise ValueError("proxy history window must align to the 4h timezone.utc grid")


@dataclass(frozen=True)
class ProxyHistoryResult:
    data_path: Path
    manifest_path: Path


BitstampFetcher = Callable[[datetime, datetime], list[dict[str, str]]]


def build_btc_market_proxy(
    *,
    request: ProxyHistoryRequest,
    fetch_bitstamp: BitstampFetcher | None = None,
) -> ProxyHistoryResult:
    if not request.binance_path.is_file():
        raise ValueError(f"missing Binance BTC/USDT data: {request.binance_path}")
    if request.binance_path.name != "BTC_USDT-4h.feather" or request.binance_path.parent.name != "binance":
        raise ValueError("binance_path must be the exact Binance source path binance/BTC_USDT-4h.feather")
    data_path = request.output_dir / "BTC_USDT-4h.feather"
    if data_path.resolve() == request.binance_path.resolve() or (
        data_path.exists() and data_path.samefile(request.binance_path)
    ):
        raise ValueError("proxy output must not overwrite exact Binance data")
    binance_sha256 = _sha256(request.binance_path)

    binance = _normalize_frame(pd.read_feather(request.binance_path), source="Binance")
    if binance.empty:
        raise ValueError("Binance BTC/USDT data is empty")
    transition = max(request.start, binance["date"].min().to_pydatetime())
    if transition >= request.end:
        raise ValueError("requested proxy window does not include Binance BTC/USDT history")
    if not _on_four_hour_grid(transition):
        raise ValueError("Binance transition does not align to the 4h timezone.utc grid")

    fetcher = fetch_bitstamp or fetch_bitstamp_ohlcv
    bitstamp = _normalize_bitstamp(fetcher(request.start, transition), request.start, transition)
    exact_binance = binance[(binance["date"] >= transition) & (binance["date"] < request.end)].copy()
    combined = pd.concat([bitstamp, exact_binance], ignore_index=True)
    combined = combined.sort_values("date", kind="stable").reset_index(drop=True)
    fallback_frames = []
    for gap_start, gap_end in _contiguous_gaps(_missing_grid(combined, transition, request.end)):
        fallback_frames.append(
            _normalize_bitstamp(fetcher(gap_start, gap_end), gap_start, gap_end)
        )
    fallback = (
        pd.concat(fallback_frames, ignore_index=True)
        if fallback_frames
        else DataFrame(columns=_COLUMNS)
    )
    if not fallback.empty:
        combined = pd.concat([combined, fallback], ignore_index=True)
        combined = combined.sort_values("date", kind="stable").reset_index(drop=True)
    _validate_complete_grid(combined, request.start, request.end)

    request.output_dir.mkdir(parents=True, exist_ok=True)
    combined.loc[:, _COLUMNS].to_feather(data_path)
    manifest_path = request.output_dir / "BTC_USDT-4h.proxy-manifest.json"
    manifest = {
        "schema_version": 1,
        "dataset": "btc_market_proxy_4h",
        "pair_contract": "BTC/USD before transition; BTC/USDT where available; BTC/USD for enumerated gaps",
        "research_only": True,
        "production_eligible": False,
        "production_rejection_reason": (
            "pre-transition candles use Bitstamp BTC/USD and are not exact BTC/USDT observations"
        ),
        "start": request.start.astimezone(timezone.utc).isoformat(),
        "end_exclusive": request.end.astimezone(timezone.utc).isoformat(),
        "transition_at": transition.astimezone(timezone.utc).isoformat(),
        "row_count": len(combined),
        "expected_row_count": _expected_count(request.start, request.end),
        "data_sha256": _sha256(data_path),
        "sources": {
            "pre_transition": {
                "provider": "bitstamp_public",
                "pair": "BTC/USD",
                "timeframe": "4h",
                "endpoint": _BITSTAMP_ENDPOINT,
                "row_count": len(bitstamp),
                "credentials_required": False,
            },
            "post_transition": {
                "provider": "binance_local",
                "pair": "BTC/USDT",
                "timeframe": "4h",
                "path": request.binance_path.as_posix(),
                "sha256": binance_sha256,
                "row_count": len(exact_binance),
                "gap_fallback_provider": "bitstamp_public",
                "gap_fallback_row_count": len(fallback),
                "gap_fallback_intervals": [timestamp.isoformat() for timestamp in fallback["date"]],
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return ProxyHistoryResult(data_path=data_path, manifest_path=manifest_path)


def fetch_bitstamp_ohlcv(start: datetime, end: datetime) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    cursor = start.astimezone(timezone.utc)
    while cursor < end:
        # Bitstamp returns at most 1000 rows and may pad a short final response
        # with older rows. Windowing plus strict post-filtering keeps pagination causal.
        window_end = min(end - _STEP, cursor + _STEP * 999)
        query = urlencode(
            {
                "step": int(_STEP.total_seconds()),
                "limit": 1000,
                "start": int(cursor.timestamp()),
                "end": int(window_end.timestamp()),
                "exclude_current_candle": "true",
            }
        )
        payload = _read_bitstamp_page(f"{_BITSTAMP_ENDPOINT}?{query}")
        if not isinstance(payload, dict):
            raise ValueError("Bitstamp OHLC response must be an object")
        data = payload.get("data")
        page = data.get("ohlc") if isinstance(data, dict) else None
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ValueError("Bitstamp OHLC response is missing data.ohlc")
        rows.extend(page)
        cursor = window_end + _STEP
    return rows


def _read_bitstamp_page(url: str) -> object:
    for attempt in range(3):
        try:
            return read_json(
                url,
                provider="bitstamp_public",
                timeout_seconds=30,
                headers={"User-Agent": "btc-quant/1.0"},
            )
        except ProviderHttpError as exc:
            if exc.status_code != 429 and exc.status_code < 500:
                raise
            if attempt == 2:
                raise
            sleep(exc.retry_after_seconds or float(2**attempt))
        except URLError:
            if attempt == 2:
                raise
            sleep(float(2**attempt))
    raise RuntimeError("Bitstamp retry loop terminated unexpectedly")


def _normalize_bitstamp(rows: list[dict[str, str]], start: datetime, end: datetime) -> DataFrame:
    records = []
    for row in rows:
        try:
            records.append(
                {
                    "date": pd.to_datetime(int(row["timestamp"]), unit="s", utc=True),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Bitstamp OHLC row is malformed") from exc
    frame = _normalize_frame(DataFrame.from_records(records, columns=_COLUMNS), source="Bitstamp")
    frame = frame[(frame["date"] >= start) & (frame["date"] < end)].copy()
    frame = frame.drop_duplicates(subset="date", keep="last").reset_index(drop=True)
    _validate_complete_grid(frame, start, end)
    return frame


def _normalize_frame(frame: DataFrame, *, source: str) -> DataFrame:
    missing = sorted(set(_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"{source} OHLCV is missing columns: {', '.join(missing)}")
    result = frame.loc[:, _COLUMNS].copy()
    result["date"] = pd.to_datetime(result["date"], utc=True, errors="coerce")
    for column in _COLUMNS[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if result.isna().any(axis=None):
        raise ValueError(f"{source} OHLCV contains invalid values")
    invalid = (
        (result["open"] <= 0)
        | (result["high"] <= 0)
        | (result["low"] <= 0)
        | (result["close"] <= 0)
        | (result["volume"] < 0)
        | (result["high"] < result[["open", "close", "low"]].max(axis=1))
        | (result["low"] > result[["open", "close", "high"]].min(axis=1))
    )
    if invalid.any():
        raise ValueError(f"{source} OHLCV contains invalid candles")
    return result.sort_values("date", kind="stable").reset_index(drop=True)


def _validate_complete_grid(frame: DataFrame, start: datetime, end: datetime) -> None:
    if frame["date"].duplicated().any():
        raise ValueError("proxy history contains duplicate 4h intervals")
    actual = set(frame["date"].tolist())
    expected = set(pd.date_range(start, end, freq="4h", inclusive="left").tolist())
    missing = expected - actual
    unexpected = actual - expected
    if missing:
        raise ValueError(f"proxy history is missing {len(missing)} expected 4h intervals")
    if unexpected:
        raise ValueError(f"proxy history contains {len(unexpected)} unexpected 4h intervals")


def _missing_grid(frame: DataFrame, start: datetime, end: datetime) -> list[datetime]:
    actual = set(frame["date"].tolist())
    expected = pd.date_range(start, end, freq="4h", inclusive="left")
    return [timestamp.to_pydatetime() for timestamp in expected if timestamp not in actual]


def _contiguous_gaps(missing: list[datetime]) -> list[tuple[datetime, datetime]]:
    if not missing:
        return []
    groups = []
    group_start = previous = missing[0]
    for timestamp in missing[1:]:
        if timestamp != previous + _STEP:
            groups.append((group_start, previous + _STEP))
            group_start = timestamp
        previous = timestamp
    groups.append((group_start, previous + _STEP))
    return groups


def _expected_count(start: datetime, end: datetime) -> int:
    return int((end - start) / _STEP)


def _on_four_hour_grid(value: datetime) -> bool:
    utc = value.astimezone(timezone.utc)
    return utc.minute == 0 and utc.second == 0 and utc.microsecond == 0 and utc.hour % 4 == 0


def _parse_cli_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a research-only BTC/USD to BTC/USDT 4h proxy history.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--binance-file", type=Path, default=Path("user_data/data/binance/BTC_USDT-4h.feather"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("user_data/research_data/btc-market-proxy"),
    )
    args = parser.parse_args()
    result = build_btc_market_proxy(
        request=ProxyHistoryRequest(
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            binance_path=args.binance_file,
            output_dir=args.output_dir,
        )
    )
    print(f"data={result.data_path} manifest={result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

