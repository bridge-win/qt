"""Data ingestion adapters and local store.

Adapter philosophy:
- Each adapter is a pure function or class with a uniform `fetch_*` interface
  returning a `pandas.DataFrame` indexed by UTC datetime.
- All adapters degrade gracefully when keys are absent (return empty DF with
  the expected schema and a logged warning) so research can proceed offline.
- Network calls are wrapped with tenacity retry + httpx timeout.
- Persisted to Parquet via `qt.data.store.ParquetStore` so the backtest engine
  only reads from local snapshots (deterministic, replayable).
"""

from qt.data.store import ParquetStore

__all__ = ["ParquetStore"]
