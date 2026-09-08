# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Historical Binance 1m K-line/funding backfill and local loaders."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pandas as pd

from .binance_rest import BinanceRest

log = logging.getLogger(__name__)


def _read_dataset(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    files = sorted(path.glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=columns)
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception as exc:
            log.warning("skip unreadable %s: %s", f, exc)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")


def _read_live_dataset(data_dir: Path, table: str, columns: list[str],
                       max_days: int = 60) -> pd.DataFrame:
    root = Path(data_dir) / "live"
    if not root.exists():
        return pd.DataFrame(columns=columns)
    frames = []
    for day in sorted((d for d in root.iterdir() if d.is_dir()))[-max_days:]:
        for file in sorted(day.glob(f"{table}-*.parquet")):
            try:
                frame = pd.read_parquet(file)
                frames.append(frame[[c for c in columns if c in frame.columns]])
            except Exception as exc:
                log.warning("skip unreadable %s: %s", file, exc)
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")


def load_klines(data_dir: Path, start_ms: int | None = None,
                end_ms: int | None = None) -> pd.DataFrame:
    df = _read_dataset(Path(data_dir) / "history" / "klines_1m",
                       ["ts", "open", "high", "low", "close", "volume"])
    live = _read_live_dataset(Path(data_dir), "klines",
                              ["ts", "open", "high", "low", "close", "volume"])
    if not live.empty:
        df = pd.concat([df, live], ignore_index=True).drop_duplicates("ts").sort_values("ts")
    if start_ms is not None:
        df = df[df.ts >= start_ms]
    if end_ms is not None:
        df = df[df.ts <= end_ms]
    return df.reset_index(drop=True)


def load_funding(data_dir: Path) -> pd.DataFrame:
    historical = _read_dataset(Path(data_dir) / "history" / "funding", ["ts", "rate"])
    live = _read_live_dataset(Path(data_dir), "funding", ["ts", "rate"])
    frames = [x for x in (historical, live) if not x.empty]
    if not frames:
        return pd.DataFrame(columns=["ts", "rate"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")


def load_oi(data_dir: Path) -> pd.DataFrame:
    hist = _read_dataset(Path(data_dir) / "history" / "oi", ["ts", "oi"])
    live_root = Path(data_dir) / "live"
    frames = [hist] if not hist.empty else []
    if live_root.exists():
        for f in sorted(live_root.glob("*/oi-*.parquet")):
            try:
                frames.append(pd.read_parquet(f)[["ts", "oi"]])
            except Exception:
                pass
    if not frames:
        return pd.DataFrame(columns=["ts", "oi"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")


async def _backfill(symbol: str, root: Path, since: str):
    start = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
    end = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    kdir, fdir = root / "history" / "klines_1m", root / "history" / "funding"
    kdir.mkdir(parents=True, exist_ok=True); fdir.mkdir(parents=True, exist_ok=True)
    async with BinanceRest() as rest:
        cursor, chunk = start, 0
        while cursor < end:
            rows = await rest.klines(symbol, start_time=cursor, end_time=end, limit=1500)
            if not rows:
                break
            df = pd.DataFrame([{ "ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                                 "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                               for r in rows])
            df.to_parquet(kdir / f"part-{chunk:06d}.parquet", index=False)
            cursor = int(df.ts.iloc[-1]) + 60_000; chunk += 1
            if chunk % 100 == 0:
                log.info("backfill klines through %s", pd.Timestamp(cursor, unit="ms", tz="UTC"))
            await asyncio.sleep(0.05)
        cursor, chunk = start, 0
        while cursor < end:
            rows = await rest.funding_history(symbol, start_time=cursor, end_time=end, limit=1000)
            if not rows:
                break
            df = pd.DataFrame([{"ts": int(r["fundingTime"]), "rate": float(r["fundingRate"])}
                               for r in rows])
            df.to_parquet(fdir / f"part-{chunk:04d}.parquet", index=False)
            cursor = int(df.ts.iloc[-1]) + 1; chunk += 1
            await asyncio.sleep(0.05)


async def download_range(symbol: str, root: Path, start_ms: int, end_ms: int) -> None:
    """Append a bounded kline/funding range. Existing timestamps are deduplicated by loaders."""
    root = Path(root)
    kdir, fdir = root / "history" / "klines_1m", root / "history" / "funding"
    kdir.mkdir(parents=True, exist_ok=True)
    fdir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    async with BinanceRest() as rest:
        cursor, chunk = start_ms, 0
        while cursor <= end_ms:
            rows = await rest.klines(symbol, start_time=cursor, end_time=end_ms, limit=1500)
            if not rows:
                break
            frame = pd.DataFrame(
                ({"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                  "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                 for r in rows))
            frame.drop_duplicates("ts").to_parquet(
                kdir / f"range-{start_ms}-{end_ms}-{stamp}-{chunk:05d}.parquet", index=False)
            chunk += 1
            new_cursor = int(rows[-1][0]) + 60_000
            if new_cursor <= cursor:
                break
            cursor = new_cursor
            await asyncio.sleep(0.03)

        cursor, rows_out = start_ms, []
        while cursor <= end_ms:
            rows = await rest.funding_history(symbol, start_time=cursor,
                                               end_time=end_ms, limit=1000)
            if not rows:
                break
            rows_out.extend({"ts": int(r["fundingTime"]), "rate": float(r["fundingRate"])}
                            for r in rows)
            new_cursor = int(rows[-1]["fundingTime"]) + 1
            if new_cursor <= cursor:
                break
            cursor = new_cursor
            await asyncio.sleep(0.03)
        if rows_out:
            pd.DataFrame(rows_out).drop_duplicates("ts").to_parquet(
                fdir / f"range-{start_ms}-{end_ms}-{stamp}.parquet", index=False)


def run_backfill(symbol: str, data_dir: Path, since: str):
    asyncio.run(_backfill(symbol, Path(data_dir), since))

