"""Binance USDⓈ-M forceOrder WebSocket recorder → hourly liquidation bars.

Why: every public liquidation feed is *sampled*. Since 2021-04 Binance
pushes at most one forceOrder per symbol per second, so aggregators see
a fraction of true volume. Self-recording still only sees that sample —
but you own the history, it is free, and the relative Z-score
(``qt.indicators.derivatives.liquidation_zscore``) is what the signal
uses, not the absolute level.

Writes ``data/parquet/derivatives/binance_BTCUSDT_liq_1h.parquet`` with
``long_liq_usd`` / ``short_liq_usd`` (forced SELL = long liquidated,
forced BUY = short liquidated). Run under systemd / the watchdog:

    python scripts/record_liquidations_ws.py --symbol btcusdt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from datetime import datetime, timezone

import pandas as pd

from qt.core.config import load_settings
from qt.core.logging import configure_logging, get_logger
from qt.data.store import ParquetStore

log = get_logger(__name__)
WS = "wss://fstream.binance.com/ws/{symbol}@forceOrder"


class HourlyAgg:
    def __init__(self, store: ParquetStore, key: str, flush_seconds: int = 300) -> None:
        self.store, self.key, self.flush_seconds = store, key, flush_seconds
        self.buckets: dict[pd.Timestamp, dict[str, float]] = {}
        self._last_flush = datetime.now(tz=timezone.utc)

    def add(self, ts_ms: int, side: str, notional: float) -> None:
        hour = pd.Timestamp(ts_ms, unit="ms", tz="UTC").floor("h")
        b = self.buckets.setdefault(hour, {"long_liq_usd": 0.0, "short_liq_usd": 0.0})
        # forceOrder side is the *order* side: SELL = a long was liquidated
        b["long_liq_usd" if side == "SELL" else "short_liq_usd"] += notional

    def maybe_flush(self, force: bool = False) -> None:
        now = datetime.now(tz=timezone.utc)
        if not force and (now - self._last_flush).total_seconds() < self.flush_seconds:
            return
        if self.buckets:
            df = pd.DataFrame.from_dict(self.buckets, orient="index").sort_index()
            df.index.name = "ts"
            existing = self.store.read("derivatives", self.key)
            if not existing.empty:
                # merge additively for partially-written hours
                df = df.add(existing.reindex(df.index).fillna(0.0), fill_value=0.0)
            self.store.upsert("derivatives", self.key, df)
            done = [h for h in self.buckets if h < pd.Timestamp(now).floor("h")]
            for h in done:
                self.buckets.pop(h, None)
            log.info("liq_flushed", rows=len(df))
        self._last_flush = now


async def run(symbol: str, agg: HourlyAgg) -> None:
    import websockets

    url = WS.format(symbol=symbol.lower())
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("liq_ws_connected", url=url)
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    o = msg.get("o") or {}
                    try:
                        qty = float(o.get("q", 0))
                        price = float(o.get("ap") or o.get("p") or 0)
                        agg.add(int(msg.get("E", 0)), str(o.get("S", "SELL")), qty * price)
                    except (TypeError, ValueError):
                        continue
                    agg.maybe_flush()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # reconnect with backoff
            log.warning("liq_ws_error", error=str(e), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--symbol", default="btcusdt")
    ap.add_argument("--flush-seconds", type=int, default=300)
    args = ap.parse_args()
    configure_logging("INFO")
    settings = load_settings(args.config)
    store = ParquetStore(settings.data.parquet_dir)
    agg = HourlyAgg(store, f"binance_{args.symbol.upper()}_liq_1h", args.flush_seconds)

    loop = asyncio.new_event_loop()
    task = loop.create_task(run(args.symbol, agg))

    def _stop(*_: object) -> None:
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        agg.maybe_flush(force=True)
        loop.close()


if __name__ == "__main__":
    main()
