# Migrated verbatim from btcqt; source attribution is recorded in src/qt/workbench/assets/migration-manifest.json.
"""Read-only market assessment derived from the live state journal.

The assessment is deliberately separate from order generation.  It explains
the same S0/S1/S2 gates for humans, but cannot emit an Intent or change runtime
configuration.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import aiohttp
import pandas as pd

from .config import Config

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
EXTERNAL_CACHE_SEC = 15 * 60
EXTERNAL_TIMEOUT_SEC = 5
EXTERNAL_MAX_BYTES = 2_000_000


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None and self._text is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = None


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2_000:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _clean_text(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_number(value: str) -> float | None:
    normalized = value.replace("$", "").replace(",", "").replace("−", "-").strip()
    if normalized in {"", "-", "—", "N/A", "n/a"}:
        return None
    negative = normalized.startswith("(") and normalized.endswith(")")
    if negative:
        normalized = normalized[1:-1]
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    parsed = float(match.group())
    return -abs(parsed) if negative else parsed


def parse_farside_etf(html: str) -> dict[str, Any]:
    parser = _TableParser()
    parser.feed(html)
    total_index: int | None = None
    data_rows: list[list[str]] = []
    for row in parser.rows:
        lowered = [cell.casefold() for cell in row]
        if total_index is None and "total" in lowered:
            total_index = lowered.index("total")
            continue
        data_rows.append(row)
    if total_index is None:
        raise ValueError("ETF table header is unavailable")
    date_formats = ("%d %b %Y", "%d %b %y", "%d-%b-%Y", "%Y-%m-%d")
    parsed_rows: list[tuple[datetime, float]] = []
    for row in data_rows:
        if len(row) <= total_index:
            continue
        parsed_date = None
        for fmt in date_formats:
            try:
                parsed_date = datetime.strptime(row[0].strip(), fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        total = _parse_number(row[total_index])
        if parsed_date is not None and total is not None:
            parsed_rows.append((parsed_date, total))
    if not parsed_rows:
        raise ValueError("ETF table has no dated total row")
    parsed_rows.sort(key=lambda item: item[0])
    session, total = parsed_rows[-1]
    return {
        "session_date": session.date().isoformat(),
        "net_flow_usd_m": total,
        "unit": "USD millions",
        "history": [
            {"date": date.date().isoformat(), "net_flow_usd_m": value}
            for date, value in parsed_rows[-30:]
        ],
    }


def _xml_values(element: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for child in element.iter():
        name = child.tag.rsplit("}", 1)[-1]
        if child.text and child.text.strip():
            values[name] = child.text.strip()
    return values


def parse_treasury_yields(xml: str) -> dict[str, Any]:
    root = ET.fromstring(xml)
    rows = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] != "entry":
            continue
        values = _xml_values(entry)
        date = values.get("NEW_DATE")
        two_year = _parse_number(values.get("BC_2YEAR", ""))
        ten_year = _parse_number(values.get("BC_10YEAR", ""))
        if date and (two_year is not None or ten_year is not None):
            rows.append((date, two_year, ten_year))
    if not rows:
        raise ValueError("Treasury yield feed has no observations")
    date, two_year, ten_year = rows[-1]
    return {
        "session_date": date[:10], "two_year_pct": two_year, "ten_year_pct": ten_year,
        "history": [
            {"date": row_date[:10], "two_year_pct": row_two, "ten_year_pct": row_ten}
            for row_date, row_two, row_ten in rows[-30:]
        ],
    }


def parse_rss(xml: str, *, limit: int = 5) -> list[dict[str, str]]:
    root = ET.fromstring(xml)
    items = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        values = _xml_values(item)
        title = _clean_text(values.get("title"))
        link = _safe_url(values.get("link"))
        if not link:
            for child in item.iter():
                if child.tag.rsplit("}", 1)[-1] == "link":
                    link = _safe_url(child.attrib.get("href"))
                    if link:
                        break
        published = _clean_text(values.get("pubDate") or values.get("published") or values.get("updated"), 80)
        if title and link:
            items.append({"title": title, "url": link, "published": published})
        if len(items) >= limit:
            break
    if not items:
        raise ValueError("RSS feed has no usable items")
    return items


def parse_relevant_rss(xml: str, *, keywords: tuple[str, ...], limit: int = 5) -> list[dict[str, str]]:
    items = parse_rss(xml, limit=30)
    selected = [item for item in items if any(keyword in item["title"].casefold() for keyword in keywords)]
    if not selected:
        raise ValueError("RSS feed has no relevant monetary or liquidity items")
    return selected[:limit]


def parse_treasury_releases(html: str, *, limit: int = 5) -> list[dict[str, str]]:
    parser = _LinkParser()
    parser.feed(html)
    keywords = ("buyback", "refunding", "debt", "financing", "borrowing", "auction", "liquidity")
    items = []
    seen = set()
    for href, title in parser.links:
        if not title or not href.startswith("/news/press-releases/"):
            continue
        if not any(keyword in title.casefold() for keyword in keywords):
            continue
        url = f"https://home.treasury.gov{href}"
        if url in seen:
            continue
        seen.add(url)
        items.append({"title": _clean_text(title), "url": url, "published": ""})
        if len(items) >= limit:
            break
    if not items:
        raise ValueError("Treasury release page has no relevant financing items")
    return items


def parse_gdelt(body: str, *, limit: int = 8) -> list[dict[str, str]]:
    raw = json.loads(body)
    items = []
    for article in raw.get("articles") or []:
        title = _clean_text(article.get("title"))
        url = _safe_url(article.get("url"))
        if not title or not url:
            continue
        items.append({
            "title": title,
            "url": url,
            "published": _clean_text(article.get("seendate"), 40),
            "domain": _clean_text(article.get("domain"), 100),
        })
        if len(items) >= limit:
            break
    if not items:
        raise ValueError("news index has no usable items")
    return items


@dataclass(frozen=True)
class _Source:
    name: str
    url: str
    parser: Any


class ExternalEvidenceCache:
    """Keyless external evidence with non-blocking refresh and stale fallback."""

    def __init__(self, ttl_sec: int = EXTERNAL_CACHE_SEC) -> None:
        self.ttl_sec = ttl_sec
        self._sources: dict[str, dict[str, Any]] = {}
        self._last_attempt_ms: int | None = None
        self._task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        stale = self._last_attempt_ms is None or now_ms - self._last_attempt_ms >= self.ttl_sec * 1000
        if stale and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.refresh())
        sources = copy.deepcopy(self._sources)
        if not sources:
            return {"status": "loading", "updated_ms": None, "sources": {}}
        statuses = {source.get("status") for source in sources.values()}
        overall = "available" if "available" in statuses else "stale" if "stale" in statuses else "unavailable"
        updated = max((source.get("updated_ms") or 0 for source in sources.values()), default=0) or None
        return {"status": overall, "updated_ms": updated, "sources": sources}

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url, headers={"User-Agent": "btc-qt-assessment/0.3"}) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > EXTERNAL_MAX_BYTES:
                    raise ValueError("external response exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks).decode(response.charset or "utf-8", errors="replace")

    async def refresh(self) -> None:
        now_ms = int(time.time() * 1000)
        year = datetime.now(timezone.utc).year
        sources = [
            _Source("etf", "https://farside.co.uk/bitcoin-etf-flow-all-data/", parse_farside_etf),
            _Source(
                "treasury_yields",
                "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
                f"?data=daily_treasury_yield_curve&field_tdr_date_value={year}",
                parse_treasury_yields,
            ),
            _Source(
                "fed", "https://www.federalreserve.gov/feeds/press_all.xml",
                lambda body: parse_relevant_rss(
                    body,
                    keywords=("monetary", "federal open market", "interest rate", "minutes", "inflation", "liquidity"),
                ),
            ),
            _Source(
                "treasury_news", "https://home.treasury.gov/news/press-releases",
                parse_treasury_releases,
            ),
            _Source(
                "catalysts",
                "https://api.gdeltproject.org/api/v2/doc/doc?query=bitcoin&mode=artlist"
                "&maxrecords=12&format=json&sort=datedesc",
                parse_gdelt,
            ),
        ]
        timeout = aiohttp.ClientTimeout(total=EXTERNAL_TIMEOUT_SEC)
        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            results = await asyncio.gather(
                *(self._load_source(session, source) for source in sources),
                return_exceptions=True,
            )
        for source, result in zip(sources, results):
            previous = self._sources.get(source.name)
            if isinstance(result, Exception):
                error = _clean_text(result, 160)
                if previous and previous.get("data") is not None:
                    self._sources[source.name] = {**previous, "status": "stale", "error": error}
                else:
                    self._sources[source.name] = {
                        "status": "unavailable", "updated_ms": None, "source_url": source.url,
                        "data": None, "error": error,
                    }
                log.warning("assessment source %s failed: %s", source.name, error)
                continue
            self._sources[source.name] = {
                "status": "available", "updated_ms": now_ms, "source_url": source.url,
                "data": result, "error": None,
            }
        self._last_attempt_ms = now_ms

    async def _load_source(self, session: aiohttp.ClientSession, source: _Source) -> Any:
        return source.parser(await self._fetch_text(session, source.url))


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _gate(code: str, current: object, threshold: object, passed: bool,
          *, status: str | None = None) -> dict[str, Any]:
    return {"code": code, "current": current, "threshold": threshold,
            "passed": bool(passed), "status": status or ("pass" if passed else "fail")}


def _risk_blocked(risk: dict[str, Any]) -> bool:
    return any(bool(risk.get(key)) for key in (
        "killed", "halted_day", "halted_week", "halted_system", "paused", "data_stale",
    ))


def _permissions(regime: str) -> dict[str, bool]:
    return {
        "s0": regime != "CASCADE",
        "s1_buy": regime != "TREND_DOWN",
        "s1_sell": regime != "TREND_UP",
        "s2": regime in {"RANGE", "POST_CASCADE"},
    }


def _history_stats(rows: pd.DataFrame, as_of_ms: int, current_price: float) -> tuple[dict[str, Any], float]:
    prices = rows[["ts", "price"]].dropna().sort_values("ts") if not rows.empty and "price" in rows else pd.DataFrame()
    result: dict[str, Any] = {}
    for label, days in (("24h", 1), ("3d", 3), ("7d", 7)):
        start = as_of_ms - days * DAY_MS
        window = prices[(prices.ts >= start) & (prices.ts <= as_of_ms)] if not prices.empty else prices
        before = prices[prices.ts <= start].tail(1) if not prices.empty else prices
        if before.empty and not window.empty:
            before = window.head(1)
        reference = _finite(before.iloc[-1].price) if not before.empty else None
        change = ((current_price / reference) - 1) * 100 if reference and reference > 0 else None
        result[label] = {
            "change_pct": change,
            "low": _finite(window.price.min()) if not window.empty else None,
            "high": _finite(window.price.max()) if not window.empty else None,
            "from_ms": int(before.iloc[-1].ts) if not before.empty else None,
        }
    if prices.empty:
        return result, 0.0
    coverage = min(1.0, max(0.0, (as_of_ms - int(prices.ts.min())) / (7 * DAY_MS)))
    return result, round(coverage, 4)


def _funding_streak(rows: pd.DataFrame, threshold: float, *, high: bool) -> int:
    if rows.empty or "funding_pctl" not in rows or "ts" not in rows:
        return 0
    selected = rows[["ts", "funding_pctl"]].dropna().sort_values("ts")
    if selected.empty:
        return 0
    selected = selected.assign(bucket=(selected.ts.astype("int64") // (8 * 3_600_000)))
    latest = selected.groupby("bucket", sort=True).tail(1)
    streak = 0
    for value in reversed(latest.funding_pctl.tolist()):
        extreme = value >= threshold if high else value <= threshold
        if not extreme:
            break
        streak += 1
    return streak


def _confidence(fresh: bool, coverage: float, external: dict[str, Any]) -> str:
    available = sum(1 for source in external.get("sources", {}).values()
                    if source.get("status") == "available")
    if fresh and coverage >= 0.95 and available >= 2:
        return "high"
    if fresh and coverage >= 3 / 7:
        return "medium"
    return "low"


def build_assessment(snapshot: dict[str, Any], risk: dict[str, Any], age_sec: float | None,
                     rows: pd.DataFrame, cfg: Config,
                     external: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a language-neutral explanation payload for the Web UI."""
    external = external or {"status": "unavailable", "updated_ms": None, "sources": {}}
    now_ms = int(time.time() * 1000)
    as_of_ms = int(snapshot.get("ts") or now_ms)
    price = _finite(snapshot.get("price")) or 0.0
    fresh = age_sec is not None and age_sec < 90 and as_of_ms > 0
    warmed = bool(snapshot.get("warmed_up"))
    blocked = _risk_blocked(risk)
    regime = str(snapshot.get("regime") or "RANGE")
    permissions = _permissions(regime)
    spread = _finite(snapshot.get("spread_pctl"))
    spread_ok = spread is None or spread < cfg.risk.spread_guard_pctl
    trend_score = _finite(snapshot.get("trend_score"))
    trend_agree = int(snapshot.get("trend_agree") or 0)
    donchian = int(snapshot.get("donchian_break") or 0)
    rv_pctl = _finite(snapshot.get("rv_pctl"))
    dev = _finite(snapshot.get("dev"))
    vel5 = _finite(snapshot.get("vel5"))
    funding_pctl = _finite(snapshot.get("funding_pctl"))
    oi_pctl = _finite(snapshot.get("open_interest_pctl"))
    returns, coverage = _history_stats(rows, as_of_ms, price)

    s0_common = cfg.s0.enabled and warmed and fresh and not blocked and permissions["s0"] and spread_ok
    s0_buy = bool(s0_common and trend_score is not None
                  and trend_score >= cfg.s0.min_abs_t and trend_agree >= cfg.s0.min_agree
                  and donchian == 1)
    s0_sell = bool(s0_common and trend_score is not None
                   and trend_score <= -cfg.s0.min_abs_t and trend_agree >= cfg.s0.min_agree
                   and donchian == -1)
    s0_gates = [
        _gate("enabled", cfg.s0.enabled, True, cfg.s0.enabled),
        _gate("trend_strength", trend_score, f"abs >= {cfg.s0.min_abs_t}",
              trend_score is not None and abs(trend_score) >= cfg.s0.min_abs_t),
        _gate("trend_agreement", trend_agree, f">= {cfg.s0.min_agree}", trend_agree >= cfg.s0.min_agree),
        _gate("bearish_breakout", donchian, -1, donchian == -1),
        _gate("regime_permission", regime, "not CASCADE", permissions["s0"]),
        _gate("spread_guard", spread, f"< {cfg.risk.spread_guard_pctl}", spread_ok),
    ]

    event_dir = str(snapshot.get("event_dir") or "none")
    event_verdict = str(snapshot.get("event_verdict") or "none")
    event_vwap = _finite(snapshot.get("event_vwap"))
    s1_sell_direction = event_dir == "up"
    s1_sell_edge = event_vwap is not None and event_vwap < price
    s1_sell = bool(cfg.s1.enabled and warmed and fresh and not blocked and spread_ok
                   and bool(snapshot.get("event_active")) and event_verdict == "reject"
                   and s1_sell_direction and permissions["s1_sell"] and s1_sell_edge)
    s1_status = "ready" if s1_sell else "missed" if (
        event_verdict == "reject" and s1_sell_direction and event_vwap is not None and not s1_sell_edge
    ) else "waiting"
    s1_gates = [
        _gate("enabled", cfg.s1.enabled, True, cfg.s1.enabled),
        _gate("up_event_rejected", f"{event_dir}:{event_verdict}", "up:reject",
              bool(snapshot.get("event_active")) and event_verdict == "reject" and s1_sell_direction),
        _gate("sell_regime_permission", regime, "S1 sell allowed", permissions["s1_sell"]),
        _gate("vwap_downside_room", event_vwap, f"< price {price}", s1_sell_edge,
              status="missed" if s1_status == "missed" else None),
        _gate("spread_guard", spread, f"< {cfg.risk.spread_guard_pctl}", spread_ok),
    ]

    high_streak = _funding_streak(rows, cfg.s2.funding_pctl_hi, high=True)
    s2_sell = bool(high_streak >= cfg.s2.min_persist_intervals
                   and oi_pctl is not None and oi_pctl >= cfg.s2.oi_pctl_min
                   and dev is not None and dev > 0.5 and vel5 is not None and vel5 <= 0
                   and permissions["s2"] and spread_ok)
    s2_gates = [
        _gate("research_only", cfg.s2.enabled, False, not cfg.s2.enabled, status="research"),
        _gate("funding_persistence", high_streak, f">= {cfg.s2.min_persist_intervals}",
              high_streak >= cfg.s2.min_persist_intervals),
        _gate("oi_crowding", oi_pctl, f">= {cfg.s2.oi_pctl_min}",
              oi_pctl is not None and oi_pctl >= cfg.s2.oi_pctl_min),
        _gate("upward_stretch", dev, "> 0.5", dev is not None and dev > 0.5),
        _gate("momentum_stalled", vel5, "<= 0", vel5 is not None and vel5 <= 0),
        _gate("regime_permission", regime, "RANGE or POST_CASCADE", permissions["s2"]),
    ]

    high_vol = rv_pctl is not None and rv_pctl >= cfg.s0.high_vol_rv_pctl
    overheat = high_vol or (dev is not None and dev > 0.5) or (
        _finite(snapshot.get("fng")) or 0) >= 75

    if not fresh or not warmed or price <= 0:
        tactical_status = swing_status = "data_insufficient"
    else:
        tactical_status = (
            "bearish_confirmed" if s0_sell else
            "pullback_risk" if s1_sell or s2_sell else
            "overheated_wait" if overheat else
            "bullish_continuation" if s0_buy else
            "overheated_wait"
        )
        if s0_sell:
            swing_status = "bearish_confirmed"
        elif trend_score is not None and trend_score <= -cfg.s0.min_abs_t and trend_agree >= cfg.s0.min_agree:
            swing_status = "pullback_risk"
        elif trend_score is not None and trend_score >= cfg.s0.min_abs_t and trend_agree >= cfg.s0.min_agree:
            swing_status = "bullish_continuation"
        else:
            swing_status = "overheated_wait"

    confidence = _confidence(fresh, coverage, external)
    tactical_reasons = ["s0_bearish_confirmed"] if s0_sell else []
    if s1_sell:
        tactical_reasons.append("s1_rejected_up_event")
    if s1_status == "missed":
        tactical_reasons.append("s1_target_already_crossed")
    if s2_sell:
        tactical_reasons.append("s2_shadow_crowding")
    if high_vol:
        tactical_reasons.append("extreme_realized_volatility")
    if s0_buy:
        tactical_reasons.append("s0_bullish_breakout")
    if not tactical_reasons:
        tactical_reasons.append("no_complete_bearish_setup")

    change_24h = _finite(returns.get("24h", {}).get("change_pct"))
    doi_1h = _finite(snapshot.get("doi_1h"))
    liq_pctl = _finite(snapshot.get("liq_oi_pctl"))
    etf_source = external.get("sources", {}).get("etf", {})
    etf_flow = _finite((etf_source.get("data") or {}).get("net_flow_usd_m"))
    drivers = [
        {
            "code": "price_breakout", "status": "support" if change_24h is not None and change_24h > 2 else "mixed",
            "metrics": {"change_24h_pct": change_24h, "donchian_break": donchian,
                        "trend_score": trend_score},
        },
        {
            "code": "short_squeeze", "status": "support" if (
                change_24h is not None and change_24h > 0 and
                ((doi_1h is not None and doi_1h < 0) or (liq_pctl is not None and liq_pctl >= 95))
            ) else "mixed",
            "metrics": {"doi_1h": doi_1h, "liq_oi_pctl": liq_pctl,
                        "cascade_dir": snapshot.get("cascade_dir")},
        },
        {
            "code": "follow_through", "status": "support" if s0_buy or (
                trend_score is not None and trend_score > 0 and trend_agree >= cfg.s0.min_agree
            ) else "mixed",
            "metrics": {"trend_agree": trend_agree, "etf_net_flow_usd_m": etf_flow,
                        "etf_status": etf_source.get("status", "loading")},
        },
        {
            "code": "overheat_risk", "status": "risk" if overheat else "clear",
            "metrics": {"rv_pctl": rv_pctl, "funding_pctl": funding_pctl,
                        "open_interest_pctl": oi_pctl, "dev": dev},
        },
    ]

    return {
        "as_of_ms": as_of_ms,
        "generated_ms": now_ms,
        "freshness": {"age_sec": age_sec, "fresh": fresh, "history_coverage_7d": coverage},
        "price": price,
        "returns": returns,
        "verdicts": {
            "tactical": {
                "horizon": "1_3_days", "status": tactical_status, "confidence": confidence,
                "reasons": tactical_reasons,
                "invalidations": ["fresh_s1_sell_with_edge", "s0_downside_breakout", "data_stale"],
            },
            "swing": {
                "horizon": "1_4_weeks", "status": swing_status, "confidence": confidence,
                "reasons": ["multi_horizon_trend", "positioning_not_signal", "external_context_only"],
                "invalidations": ["trend_score_reversal", "downside_donchian", "persistent_leverage_crowding"],
            },
        },
        "strategies": {
            "s0": {"buy_ready": s0_buy, "sell_ready": s0_sell, "status": "ready" if s0_buy or s0_sell else "waiting", "gates": s0_gates},
            "s1": {"sell_ready": s1_sell, "status": s1_status, "gates": s1_gates},
            "s2": {"sell_shadow": s2_sell, "status": "research", "gates": s2_gates},
        },
        "drivers": drivers,
        "levels": {
            "price": price,
            "ema_anchor": _finite(snapshot.get("ema_anchor")),
            "event_vwap": event_vwap,
            "event_extreme": _finite(snapshot.get("event_extreme")),
            "high_24h": returns.get("24h", {}).get("high"),
            "low_24h": returns.get("24h", {}).get("low"),
            "high_7d": returns.get("7d", {}).get("high"),
            "low_7d": returns.get("7d", {}).get("low"),
        },
        "external": external,
        "limitations": [
            "research_only", "external_context_not_execution", "etf_daily_not_realtime",
            "news_headlines_not_causal_proof", "s2_disabled_in_production",
        ],
    }

