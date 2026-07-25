"""Performance reporting and validation-facing metrics."""

from btc_backtest.reporting.artifacts import (
    ArtifactBundle,
    ArtifactWriter,
    RunManifest,
)
from btc_backtest.reporting.html import render_html
from btc_backtest.reporting.metrics import (
    BenchmarkComparison,
    PerformanceMetrics,
    compare_benchmarks,
    compute_metrics,
    periods_per_year,
)

__all__ = [
    "ArtifactBundle",
    "ArtifactWriter",
    "BenchmarkComparison",
    "PerformanceMetrics",
    "RunManifest",
    "compare_benchmarks",
    "compute_metrics",
    "periods_per_year",
    "render_html",
]
