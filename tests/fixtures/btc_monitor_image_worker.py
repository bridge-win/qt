"""Offline image smoke only; mounted read-only, never included in the image.

The production CLI remains unchanged. This harness injects deterministic public
market-shaped fixtures into the real service and exercises its signal lifecycle.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import signal
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from qt.btc_monitor.config import MonitorConfig
from qt.btc_monitor.models import MarketEvent
from qt.btc_monitor.service import MonitorService


def verify_container_boundary(state_dir: Path) -> None:
    assert os.getuid() == 10001 and os.getgid() == 10001
    assert os.statvfs("/").f_flag & os.ST_RDONLY
    fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    assert int(fields["CapEff"].strip(), 16) == 0
    assert fields["NoNewPrivs"].strip() == "1"
    assert state_dir.stat().st_uid == 10001
    assert state_dir.stat().st_mode & 0o2000
    with tempfile.TemporaryFile(dir="/tmp") as temporary:
        temporary.write(b"bounded tmpfs is writable")
    (state_dir / "smoke-worker.pid").write_text(str(os.getpid()))


async def fixture_feed(config, emit, reset, stop, report_error) -> None:
    del config, report_error
    reset("offline_image_fixture_warmup")
    opened = math.floor(time.time() / 60) * 60 - 180
    for index in range(3):
        start = opened + index * 60
        wall, mono = time.time(), time.monotonic()
        emit(MarketEvent(
            channel="candle", event_time=start + 59.999, received_at=wall,
            received_mono=mono, sequence=int(start * 1000), price=100000,
            low=99900, high=100100, open_time=start, close_time=start + 59.999,
            historical=True,
        ))
    next_candle = opened + 180
    sequence = 0
    while not stop.is_set():
        wall, mono = time.time(), time.monotonic()
        if wall >= next_candle + 60:
            emit(MarketEvent(
                channel="candle", event_time=next_candle + 59.999,
                received_at=wall, received_mono=mono, sequence=int(next_candle * 1000),
                price=100000, low=99900, high=100100,
                open_time=next_candle, close_time=next_candle + 59.999,
            ))
            next_candle += 60
        sequence += 1
        price = 100000 + 10 * math.sin(sequence / 5)
        emit(MarketEvent(
            channel="quote", received_at=wall, received_mono=mono,
            sequence=sequence, price=price, bid=price - 0.5, ask=price + 0.5,
        ))
        emit(MarketEvent(
            channel="trade", event_time=wall, received_at=wall, received_mono=mono,
            sequence=sequence, price=price, quantity=1,
        ))
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.2)


async def run(state_dir: Path) -> None:
    # Only micro warmup is shortened for this smoke; production defaults stay intact.
    config = MonitorConfig(state_dir=state_dir, minimum_coverage_seconds=5, minimum_trade_buckets=3)
    service = MonitorService(config, feed_runner=fixture_feed)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.stop_event.set)
    await service.run()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    state_dir = parser.parse_args().state_dir
    os.umask(0o027)
    verify_container_boundary(state_dir)
    asyncio.run(run(state_dir))


if __name__ == "__main__":
    main()
