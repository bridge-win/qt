# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Core datatypes shared by live engine, paper trading, and backtester."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


# ---------------------------------------------------------------- market events

@dataclass(slots=True)
class Candle:
    ts: int              # open time, ms UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_ts(self) -> int:
        return self.ts + 60_000  # 1m candles throughout the core


@dataclass(slots=True)
class BookTicker:
    ts: int
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


@dataclass(slots=True)
class MarkPrice:
    ts: int
    mark: float
    index: float
    funding_rate: float          # predicted rate for next settlement
    next_funding_ts: int


@dataclass(slots=True)
class ForceOrder:
    """Exchange liquidation print. side is the liquidation ORDER side:
    SELL = longs being liquidated, BUY = shorts being liquidated."""
    ts: int
    side: str
    price: float
    qty: float

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class FundingRate:
    ts: int
    rate: float                  # settled rate


@dataclass(slots=True)
class OISnapshot:
    ts: int
    oi: float                    # base-asset open interest


# ---------------------------------------------------------------- state & enums

class Regime(str, enum.Enum):
    RANGE = "RANGE"
    POST_CASCADE = "POST_CASCADE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    CASCADE = "CASCADE"


class Trend(str, enum.Enum):
    STRONG_UP = "STRONG_UP"
    WEAK_UP = "WEAK_UP"
    NEUTRAL = "NEUTRAL"
    WEAK_DOWN = "WEAK_DOWN"
    STRONG_DOWN = "STRONG_DOWN"


@dataclass(slots=True)
class MarketState:
    """Snapshot produced by the indicator engine at each 1m candle close.
    None fields mean 'not warmed up / data unavailable' — consumers must cope."""
    ts: int = 0
    price: float = 0.0
    ema_anchor: float | None = None      # EMA60 of 1m closes
    atr: float | None = None             # ATR60 on 1m
    sigma_1m: float | None = None        # EWMA vol of 1m log returns
    vel1: float | None = None            # B1: k-min return / (sigma*sqrt(k))
    vel3: float | None = None
    vel5: float | None = None
    dev: float | None = None             # B2: (price - ema)/atr
    atr_tf: float | None = None          # ATR(14) on the trend timeframe (4h), for S2 stops
    vb: float | None = None              # B3: volume burst ratio
    liq_count_60s: int = 0               # B4
    liq_notional_60s: float = 0.0
    liq_pctl: float | None = None
    doi_5m: float | None = None          # B5
    doi_1h: float | None = None
    open_interest: float | None = None   # base-asset OI snapshot
    open_interest_pctl: float | None = None
    funding_rate: float | None = None    # predicted
    funding_pctl: float | None = None    # B6 (30d window)
    rv_pctl: float | None = None         # B7
    trend: Trend = Trend.NEUTRAL         # B8
    adx: float | None = None
    basis_z: float | None = None         # B9
    fng: float | None = None             # A9 fear&greed, 0-100
    long_short_ratio: float | None = None
    cascade_score: float = 0.0           # C1
    cascade_dir: str = "none"            # down|up|none
    crowding_score: float = 0.0          # C2, signed
    regime: Regime = Regime.RANGE        # C3
    eatfear: float = 0.0                 # C4
    eatgreed: float = 0.0
    # ---- v0.2.0 additions (plan v2.1) ----
    trend_score: float | None = None     # T: multi-horizon vol-normalized momentum
    trend_agree: int = 0                 # how many of 72h/7d/14d horizons share T's sign
    donchian_break: int = 0              # 1h close broke prior 20-bar high(+1)/low(-1)/none(0)
    atr_1h: float | None = None          # ATR(14, 1h) for trend stops
    ret_z_robust: float | None = None    # robust z of 1m return (IQR-based)
    liq_oi_ratio: float | None = None    # 60s liq notional / OI notional
    liq_oi_pctl: float | None = None
    liq_decaying: bool | None = None     # liquidation intensity peaked and declining
    doi_5m_pctl: float | None = None     # percentile of 5m OI change
    xvenue_dev: float | None = None      # (local - median other venues) / ATR60
    taker_imbalance: float | None = None # 5m (buy-sell)/(buy+sell), -1..1
    spread_pctl: float | None = None     # bid-ask spread percentile (guard input)
    event_active: bool = False           # extreme-event window open
    event_id: int = 0
    event_dir: str = "none"              # down|up
    event_extreme: float | None = None
    event_vwap: float | None = None
    reclaim_frac: float | None = None    # 0..1+ recovery toward pre-event reference
    event_verdict: str = "none"          # none|pending|reject|accept|ambiguous
    warmed_up: bool = False

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["trend"] = self.trend.value
        d["regime"] = self.regime.value
        return d


# ---------------------------------------------------------------- orders/intents

class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(slots=True)
class Order:
    """A resting order the executor manages. kind: LIMIT (post-only), STOP, TP."""
    order_id: str
    strategy: str
    side: Side
    kind: str                    # LIMIT | STOP | TP
    price: float
    qty: float
    reduce_only: bool = False
    tag: str = ""                # e.g. rung index


@dataclass(slots=True)
class Intent:
    """What a strategy wants. Executors (paper/live/backtest) interpret it."""
    action: str                  # includes LIMIT_IOC_ENTER and CANCEL_ENTRIES
    strategy: str
    side: Side | None = None
    prices: list[float] = field(default_factory=list)
    qtys: list[float] = field(default_factory=list)
    price: float | None = None
    qty: float | None = None
    reason: str = ""


@dataclass(slots=True)
class Fill:
    ts: int
    order_id: str
    strategy: str
    side: Side
    price: float
    qty: float
    fee: float
    kind: str                    # LIMIT | STOP | TP | MARKET
    tag: str = ""


def intent_notional(intent: Intent, ref_price: float) -> float:
    """Worst-case additional notional reserved by an entry intent."""
    if intent.action == "REPLACE_LADDER":
        return sum(float(p) * float(q) for p, q in zip(intent.prices, intent.qtys))
    if intent.action in ("MARKET_ENTER", "LIMIT_IOC_ENTER"):
        return float(intent.qty or 0) * float(intent.price or ref_price)
    return 0.0


@dataclass
class Position:
    """Net position held by one strategy (one-way)."""
    strategy: str
    side: Side | None = None
    qty: float = 0.0
    avg_price: float = 0.0
    opened_ts: int = 0
    rungs_filled: int = 0
    deadline_ts: int | None = None       # time-stop deadline
    meta: dict = field(default_factory=dict)

    def is_flat(self) -> bool:
        return self.qty <= 1e-12 or self.side is None

    def apply_fill(self, f: Fill) -> float:
        """Update position with a fill; returns realized pnl (0 when increasing)."""
        if self.is_flat():
            self.side, self.qty, self.avg_price = f.side, f.qty, f.price
            self.opened_ts = f.ts
            return 0.0
        if f.side is self.side:  # add
            total = self.qty + f.qty
            self.avg_price = (self.avg_price * self.qty + f.price * f.qty) / total
            self.qty = total
            return 0.0
        # reduce / close
        closed = min(self.qty, f.qty)
        pnl = (f.price - self.avg_price) * closed * self.side.sign
        self.qty -= closed
        if self.qty <= 1e-12:
            self.side, self.qty, self.avg_price = None, 0.0, 0.0
            self.rungs_filled = 0
            self.deadline_ts = None
        return pnl


@dataclass(slots=True)
class Trade:
    """A closed round-trip, for reporting."""
    strategy: str
    side: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float                   # net of fees
    fees: float
    exit_kind: str               # TP | STOP | TIME | FLATTEN

