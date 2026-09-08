# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Incremental O(1)/O(log n) rolling statistics used by the indicator engine.

These run on every market event, so they must not recompute whole windows.
Cross-checked against pandas in tests/test_rolling.py.
"""
from __future__ import annotations

import bisect
import math
from collections import deque


class Ewma:
    """Exponentially weighted moving average with pandas-compatible alpha = 2/(span+1)."""

    def __init__(self, span: int):
        self.alpha = 2.0 / (span + 1.0)
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else (1 - self.alpha) * self.value + self.alpha * x
        return self.value


class EwmVol:
    """EWMA volatility (std) of a series of returns."""

    def __init__(self, span: int):
        self._var = Ewma(span)

    def update(self, r: float) -> float:
        v = self._var.update(r * r)
        return math.sqrt(max(v, 0.0))

    @property
    def value(self) -> float | None:
        return None if self._var.value is None else math.sqrt(max(self._var.value, 0.0))


class RollingWindow:
    """Fixed-size window with O(log n) median/quantile via a sorted shadow list."""

    def __init__(self, size: int):
        self.size = size
        self._q: deque[float] = deque()
        self._sorted: list[float] = []

    def update(self, x: float) -> None:
        self._q.append(x)
        bisect.insort(self._sorted, x)
        if len(self._q) > self.size:
            old = self._q.popleft()
            idx = bisect.bisect_left(self._sorted, old)
            self._sorted.pop(idx)

    def __len__(self) -> int:
        return len(self._q)

    @property
    def full(self) -> bool:
        return len(self._q) >= self.size

    def median(self) -> float | None:
        n = len(self._sorted)
        if n == 0:
            return None
        m = n // 2
        return self._sorted[m] if n % 2 else 0.5 * (self._sorted[m - 1] + self._sorted[m])

    def quantile(self, q: float) -> float | None:
        n = len(self._sorted)
        if n == 0:
            return None
        # linear interpolation, matching numpy's default
        pos = q * (n - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return self._sorted[lo] * (1 - frac) + self._sorted[hi] * frac

    def percentile_rank(self, x: float) -> float | None:
        """Percentage of stored values <= x, in [0, 100]."""
        n = len(self._sorted)
        if n == 0:
            return None
        return 100.0 * bisect.bisect_right(self._sorted, x) / n

    def sum(self) -> float:
        return float(sum(self._q))

    def mean(self) -> float | None:
        return None if not self._q else self.sum() / len(self._q)

    def std(self) -> float | None:
        n = len(self._q)
        if n < 2:
            return None
        mu = self.sum() / n
        return math.sqrt(sum((v - mu) ** 2 for v in self._q) / (n - 1))


class Atr:
    """Wilder's ATR over OHLC candles."""

    def __init__(self, period: int):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._warmup: list[float] = []

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = high - low if self._prev_close is None else max(
            high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._warmup.append(tr)
            if len(self._warmup) >= self.period:
                self.value = sum(self._warmup) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class TimedWindow:
    """Values with timestamps; keeps only entries within `span_ms` of the latest update."""

    def __init__(self, span_ms: int):
        self.span_ms = span_ms
        self._q: deque[tuple[int, float]] = deque()
        self._sum = 0.0

    def update(self, ts_ms: int, value: float) -> None:
        self._q.append((ts_ms, value))
        self._sum += value
        self._evict(ts_ms)

    def advance(self, ts_ms: int) -> None:
        self._evict(ts_ms)

    def _evict(self, now_ms: int) -> None:
        while self._q and self._q[0][0] < now_ms - self.span_ms:
            _, v = self._q.popleft()
            self._sum -= v

    def sum(self) -> float:
        return self._sum

    def count(self) -> int:
        return len(self._q)


