"""Retail-scale opportunity scanners.

Each scanner returns normalized :class:`Opportunity` rows. Network access is
only used through injected exchange clients, so unit tests can provide fakes and
the live runner can provide ccxt clients.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, cast

import pandas as pd

from qt.core.logging import get_logger
from qt.indicators.events import flash_crash, wick_cluster

log = get_logger(__name__)


class ExchangeClient(Protocol):
    id: str
    has: Mapping[str, object]

    def fetch_funding_rates(
        self,
        symbols: Sequence[str] | None = None,
        params: Mapping[str, object] | None = None,
    ) -> object: ...

    def fetch_tickers(
        self,
        symbols: Sequence[str] | None = None,
        params: Mapping[str, object] | None = None,
    ) -> object: ...

    def fetch_ticker(
        self,
        symbol: str,
        params: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...


ExchangeFactory = Callable[[str], ExchangeClient]
OhlcvProvider = Callable[["ScannerSettings"], pd.DataFrame]


@dataclass(frozen=True)
class Opportunity:
    kind: str
    venue: str
    symbol: str
    edge_bps: float
    confidence: float
    capacity_usd: float
    action: str
    why: str
    details: dict[str, object] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    score: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "venue": self.venue,
            "symbol": self.symbol,
            "edge_bps": round(float(self.edge_bps), 4),
            "score": round(float(self.score), 4),
            "confidence": round(float(self.confidence), 4),
            "capacity_usd": round(float(self.capacity_usd), 2),
            "action": self.action,
            "why": self.why,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ScannerSettings:
    venues: list[str] = field(default_factory=lambda: ["binance", "okx", "bybit"])
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT"])
    stable_symbols: list[str] = field(default_factory=lambda: ["USDC/USDT", "DAI/USDT"])
    funding_apr_threshold: float = 0.12
    funding_diff_apr_threshold: float = 0.15
    taker_fee_bps: float = 7.5
    spread_buffer_bps: float = 5.0
    min_spread_edge_bps: float = 0.0
    depeg_threshold_bps: float = 30.0
    basis_apr_threshold: float = 0.10
    basis_default_days: float = 90.0
    basis_contracts: list[dict[str, str]] = field(default_factory=list)
    wick_recent_bars: int = 6
    wick_ratio_min: float = 2.0
    wick_cluster_count: int = 1
    flash_crash_pct: float = 0.08
    flash_crash_bars: int = 4
    wick_exchange: str = "binance"
    wick_timeframe: str = "1m"
    wick_history_days: int = 2
    default_capacity_usd: float = 10_000.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> ScannerSettings:
        if raw is None:
            return cls()
        defaults = cls()
        return cls(
            venues=_str_list(raw.get("venues"), defaults.venues),
            symbols=_str_list(raw.get("symbols"), defaults.symbols),
            stable_symbols=_str_list(raw.get("stable_symbols"), defaults.stable_symbols),
            funding_apr_threshold=_float_value(
                raw.get("funding_apr_threshold"), defaults.funding_apr_threshold
            ),
            funding_diff_apr_threshold=_float_value(
                raw.get("funding_diff_apr_threshold"), defaults.funding_diff_apr_threshold
            ),
            taker_fee_bps=_float_value(raw.get("taker_fee_bps"), defaults.taker_fee_bps),
            spread_buffer_bps=_float_value(
                raw.get("spread_buffer_bps"), defaults.spread_buffer_bps
            ),
            min_spread_edge_bps=_float_value(
                raw.get("min_spread_edge_bps"), defaults.min_spread_edge_bps
            ),
            depeg_threshold_bps=_float_value(
                raw.get("depeg_threshold_bps"), defaults.depeg_threshold_bps
            ),
            basis_apr_threshold=_float_value(
                raw.get("basis_apr_threshold"), defaults.basis_apr_threshold
            ),
            basis_default_days=_float_value(
                raw.get("basis_default_days"), defaults.basis_default_days
            ),
            basis_contracts=_basis_contracts(raw.get("basis_contracts")),
            wick_recent_bars=_int_value(raw.get("wick_recent_bars"), defaults.wick_recent_bars),
            wick_ratio_min=_float_value(raw.get("wick_ratio_min"), defaults.wick_ratio_min),
            wick_cluster_count=_int_value(
                raw.get("wick_cluster_count"), defaults.wick_cluster_count
            ),
            flash_crash_pct=_float_value(raw.get("flash_crash_pct"), defaults.flash_crash_pct),
            flash_crash_bars=_int_value(raw.get("flash_crash_bars"), defaults.flash_crash_bars),
            wick_exchange=str(raw.get("wick_exchange") or defaults.wick_exchange),
            wick_timeframe=str(raw.get("wick_timeframe") or defaults.wick_timeframe),
            wick_history_days=_int_value(raw.get("wick_history_days"), defaults.wick_history_days),
            default_capacity_usd=_float_value(
                raw.get("default_capacity_usd"), defaults.default_capacity_usd
            ),
        )


def ccxt_exchange_factory(exchange_id: str) -> ExchangeClient:
    try:
        import ccxt
    except ImportError as exc:
        raise RuntimeError("ccxt is required for live intel scanning") from exc
    klass = getattr(ccxt, exchange_id, None)
    if klass is None:
        raise ValueError(f"Unknown ccxt exchange: {exchange_id}")
    return cast(ExchangeClient, klass({"enableRateLimit": True}))


class FundingScanner:
    def __init__(self, exchange_factory: ExchangeFactory = ccxt_exchange_factory) -> None:
        self.exchange_factory = exchange_factory

    def scan(self, settings: ScannerSettings) -> list[Opportunity]:
        rows_by_symbol: dict[str, list[tuple[str, float, float]]] = {}
        out: list[Opportunity] = []
        for venue in settings.venues:
            try:
                exchange = self.exchange_factory(venue)
                records = _funding_records(exchange.fetch_funding_rates(settings.symbols))
            except Exception as exc:
                log.warning("intel_funding_scan_failed", venue=venue, error=str(exc))
                continue
            for record in records:
                symbol = str(record.get("symbol") or settings.symbols[0])
                rate = _num(
                    record.get("fundingRate")
                    or record.get("funding_rate")
                    or record.get("nextFundingRate")
                    or record.get("rate")
                )
                if rate is None:
                    continue
                apr = rate * 3.0 * 365.0
                rows_by_symbol.setdefault(symbol, []).append((venue, rate, apr))
                if apr >= settings.funding_apr_threshold:
                    out.append(
                        Opportunity(
                            kind="funding",
                            venue=venue,
                            symbol=symbol,
                            edge_bps=apr * 10_000.0,
                            confidence=_confidence(apr, settings.funding_apr_threshold),
                            capacity_usd=settings.default_capacity_usd,
                            action=f"short {venue} perp + long spot",
                            why=f"{venue} funding APR {apr:.1%} is above threshold",
                            details={"funding_rate_8h": rate, "annualized_apr": apr},
                        )
                    )

        for symbol, rows in rows_by_symbol.items():
            if len(rows) < 2:
                continue
            low = min(rows, key=lambda item: item[2])
            high = max(rows, key=lambda item: item[2])
            diff_apr = high[2] - low[2]
            if diff_apr >= settings.funding_diff_apr_threshold:
                out.append(
                    Opportunity(
                        kind="funding_diff",
                        venue=f"{low[0]}->{high[0]}",
                        symbol=symbol,
                        edge_bps=diff_apr * 10_000.0,
                        confidence=_confidence(diff_apr, settings.funding_diff_apr_threshold),
                        capacity_usd=settings.default_capacity_usd,
                        action=f"long {low[0]} perp, short {high[0]} perp",
                        why=(
                            f"cross-venue funding spread is {diff_apr:.1%} APR "
                            f"({low[0]} {low[2]:.1%} vs {high[0]} {high[2]:.1%})"
                        ),
                        details={
                            "long_venue": low[0],
                            "short_venue": high[0],
                            "long_apr": low[2],
                            "short_apr": high[2],
                        },
                    )
                )
        return out


class SpreadScanner:
    def __init__(self, exchange_factory: ExchangeFactory = ccxt_exchange_factory) -> None:
        self.exchange_factory = exchange_factory

    def scan(self, settings: ScannerSettings) -> list[Opportunity]:
        by_symbol: dict[str, list[dict[str, object]]] = {}
        for venue in settings.venues:
            try:
                exchange = self.exchange_factory(venue)
                raw = exchange.fetch_tickers(settings.symbols)
            except Exception as exc:
                log.warning("intel_spread_scan_failed", venue=venue, error=str(exc))
                continue
            for symbol, ticker in _ticker_map(raw).items():
                bid = _num(ticker.get("bid"))
                ask = _num(ticker.get("ask"))
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue
                by_symbol.setdefault(symbol, []).append(
                    {
                        "venue": venue,
                        "bid": bid,
                        "ask": ask,
                        "capacity_usd": _capacity_usd(ticker, bid, settings.default_capacity_usd),
                    }
                )

        out: list[Opportunity] = []
        for symbol, rows in by_symbol.items():
            if len(rows) < 2:
                continue
            buy = min(rows, key=lambda row: _float_value(row.get("ask"), math.inf))
            sell = max(rows, key=lambda row: _float_value(row.get("bid"), 0.0))
            if buy["venue"] == sell["venue"]:
                continue
            buy_ask = _num(buy.get("ask"))
            sell_bid = _num(sell.get("bid"))
            if buy_ask is None or sell_bid is None:
                continue
            mid = (buy_ask + sell_bid) / 2.0
            gross_bps = ((sell_bid - buy_ask) / mid) * 10_000.0
            fee_floor = settings.taker_fee_bps * 2.0 + settings.spread_buffer_bps
            net_bps = gross_bps - fee_floor
            if net_bps <= settings.min_spread_edge_bps:
                continue
            out.append(
                Opportunity(
                    kind="spread",
                    venue=f"{buy['venue']}->{sell['venue']}",
                    symbol=symbol,
                    edge_bps=net_bps,
                    confidence=_confidence(net_bps, max(fee_floor, 1.0)),
                    capacity_usd=min(
                        _float_value(buy.get("capacity_usd"), settings.default_capacity_usd),
                        _float_value(sell.get("capacity_usd"), settings.default_capacity_usd),
                    ),
                    action="buy low venue, sell high venue",
                    why=f"net cross-venue spread is {net_bps:.1f} bps after fees",
                    details={
                        "buy_venue": buy["venue"],
                        "sell_venue": sell["venue"],
                        "buy_ask": buy_ask,
                        "sell_bid": sell_bid,
                        "gross_bps": gross_bps,
                        "fee_floor_bps": fee_floor,
                    },
                )
            )
        return out


class BasisScanner:
    def __init__(self, exchange_factory: ExchangeFactory = ccxt_exchange_factory) -> None:
        self.exchange_factory = exchange_factory

    def scan(self, settings: ScannerSettings) -> list[Opportunity]:
        out: list[Opportunity] = []
        for contract in settings.basis_contracts:
            venue = contract.get("venue", settings.venues[0] if settings.venues else "binance")
            spot_symbol = contract.get("spot_symbol", contract.get("symbol", "BTC/USDT"))
            future_symbol = contract.get("future_symbol", "")
            if not future_symbol:
                continue
            try:
                exchange = self.exchange_factory(venue)
                spot = exchange.fetch_ticker(spot_symbol)
                future = exchange.fetch_ticker(future_symbol)
            except Exception as exc:
                log.warning("intel_basis_scan_failed", venue=venue, error=str(exc))
                continue
            spot_mid = _mid(spot)
            future_mid = _mid(future)
            if spot_mid is None or future_mid is None or spot_mid <= 0:
                continue
            days = _days_to_expiry(future, settings.basis_default_days)
            if days <= 0:
                continue
            basis = (future_mid - spot_mid) / spot_mid
            apr = basis * (365.0 / days)
            if apr < settings.basis_apr_threshold:
                continue
            out.append(
                Opportunity(
                    kind="basis",
                    venue=venue,
                    symbol=spot_symbol,
                    edge_bps=apr * 10_000.0,
                    confidence=_confidence(apr, settings.basis_apr_threshold),
                    capacity_usd=settings.default_capacity_usd,
                    action="long spot, short dated future",
                    why=f"dated futures basis is {apr:.1%} annualized",
                    details={
                        "spot_symbol": spot_symbol,
                        "future_symbol": future_symbol,
                        "spot_mid": spot_mid,
                        "future_mid": future_mid,
                        "days_to_expiry": days,
                        "basis": basis,
                    },
                )
            )
        return out


class DepegScanner:
    def __init__(self, exchange_factory: ExchangeFactory = ccxt_exchange_factory) -> None:
        self.exchange_factory = exchange_factory

    def scan(self, settings: ScannerSettings) -> list[Opportunity]:
        out: list[Opportunity] = []
        for venue in settings.venues:
            try:
                exchange = self.exchange_factory(venue)
            except Exception as exc:
                log.warning("intel_depeg_client_failed", venue=venue, error=str(exc))
                continue
            for symbol in settings.stable_symbols:
                try:
                    ticker = exchange.fetch_ticker(symbol)
                except Exception as exc:
                    log.warning(
                        "intel_depeg_scan_failed", venue=venue, symbol=symbol, error=str(exc)
                    )
                    continue
                mid = _mid(ticker)
                if mid is None or mid <= 0:
                    continue
                deviation_bps = abs(mid - 1.0) * 10_000.0
                if deviation_bps < settings.depeg_threshold_bps:
                    continue
                below = mid < 1.0
                out.append(
                    Opportunity(
                        kind="depeg",
                        venue=venue,
                        symbol=symbol,
                        edge_bps=deviation_bps,
                        confidence=_confidence(deviation_bps, settings.depeg_threshold_bps),
                        capacity_usd=settings.default_capacity_usd,
                        action="buy stable below peg" if below else "sell/redeem stable above peg",
                        why=f"{symbol} trades {deviation_bps:.1f} bps from peg on {venue}",
                        details={"mid": mid, "deviation_bps": deviation_bps, "below_peg": below},
                    )
                )
        return out


class WickScanner:
    def __init__(self, ohlcv_provider: OhlcvProvider | None = None) -> None:
        self.ohlcv_provider = ohlcv_provider

    def scan(self, settings: ScannerSettings) -> list[Opportunity]:
        if self.ohlcv_provider is None:
            return []
        try:
            ohlcv = self.ohlcv_provider(settings)
        except Exception as exc:
            log.warning("intel_wick_scan_failed", error=str(exc))
            return []
        if ohlcv.empty or not {"open", "high", "low", "close"}.issubset(ohlcv.columns):
            return []

        recent = ohlcv.tail(max(settings.wick_recent_bars, settings.flash_crash_bars + 1))
        close = recent["close"].astype("float64")
        body = (recent["close"] - recent["open"]).abs().replace(0, math.nan)
        lower_wick = recent[["open", "close"]].min(axis=1) - recent["low"]
        wick_ratio = (lower_wick / body).replace([math.inf, -math.inf], math.nan).fillna(0.0)
        wick_now = wick_cluster(
            wick_ratio,
            window=settings.wick_recent_bars,
            count=settings.wick_cluster_count,
            ratio_min=settings.wick_ratio_min,
        )
        crash_now = flash_crash(
            close,
            threshold_pct=settings.flash_crash_pct,
            bars=settings.flash_crash_bars,
        ).fillna(False)
        active = bool(wick_now.tail(settings.wick_recent_bars).any() or crash_now.iloc[-1])
        if not active:
            return []

        latest = recent.iloc[-1]
        rebound_bps = ((float(latest["close"]) - float(latest["low"])) / float(latest["close"])) * 10_000.0
        confidence = 0.55
        if bool(wick_now.iloc[-1]):
            confidence += 0.20
        if bool(crash_now.iloc[-1]):
            confidence += 0.20
        return [
            Opportunity(
                kind="wick",
                venue="market",
                symbol=settings.symbols[0] if settings.symbols else "BTC/USDT",
                edge_bps=max(0.0, rebound_bps),
                confidence=min(0.95, confidence),
                capacity_usd=settings.default_capacity_usd,
                action="arm deep limit ladder",
                why="wick regime detected: recent lower wick cluster or flash crash",
                details={
                    "latest_low": float(latest["low"]),
                    "latest_close": float(latest["close"]),
                    "latest_wick_ratio": float(wick_ratio.iloc[-1]),
                    "wick_cluster": bool(wick_now.iloc[-1]),
                    "flash_crash": bool(crash_now.iloc[-1]),
                },
            )
        ]


def _funding_records(raw: object) -> list[Mapping[str, object]]:
    if isinstance(raw, Mapping):
        items = list(raw.values())
    elif isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        items = list(raw)
    else:
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _str_list(value: object, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    out = [str(item) for item in value if isinstance(item, str | int | float)]
    return out or list(fallback)


def _float_value(value: object, fallback: float) -> float:
    parsed = _num(value)
    return fallback if parsed is None else parsed


def _int_value(value: object, fallback: int) -> int:
    parsed = _num(value)
    return fallback if parsed is None else int(parsed)


def _basis_contracts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        contract = {str(k): str(v) for k, v in item.items() if isinstance(k, str)}
        out.append(contract)
    return out


def _ticker_map(raw: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Mapping[str, object]] = {}
    for key, value in raw.items():
        if isinstance(value, Mapping):
            symbol = str(value.get("symbol") or key)
            out[symbol] = value
    return out


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _mid(ticker: Mapping[str, object]) -> float | None:
    bid = _num(ticker.get("bid"))
    ask = _num(ticker.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return _num(ticker.get("last"))


def _capacity_usd(
    ticker: Mapping[str, object],
    price: float,
    fallback: float,
) -> float:
    for key in ("bidVolume", "askVolume", "baseVolume"):
        amount = _num(ticker.get(key))
        if amount is not None and amount > 0:
            return amount * price
    quote = _num(ticker.get("quoteVolume"))
    return quote if quote is not None and quote > 0 else fallback


def _days_to_expiry(ticker: Mapping[str, object], fallback: float) -> float:
    expiry = _num(ticker.get("expiry"))
    if expiry is None:
        return fallback
    if expiry > 10_000_000_000:
        expiry /= 1000.0
    expiry_dt = datetime.fromtimestamp(expiry, tz=timezone.utc)
    return (expiry_dt - datetime.now(tz=timezone.utc)).total_seconds() / 86_400.0


def _confidence(value: float, threshold: float) -> float:
    if threshold <= 0:
        return 0.5
    return max(0.1, min(0.95, value / (threshold * 2.0)))
