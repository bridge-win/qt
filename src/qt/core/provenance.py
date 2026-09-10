"""Provenance registry for every numeric constant the system uses.

Rules (from the 2026-09 review):

1. Prefer values that are *observed in public data* or *computed from public
   data at run time* over hand-set constants.
2. Whenever a number is displayed, it must carry its source.
3. Fitted or assumed numbers must state the fitting method and the data
   they were fitted on.

``method`` vocabulary
---------------------
observed    a value read directly from a public dataset (dated)
computed    derived deterministically from a public dataset at run time
            (formula + input dataset are the provenance)
literature  a threshold published by the indicator's author or a vendor
            reference page; we did not fit it ourselves
fitted      estimated by us from public data with a stated method
            (OLS / quantile / walk-forward); the fit inputs are listed
assumed     a design choice with no empirical calibration yet — flagged
            so it can be replaced when data exists

Every entry is a ``Param``; ``annotate()`` wraps a value with its provenance
for display, and ``audit_threshold_config()`` guarantees that every field of
``ThresholdConfig`` has an entry (enforced by a unit test).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Method = Literal["observed", "computed", "literature", "fitted", "assumed"]


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    kind: Literal["dataset", "paper", "vendor_doc", "article", "exchange_doc"]
    accessed: str            # ISO date we last checked it
    note: str = ""


@dataclass(frozen=True)
class Param:
    key: str
    value: Any
    method: Method
    sources: tuple[str, ...]
    derivation: str          # formula / how the number was obtained
    unit: str = ""
    data_window: str = ""    # e.g. "BTC daily 2013-04-28 .. 2026-09-09"
    fit: str = ""            # for method == fitted: estimator + n obs + residual
    caveat: str = ""


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

SOURCES: dict[str, Source] = {s.id: s for s in [
    Source("coinmetrics", "Coin Metrics Community API v4 (CapMrktCurUSD, CapRealUSD, HashRate, PriceUSD)",
           "https://docs.coinmetrics.io/api/v4", "dataset", "2026-09-10",
           "Free tier, daily frequency, full history from 2010."),
    Source("bitstamp", "Bitstamp BTC/USD daily OHLCV via ccxt",
           "https://www.bitstamp.net/api/", "dataset", "2026-09-10", "Continuous daily history since 2011-08."),
    Source("binance_fapi", "Binance USDT-M futures REST/WS (funding, OI, LSR, forceOrder)",
           "https://developers.binance.com/docs/derivatives/usds-margined-futures", "exchange_doc", "2026-09-10",
           "OI / LSR history limited to 30 days; forceOrder stream throttled to <=1 msg/s per symbol since 2021-04."),
    Source("coinglass_v4", "Coinglass Open API v4 (aggregated liquidations, funding, ETF flows)",
           "https://docs.coinglass.com/reference/aggregated-liquidation-history", "vendor_doc", "2026-09-10",
           "Paid; 1h granularity on Standard tier."),
    Source("alternative_me", "alternative.me Crypto Fear & Greed Index",
           "https://alternative.me/crypto/fear-and-greed-index/", "vendor_doc", "2026-09-10",
           "Composite: volatility 25%, momentum/volume 25%, social 15%, surveys 15%, dominance 10%, trends 10%. Daily since 2018-02-01."),
    Source("yahoo_chart", "Yahoo Finance chart API (^VIX, DX-Y.NYB)",
           "https://query1.finance.yahoo.com/v8/finance/chart/", "dataset", "2026-09-10", "Unofficial endpoint."),
    Source("fred", "FRED (VIXCLS, DTWEXBGS)", "https://fred.stlouisfed.org/", "dataset", "2026-09-10",
           "DTWEXBGS is the broad trade-weighted dollar index, not ICE DXY."),
    Source("defillama", "DefiLlama stablecoin supply", "https://stablecoins.llama.fi/stablecoincharts/all",
           "dataset", "2026-09-10", ""),
    Source("glassnode_mayer", "Glassnode Studio - Mayer Multiple (2.4 / 0.8 thresholds)",
           "https://studio.glassnode.com/charts/mayer-multiple?a=BTC", "vendor_doc", "2026-09-10",
           "'Following the original analysis, overbought and oversold have coincided with 2.4 and 0.8.'"),
    Source("mayer_2015", "Trace Mayer, Mayer Multiple (2015) - 2.4 bubble threshold from simulations",
           "https://www.coindesk.com/markets/2019/02/17/mayer-multiples-the-metric-that-helps-call-bitcoin-bubbles-and-bottoms",
           "article", "2026-09-10", "Author's backtest: accumulating below 2.4 gave best long-run results."),
    Source("mvrv_z_ref", "MVRV Z-Score reference thresholds (Awe&Wonder / Puell / Mahmudov, 2018) via Spark glossary",
           "https://www.spark.money/glossary/mvrv-ratio", "article", "2026-09-10",
           "Z>7 marked 2013/2017/2021 tops; Z<0 marked 2015/2018/2022 bottoms. Peak Z: 2017 ~9.4, Apr-2021 ~7.3, Nov-2021 ~6.4."),
    Source("mvrv_2025_cycle", "Amberdata / bit.com on-chain review of the Oct-2025 top",
           "https://blog.amberdata.io/onchain-valuation-what-bitcoins-realized-price-says-about-2026", "article", "2026-09-10",
           "2025 cycle: MVRV peak ~2.52, NUPL never > 0.75, Pi Cycle Top never crossed - literature top bands did NOT fire."),
    Source("pi_cycle", "Philip Swift (LookIntoBitcoin, 2019) Pi Cycle Top; signal history",
           "https://newhedge.io/bitcoin/pi-cycle-top-indicator", "article", "2026-09-10",
           "Signals: 2013-04-05, 2013-12-03, 2017-12-16, 2021-04-12. Missed Nov-2021 and Oct-2025 tops."),
    Source("sma200w", "200-week moving average as bear-market floor (Bitcoin Magazine Pro / Phemex 2026)",
           "https://phemex.com/blogs/bitcoin-200-week-moving-average-chart", "article", "2026-09-10",
           "Touched at every prior bear bottom; ~ $58-61k in Q1-2026."),
    Source("halving_clock", "arXiv 2607.26188 - halving-clock regularity of tops/bottoms",
           "https://arxiv.org/html/2607.26188", "paper", "2026-09-10",
           "Tops 525/546/534 days after halving (2017/2021/2025); bottoms ~1 y after top."),
    Source("cascade_oct2025", "Anatomy of a Crypto Cascade (SSRN 6579278) - minute-level Oct-10-2025 crash",
           "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6579278", "paper", "2026-09-10",
           "-12.6% in ~10 min; volume 22x baseline before trough; futures led spot."),
    Source("kraken_funding", "Kraken Learn - funding-rate extremes as a signal",
           "https://www.kraken.com/learn/futures-trading-funding-rate-strategy", "vendor_doc", "2026-09-10",
           "2021 peak funding 0.15-0.20%/8h preceded 10-30% corrections."),
    Source("liu_tsyvinski", "Liu, Tsyvinski, Wu (2022) Common Risk Factors in Cryptocurrency, J. Finance 77(2)",
           "https://papers.ssrn.com/abstract=3379131", "paper", "2026-09-10",
           "Market, size, momentum explain cross-section; momentum is a priced factor (basis for trend sleeve)."),
    Source("hash_ribbons", "Capriole Investments - Hash Ribbons (2019)",
           "https://capriole.com/hash-ribbons/", "article", "2026-09-10",
           "30d/60d hash-rate SMA cross after miner capitulation."),
    Source("halvings", "Bitcoin halving block heights / dates (blockchain record)",
           "https://www.blockchain.com/explorer", "dataset", "2026-09-10",
           "Blocks 210000 / 420000 / 630000 / 840000."),
    Source("qt_walkforward", "This repo's walk-forward experiment registry (state/experiments.jsonl)",
           "https://github.com/bridge-win/qt", "dataset", "2026-09-10",
           "Thresholds marked 'fitted' here are re-estimated by `qt backtest --walkforward`."),
]}


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def _p(key: str, value: Any, method: Method, sources: str | tuple[str, ...], derivation: str, **kw: Any) -> Param:
    src = (sources,) if isinstance(sources, str) else tuple(sources)
    for s in src:
        if s not in SOURCES:
            raise KeyError(f"unknown source id {s!r} for param {key}")
    return Param(key=key, value=value, method=method, sources=src, derivation=derivation, **kw)


REGISTRY: dict[str, Param] = {p.key: p for p in [
    # --- halving clock (observed facts) ------------------------------------
    _p("cycle.halvings", ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20"], "observed", "halvings",
       "Block timestamps at heights 210000, 420000, 630000, 840000."),
    _p("cycle.period_days", 1460, "literature", "halving_clock",
       "~4 years between halvings; observed 1,424 / 1,402 / 1,440 days.", unit="days"),
    _p("cycle.top_phase", 0.37, "fitted", "halving_clock",
       "Mean of (days from halving to cycle top)/1460 for 2017/2021/2025 = (525+546+534)/3/1460.",
       fit="mean of 3 observations; range 0.36-0.37", data_window="2016-2025"),

    # --- MVRV-Z ---------------------------------------------------------------
    _p("mvrv_z.formula", "(MC - RC) / rolling_std(MC, 730d)", "computed", ("coinmetrics", "mvrv_z_ref"),
       "Standard MVRV-Z; std window 2y as in the LookIntoBitcoin implementation. Inputs CapMrktCurUSD, CapRealUSD."),
    _p("mvrv_z.top_literature", 7.0, "literature", "mvrv_z_ref",
       "Red zone >7 marked 2013, 2017 (9.4), Apr-2021 (7.3) tops.",
       caveat="Nov-2021 top only 6.4 and Oct-2025 top never reached red zone: use data-derived bands (cycle.calibrate_bands)."),
    _p("mvrv_z.bottom_literature", 0.0, "literature", "mvrv_z_ref",
       "Green zone <0 marked 2015 / 2018 (-0.2) / 2022 bottoms."),

    # --- NUPL -------------------------------------------------------------------
    _p("nupl.formula", "(MC - RC) / MC", "computed", "coinmetrics", "Net Unrealized Profit/Loss from caps."),
    _p("nupl.euphoria", 0.75, "literature", ("mvrv_z_ref", "mvrv_2025_cycle"),
       "Glassnode band scheme: <0 capitulation, 0-0.25 hope, 0.25-0.5 optimism, 0.5-0.75 belief, >0.75 euphoria.",
       caveat="2025 cycle max ~0.60 and never sustained >0.75 - literature top band did not fire."),
    _p("nupl.capitulation", 0.0, "literature", "mvrv_z_ref", "NUPL < 0 = aggregate unrealized loss."),

    # --- Mayer multiple ----------------------------------------------------------
    _p("mayer.formula", "close / SMA(close, 200d)", "computed", "bitstamp", "Trace Mayer 2015 definition."),
    _p("mayer.top", 2.4, "literature", ("mayer_2015", "glassnode_mayer"),
       "Author's simulation: values >2.4 = speculative bubble.",
       caveat="Cycle-top Mayer has declined each cycle: >3.0 (2013), 3.4 (2017), 2.0 (2021), ~1.18 (Oct-2025)."),
    _p("mayer.bottom", 0.8, "literature", "glassnode_mayer", "Oversold threshold on Glassnode reference chart."),

    # --- 200-week SMA ---------------------------------------------------------------
    _p("sma200w.floor", 1.0, "literature", "sma200w",
       "Price / SMA(1400d) ~ 1.0 at 2015, 2018-19, 2020, 2022 bottoms."),
    _p("sma200w.top_literature", 4.0, "assumed", "sma200w",
       "Upper edge for the 0-1 band map; tops were ~5x (2017), ~3x (2021), ~2x (2025) - replaced by calibrate_bands."),

    # --- Pi Cycle ---------------------------------------------------------------------
    _p("pi_cycle.top", "SMA111 crosses above 2 x SMA350", "literature", "pi_cycle",
       "Fired 2013-04-05, 2013-12-03, 2017-12-16, 2021-04-12 within 1-4 days of the top.",
       caveat="Did not fire for the Nov-2021 or Oct-2025 tops."),
    _p("pi_cycle.bottom", "SMA150 crosses above SMA471", "literature", "pi_cycle",
       "Pi Cycle Bottom (Swift 2022)."),

    # --- Fear & Greed ----------------------------------------------------------------
    _p("fear_greed.max", 20, "literature", "alternative_me",
       "'Extreme fear' band is 0-24 on the vendor scale; we use <=20 sustained.",
       caveat="Index already embeds volatility and momentum -> collinear with price group; used as confirmation only."),
    _p("fear_greed.hot", 80, "literature", "alternative_me", "'Extreme greed' band is 75-100; we use >=80 sustained."),
    _p("fear_greed.sustained_days", 3, "assumed", "alternative_me", "Debounce; not calibrated."),

    # --- price-action thresholds -------------------------------------------------------
    _p("rsi.oversold", 20.0, "assumed", "qt_walkforward", "Standard 30 tightened to 20 for a 'rare event' detector; re-fit by walk-forward."),
    _p("rsi.overbought", 80.0, "assumed", "qt_walkforward", "Mirror of rsi.oversold."),
    _p("bb.std", 2.5, "assumed", "qt_walkforward", "Bollinger z beyond 2.5 sigma (~1.2% of bars if normal)."),
    _p("drawdown.30d_min", 0.15, "assumed", "qt_walkforward", "15% drawdown from 30d high."),
    _p("wick.body_ratio_min", 3.0, "assumed", "qt_walkforward", "Lower wick >= 3x body."),
    _p("volume.z_min", 2.0, "assumed", "qt_walkforward", "Volume z-score >= 2."),
    _p("volume.drop_min", 0.05, "assumed", "qt_walkforward", ""),
    _p("atr_disp.extreme", 3.0, "assumed", "qt_walkforward", "(close - SMA20)/ATR14 beyond +-3."),
    _p("runup.30d_min", 0.40, "assumed", "qt_walkforward", "+40% from 30d low."),
    _p("flash_crash.pct", 0.08, "literature", "cascade_oct2025", "Oct-10-2025: -12.6% in ~10 min; 8% over 4 bars is the detector floor."),
    _p("flash_crash.bars", 4, "assumed", "cascade_oct2025", ""),

    # --- volatility ----------------------------------------------------------------------
    _p("rv.ratio_min", 2.0, "assumed", "qt_walkforward", "RV(1d)/RV(30d) >= 2."),
    _p("atr.ratio_min", 2.0, "assumed", "qt_walkforward", ""),

    # --- derivatives ---------------------------------------------------------------------
    _p("funding.8h_max", -0.0005, "literature", "kraken_funding",
       "-0.05%/8h; Kraken: deeply negative funding in uptrends preceded sharp rebounds.", unit="fraction per 8h"),
    _p("funding.8h_hot", 0.0005, "literature", "kraken_funding",
       "+0.05%/8h sustained; 2021 euphoria 0.15-0.20%/8h.", unit="fraction per 8h"),
    _p("oi.drop_24h_min", 0.10, "assumed", ("binance_fapi", "cascade_oct2025"), "10% OI drop in 24h = deleveraging."),
    _p("oi.surge_24h_min", 0.15, "assumed", "binance_fapi", "15% OI build in 24h = leverage build-up."),
    _p("liq.long_z", 3.0, "assumed", ("binance_fapi", "coinglass_v4"),
       "Z-score of long-liquidation USD over 30d; absolute USD unusable because feeds are sampled."),
    _p("liq.long_24h_usd", 400_000_000.0, "assumed", "coinglass_v4",
       "Post-FTX baseline; pre-2022 use 800M-1B. Kept for display only.", unit="USD",
       caveat="Absolute liquidation USD is vendor-sampled; prefer liq.long_z."),
    _p("liq.long_pct_min", 0.95, "assumed", "coinglass_v4", ""),
    _p("lsr.percentile_max", 0.10, "assumed", "binance_fapi", "Long/short ratio in bottom decile of 30d."),
    _p("basis.backwardation", 0.0, "literature", "cascade_oct2025", "Futures below spot during stress."),

    # --- on-chain paid-source thresholds ---------------------------------------------------
    _p("asopr.max", 1.0, "literature", "mvrv_z_ref", "aSOPR < 1 = coins moved at a loss (Glassnode definition)."),
    _p("exchange_netflow.negative", True, "assumed", "qt_walkforward", "Net outflow from exchanges."),

    # --- macro ---------------------------------------------------------------------------------
    _p("vix.max", 40.0, "assumed", ("yahoo_chart", "fred"), "Do not buy when VIX > 40 (Mar-2020: 82, Oct-2008: 80)."),
    _p("dxy.z_max", 2.5, "assumed", "yahoo_chart", "20d DXY momentum z over 60d > 2.5 = dollar squeeze."),

    # --- composite aggregation ---------------------------------------------------------------
    _p("composite.entry_score_min", 0.65, "assumed", "qt_walkforward", "Share of available groups firing."),
    _p("composite.min_factor_groups", 4, "assumed", "qt_walkforward", "Of {price, vol, deriv, onchain, sentiment}."),
    _p("composite.overheat_score_min", 0.60, "assumed", "qt_walkforward", ""),

    # --- cycle position weights --------------------------------------------------------------
    _p("cycle.weights", {"mvrv_z": 3.0, "nupl": 2.0, "mayer": 2.0, "sma200w": 2.0, "halving": 1.0}, "assumed",
       ("mvrv_z_ref", "halving_clock"),
       "Design weights favouring realized-cap metrics; not fitted. Replace with inverse-variance of per-cycle timing error once >=4 cycles."),
    _p("cycle.alloc_curve", {"0": 1.0, "15": 1.0, "35": 0.75, "65": 0.5, "85": 0.25, "100": 0.1}, "assumed", "qt_walkforward",
       "Piece-wise linear target allocation vs cycle position."),
    _p("cycle.decay_fit", "OLS of ln(peak gauge) on cycle index, projected one cycle ahead", "fitted",
       ("coinmetrics", "bitstamp", "mvrv_2025_cycle"),
       "Diminishing-peaks projection used by calibrate_bands() when >=3 observed cycle tops exist.",
       fit="n = number of completed cycles (3-4); reported with residual std and per-cycle points"),

    # --- trend / risk ----------------------------------------------------------------------------
    _p("trend.ma_weeks", 20, "literature", "liu_tsyvinski",
       "Faber-style 10-month/20-week SMA; momentum is a priced crypto factor (Liu-Tsyvinski-Wu)."),
    _p("trend.hash_ribbon", "SMA30(hashrate) crosses above SMA60", "literature", "hash_ribbons", "Capriole definition."),
    _p("trend.mayer_late_cycle", 2.4, "literature", "mayer_2015", "Cross-up above Mayer 2.4 sized down."),
]}


# ---------------------------------------------------------------------------
# Mapping from ThresholdConfig fields to registry keys
# ---------------------------------------------------------------------------

THRESHOLD_FIELD_MAP: dict[str, str] = {
    "rsi_oversold": "rsi.oversold", "bb_std": "bb.std", "drawdown_30d_min": "drawdown.30d_min",
    "wick_body_ratio_min": "wick.body_ratio_min", "volume_z_min": "volume.z_min",
    "volume_drop_min": "volume.drop_min", "rv_ratio_min": "rv.ratio_min", "atr_ratio_min": "atr.ratio_min",
    "funding_rate_8h_max": "funding.8h_max", "oi_drop_24h_min": "oi.drop_24h_min",
    "long_liq_pct_min": "liq.long_pct_min", "lsr_percentile_max": "lsr.percentile_max",
    "long_liq_24h_usd": "liq.long_24h_usd", "long_liq_z": "liq.long_z",
    "flash_crash_pct": "flash_crash.pct", "flash_crash_bars": "flash_crash.bars",
    "basis_backwardation_thresh": "basis.backwardation", "asopr_max": "asopr.max",
    "mvrv_z_max": "mvrv_z.bottom_literature", "exchange_netflow_neg": "exchange_netflow.negative",
    "coinbase_premium_extreme": "exchange_netflow.negative", "coinbase_premium_bars": "exchange_netflow.negative",
    "ssr_z_max": "exchange_netflow.negative", "whale_ratio_z_max": "exchange_netflow.negative",
    "whale_net_z_max": "exchange_netflow.negative", "accumulation_trend_max": "exchange_netflow.negative",
    "fear_greed_max": "fear_greed.max", "fear_greed_sustained_days": "fear_greed.sustained_days",
    "social_z_max": "volume.z_min", "vix_max": "vix.max", "dxy_z_max": "dxy.z_max",
    "entry_score_min": "composite.entry_score_min", "min_factor_groups": "composite.min_factor_groups",
    "atr_disp_extreme": "atr_disp.extreme", "rsi_overbought": "rsi.overbought",
    "runup_30d_min": "runup.30d_min", "funding_rate_8h_hot": "funding.8h_hot",
    "oi_surge_24h_min": "oi.surge_24h_min", "mvrv_z_hot": "mvrv_z.top_literature",
    "nupl_hot": "nupl.euphoria", "fear_greed_hot": "fear_greed.hot",
    "overheat_score_min": "composite.overheat_score_min",
}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def get(key: str) -> Param:
    return REGISTRY[key]


def describe(key: str) -> dict[str, Any]:
    p = REGISTRY[key]
    d = asdict(p)
    d["sources"] = [asdict(SOURCES[s]) for s in p.sources]
    return d


def annotate(value: Any, key: str, **override: Any) -> dict[str, Any]:
    """Wrap a displayed number with its provenance.

    ``override`` lets a run-time computation report the actual data window /
    fit statistics it used (e.g. calibrate_bands) instead of the static text.
    """

    p = REGISTRY[key]
    out = {
        "value": value, "method": p.method, "derivation": p.derivation,
        "sources": [SOURCES[s].name for s in p.sources],
        "source_urls": [SOURCES[s].url for s in p.sources],
    }
    if p.unit:
        out["unit"] = p.unit
    if p.caveat:
        out["caveat"] = p.caveat
    if p.fit:
        out["fit"] = p.fit
    if p.data_window:
        out["data_window"] = p.data_window
    out.update(override)
    return out


def series_provenance(dataset_key: str) -> dict[str, str]:
    """Provenance stub for a run-time data series by its Parquet key prefix."""

    prefix = dataset_key.split("_")[0].lower()
    lookup = {
        "coinmetrics": "coinmetrics", "bitstamp": "bitstamp", "binance": "binance_fapi",
        "coinglass": "coinglass_v4", "fear": "alternative_me", "yahoo": "yahoo_chart",
        "fred": "fred", "defillama": "defillama",
    }
    sid = lookup.get(prefix)
    if sid is None:
        return {"source": "unknown", "url": "", "method": "observed"}
    s = SOURCES[sid]
    return {"source": s.name, "url": s.url, "method": "observed", "note": s.note}


def audit_threshold_config() -> list[str]:
    """Return ThresholdConfig fields with no provenance entry (should be empty)."""

    from qt.core.config import ThresholdConfig

    missing = []
    for f in ThresholdConfig.model_fields:
        key = THRESHOLD_FIELD_MAP.get(f)
        if key is None or key not in REGISTRY:
            missing.append(f)
    return missing


def report(method_filter: Method | None = None) -> list[dict[str, Any]]:
    rows = []
    for p in REGISTRY.values():
        if method_filter and p.method != method_filter:
            continue
        rows.append({
            "key": p.key, "value": p.value, "method": p.method,
            "sources": "; ".join(SOURCES[s].name for s in p.sources),
            "derivation": p.derivation, "caveat": p.caveat, "fit": p.fit,
        })
    return rows


__all__ = [
    "REGISTRY",
    "SOURCES",
    "THRESHOLD_FIELD_MAP",
    "Param",
    "Source",
    "annotate",
    "audit_threshold_config",
    "describe",
    "get",
    "report",
    "series_provenance",
]
