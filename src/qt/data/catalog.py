"""Data-source catalog and local freshness checks."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import pandas as pd

from qt.data.store import ParquetStore


@dataclass(frozen=True)
class DataSource:
    id: str
    name: str
    category: str
    provider: str
    access: str
    endpoint: str
    cadence: str
    used_for: str
    dataset: str
    key: str
    columns: tuple[str, ...]
    required_env: tuple[str, ...] = ()
    max_staleness_hours: int | None = None

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["columns"] = list(self.columns)
        data["required_env"] = list(self.required_env)
        return data


DATA_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        id="ohlcv_binance_btcusdt_1h",
        name="BTC/USDT OHLCV",
        category="market",
        provider="ccxt + Binance spot",
        access="free",
        endpoint="exchange.fetch_ohlcv(BTC/USDT, 1h)",
        cadence="1h candles",
        used_for="Price action, realized volatility, ATR exits, mark price, backtest replay.",
        dataset="ohlcv",
        key="binance_BTCUSDT_1h",
        columns=("open", "high", "low", "close", "volume"),
        max_staleness_hours=3,
    ),
    DataSource(
        id="funding_binance_btcusdt",
        name="BTCUSDT funding rate",
        category="derivatives",
        provider="Binance USD-M futures",
        access="free",
        endpoint="/fapi/v1/fundingRate",
        cadence="8h funding history",
        used_for="Contrarian derivatives stress: negative funding Z-score or sustained negative funding.",
        dataset="derivatives",
        key="binance_BTCUSDT_funding",
        columns=("funding_rate",),
        max_staleness_hours=12,
    ),
    DataSource(
        id="oi_binance_btcusdt",
        name="BTCUSDT open interest",
        category="derivatives",
        provider="Binance USD-M futures",
        access="free",
        endpoint="/futures/data/openInterestHist",
        cadence="5m-1d, last 30d from Binance",
        used_for="Liquidation cascade proxy via sharp 24h open-interest drops.",
        dataset="derivatives",
        key="binance_BTCUSDT_oi_1h",
        columns=("oi_btc", "oi_usd"),
        max_staleness_hours=3,
    ),
    DataSource(
        id="lsr_binance_btcusdt",
        name="BTCUSDT global long/short ratio",
        category="derivatives",
        provider="Binance USD-M futures",
        access="free",
        endpoint="/futures/data/globalLongShortAccountRatio",
        cadence="5m-1d, last 30d from Binance",
        used_for="Crowded-short contrarian confirmation when ratio percentile is extremely low.",
        dataset="derivatives",
        key="binance_BTCUSDT_lsr_1h",
        columns=("long_short_ratio",),
        max_staleness_hours=3,
    ),
    DataSource(
        id="fear_greed",
        name="Crypto Fear & Greed Index",
        category="sentiment",
        provider="alternative.me",
        access="free with attribution",
        endpoint="/fng/",
        cadence="daily",
        used_for="Sentiment capitulation confirmation when extreme fear persists.",
        dataset="sentiment",
        key="fear_greed",
        columns=("fear_greed", "fear_greed_label"),
        max_staleness_hours=36,
    ),
    DataSource(
        id="coinmetrics_mvrv",
        name="MVRV",
        category="onchain",
        provider="Coin Metrics Community API",
        access="free for non-commercial use",
        endpoint="/v4/timeseries/asset-metrics?metrics=CapMVRVCur",
        cadence="daily",
        used_for="Free valuation proxy when paid MVRV-Z data is unavailable.",
        dataset="onchain",
        key="coinmetrics_mvrv",
        columns=("mvrv",),
        max_staleness_hours=48,
    ),
    DataSource(
        id="glassnode_mvrv_z",
        name="MVRV Z-Score",
        category="onchain",
        provider="Glassnode",
        access="paid API key",
        endpoint="/v1/metrics/market/mvrv_z_score",
        cadence="daily",
        used_for="On-chain valuation capitulation in the composite score.",
        dataset="onchain",
        key="glassnode_mvrv_z",
        columns=("mvrv_z",),
        required_env=("QT_GLASSNODE_API_KEY",),
        max_staleness_hours=48,
    ),
    DataSource(
        id="glassnode_sopr_adj",
        name="Adjusted SOPR",
        category="onchain",
        provider="Glassnode",
        access="paid API key",
        endpoint="/v1/metrics/indicators/sopr_adjusted",
        cadence="daily",
        used_for="Spent-output loss realization confirmation.",
        dataset="onchain",
        key="glassnode_sopr_adj",
        columns=("sopr_adj",),
        required_env=("QT_GLASSNODE_API_KEY",),
        max_staleness_hours=48,
    ),
    DataSource(
        id="fred_vix",
        name="VIX",
        category="macro",
        provider="FRED",
        access="free API key",
        endpoint="series/VIXCLS",
        cadence="daily market close",
        used_for="Macro veto: avoid buying during broad-market volatility shocks.",
        dataset="macro",
        key="fred_vix",
        columns=("vix",),
        required_env=("QT_FRED_API_KEY",),
        max_staleness_hours=72,
    ),
    DataSource(
        id="fred_dxy",
        name="DXY",
        category="macro",
        provider="FRED",
        access="free API key",
        endpoint="series/DTWEXBGS",
        cadence="daily market close",
        used_for="Macro veto: avoid aggressive dollar breakout regimes.",
        dataset="macro",
        key="fred_dxy",
        columns=("dxy",),
        required_env=("QT_FRED_API_KEY",),
        max_staleness_hours=72,
    ),
)


def data_source_statuses(
    store: ParquetStore,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Return source metadata plus local parquet availability/freshness."""

    now = now or datetime.now(tz=timezone.utc)
    statuses: list[dict[str, object]] = []
    for source in DATA_SOURCES:
        status = source.as_dict()
        configured = all(bool(os.getenv(name)) for name in source.required_env)
        status["configured"] = configured
        status.update(_local_snapshot(store, source, now))
        statuses.append(status)
    return statuses


def _local_snapshot(
    store: ParquetStore,
    source: DataSource,
    now: datetime,
) -> dict[str, object]:
    path = store.path(source.dataset, source.key)
    base: dict[str, object] = {
        "path": str(path),
        "exists": path.exists(),
        "rows": 0,
        "start": None,
        "end": None,
        "fresh": None,
        "staleness_hours": None,
        "error": None,
    }
    if not path.exists():
        return base
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        base["error"] = str(exc)
        return base

    base["rows"] = len(df)
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return base

    index = df.index
    if index.tz is None:
        index = index.tz_localize("UTC")
    last = index.max()
    first = index.min()
    staleness = max(0.0, (now - last.to_pydatetime()).total_seconds() / 3600)
    base["start"] = first.isoformat()
    base["end"] = last.isoformat()
    base["staleness_hours"] = staleness
    if source.max_staleness_hours is not None:
        base["fresh"] = staleness <= source.max_staleness_hours
    return base


def source_by_key(dataset: str, key: str) -> DataSource | None:
    for source in DATA_SOURCES:
        if source.dataset == dataset and source.key == key:
            return source
    return None
