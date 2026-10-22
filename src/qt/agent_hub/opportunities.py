"""Bounded public-data screening and a tenant-scoped opportunity observation ledger.

Screens are research candidates, never trading approvals or calibrated forecasts.
Observation time is retained separately from exchange/event time. Raw bounded
public source payloads are archived with hashes for later replay and inspection.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import sqlite3
import statistics
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from qt.agent_hub.context import identifier
from qt.agent_hub.forecast_ledger import net_edge_payload
from qt.prediction_markets.polymarket import PublicDataError, parse_book, parse_market

BINANCE = "https://data-api.binance.vision"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
VERSION = "public-screen-v1"
MAX_BODY = 1_048_576


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid_public_number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError("invalid_public_number")
    return result


def _date(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except ValueError:
        return None


def _levels(payload: Any, field: str, *, prediction: bool) -> list[tuple[float, float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
        raise ValueError("invalid_orderbook")
    if len(payload[field]) > 10000:
        raise ValueError("orderbook_too_large")
    levels = []
    for row in payload[field]:
        if prediction:
            price, quantity = row["price"], row["size"]
        else:
            price, quantity = row[:2]
        price, quantity = _number(price, positive=True), _number(quantity)
        if prediction and price >= 1:
            raise ValueError("invalid_prediction_price")
        if quantity:
            levels.append((price, quantity))
    return sorted(levels, reverse=field == "bids")


def book_liquidity(
    payload: dict[str, Any], *, prediction: bool, quote_budget: float = 25
) -> dict[str, Any]:
    """Walk asks for the target notional; never substitute midpoint for a fill."""
    quote_budget = _number(quote_budget, positive=True)
    if quote_budget > 1000:
        raise ValueError("quote_budget_exceeds_screen_limit")
    bids = _levels(payload, "bids", prediction=prediction)
    asks = _levels(payload, "asks", prediction=prediction)
    if not bids or not asks:
        raise ValueError("orderbook_not_two_sided")
    bid, ask = bids[0][0], asks[0][0]
    if bid > ask:
        raise ValueError("crossed_orderbook")
    spent, shares = 0.0, 0.0
    for price, quantity in asks:
        take = min(quantity, max(0.0, quote_budget - spent) / price)
        spent += take * price
        shares += take
        if spent >= quote_budget - 1e-8:
            break
    complete = spent >= quote_budget - 1e-8
    return {
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "spread_bps": (ask - bid) / ((ask + bid) / 2) * 10000,
        "bid_depth_quote": sum(p * q for p, q in bids),
        "ask_depth_quote": sum(p * q for p, q in asks),
        "target_quote": quote_budget,
        "depth_sufficient": complete,
        "buy_vwap": spent / shares if complete else None,
        "impact_bps": (spent / shares / ask - 1) * 10000 if complete else None,
        "depth_scope": "returned_book_levels_only",
    }


def btc_features(rows: Any, *, as_of: float) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("invalid_candles")
    closed = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError("invalid_candle")
        stamp, close_time = _number(row[0]), _number(row[6])
        values = [_number(item, positive=True) for item in row[1:5]]
        volume = _number(row[5])
        opening, high, low, close = values
        if low > min(opening, close) or high < max(opening, close) or low > high:
            raise ValueError("invalid_candle_ohlc")
        if close_time - stamp != 86_399_999:
            raise ValueError("unexpected_candle_interval")
        if close_time / 1000 < as_of:
            closed.append((stamp, close_time, close, high, volume))
    if len(closed) < 61 or any(b[0] - a[0] != 86_400_000 for a, b in itertools.pairwise(closed)):
        raise ValueError("insufficient_or_gapped_closed_candles")
    if as_of - closed[-1][1] / 1000 > 172800:
        raise ValueError("daily_history_stale")
    closes = [item[2] for item in closed]
    log_returns = [math.log(b / a) for a, b in itertools.pairwise(closes[-31:])]
    fast, slow = statistics.mean(closes[-20:]), statistics.mean(closes[-60:])
    prior_high = max(item[3] for item in closed[-21:-1])
    return {
        "closed_bars": len(closed),
        "last_close_at": closed[-1][1] / 1000,
        "close": closes[-1],
        "return_7d": closes[-1] / closes[-8] - 1,
        "return_30d": closes[-1] / closes[-31] - 1,
        "realized_volatility_30d": statistics.stdev(log_returns) * math.sqrt(365),
        "sma20": fast,
        "sma60": slow,
        "trend": "up" if fast > slow else "down",
        "breakout_20d": closes[-1] > prior_high,
        "signal_is_forecast": False,
    }


def prediction_costs(info: Any, token: str, price: float) -> dict[str, Any]:
    """Only the currently documented linear fee curve is estimated.

    Legacy /fee-rate base_fee is deliberately not treated as a percentage.
    Unknown/exponent-changing schemas stay unknown instead of assuming zero.
    """
    unknown = {"known": False, "fee_per_share": None, "reason": "fee_model_unverified"}
    if not math.isfinite(price) or not 0 < price < 1:
        return {**unknown, "reason": "invalid_prediction_price"}
    if not isinstance(info, dict) or not isinstance(info.get("t"), list):
        return unknown
    if token not in {str(item.get("t")) for item in info["t"] if isinstance(item, dict)}:
        return {**unknown, "reason": "fee_token_mapping_mismatch"}
    details = info.get("fd")
    if not isinstance(details, dict):
        return unknown
    try:
        rate, exponent = _number(details.get("r")), _number(details.get("e"))
    except (TypeError, ValueError):
        return unknown
    if rate > 1 or exponent != 1:
        return {**unknown, "reason": "fee_curve_not_supported"}
    return {
        "known": True,
        "rate": rate,
        "exponent": exponent,
        "fee_per_share": rate * price * (1 - price),
        "formula": "rate * price * (1 - price)",
        "scope": "entry_taker_fee_estimate_before_protocol_rounding",
    }


class OpportunityScanner:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    def scan(
        self, tenant_id: str, *, limit: int = 5, deadline_seconds: float = 45
    ) -> dict[str, Any]:
        return asyncio.run(
            self.scan_async(tenant_id, limit=limit, deadline_seconds=deadline_seconds)
        )

    async def scan_async(
        self, tenant_id: str, *, limit: int = 5, deadline_seconds: float = 45
    ) -> dict[str, Any]:
        identifier(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("scan_limit_must_be_1_to_10")
        if not math.isfinite(deadline_seconds) or not 1 <= deadline_seconds <= 55:
            raise ValueError("scan_deadline_must_be_1_to_55_seconds")
        result: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "tenant_id": tenant_id,
            "version": VERSION,
            "started_at": time.time(),
            "candidates": [],
            "errors": [],
            "sources": [],
            "execution_authorized": False,
            "universe": {
                "btc": "BTCUSDT",
                "polymarket": "bounded_active_market_sample",
                "market_limit": limit,
            },
        }
        started = time.monotonic()
        if self.client:
            await self._bounded(self.client, result, limit, deadline_seconds)
        else:
            async with httpx.AsyncClient(
                timeout=8, follow_redirects=False, trust_env=False
            ) as client:
                await self._bounded(client, result, limit, deadline_seconds)
        result["generated_at"] = time.time()
        result["duration_seconds"] = time.monotonic() - started
        result["status"] = "partial" if result["errors"] else "complete"
        if not result["candidates"]:
            result["status"] = "unavailable"
        for candidate in result["candidates"]:
            if result["generated_at"] > candidate["expires_at"]:
                candidate["status"] = "blocked"
                candidate["reasons"].append("snapshot_expired_during_scan")
            candidate["observation_id"] = _hash(candidate)
        result["sha256"] = _hash(result)
        return result

    async def _bounded(
        self, client: httpx.AsyncClient, result: dict[str, Any], limit: int, seconds: float
    ) -> None:
        try:
            await asyncio.wait_for(self._collect(client, result, limit), timeout=seconds)
        except asyncio.TimeoutError:
            result["errors"].append({"source": "scan", "reason": "scan_deadline_exceeded"})

    async def _get(
        self,
        client: httpx.AsyncClient,
        result: dict[str, Any],
        url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        async with client.stream(
            "GET", url, params=params, timeout=8, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"public_http_{response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise ValueError("public_response_too_large")
        data = json.loads(body)
        stamp = time.time()
        receipt = {
            "url": str(response.url),
            "observed_at": stamp,
            "available_at": stamp,
            "sha256": hashlib.sha256(body).hexdigest(),
            "payload_sha256": _hash(data),
        }
        result["sources"].append({**receipt, "payload": data})
        return data, receipt

    async def _collect(self, client: httpx.AsyncClient, result: dict[str, Any], limit: int) -> None:
        # Two independent venue fetches; total runtime is bounded by wait_for.
        await asyncio.gather(self._btc(client, result), self._prediction(client, result, limit))

    def _error(self, result: dict[str, Any], source: str, error: Exception) -> None:
        reason = (
            str(error) if isinstance(error, (ValueError, PublicDataError)) else type(error).__name__
        )
        # Public upstream text and URLs are not copied into error logs.
        if (
            not reason
            or len(reason) > 100
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
                for char in reason
            )
        ):
            reason = "invalid_public_data"
        result["errors"].append({"source": source, "reason": reason})

    async def _btc(self, client: httpx.AsyncClient, result: dict[str, Any]) -> None:
        try:
            rows, history_source = await self._get(
                client,
                result,
                BINANCE + "/api/v3/klines",
                {"symbol": "BTCUSDT", "interval": "1d", "limit": 100},
            )
            features = btc_features(rows, as_of=history_source["observed_at"])
            depth, depth_source = await self._get(
                client, result, BINANCE + "/api/v3/depth", {"symbol": "BTCUSDT", "limit": 100}
            )
            liquidity = book_liquidity(depth, prediction=False)
            reasons = []
            if not liquidity["depth_sufficient"]:
                reasons.append("insufficient_depth")
            if liquidity["spread_bps"] > 20:
                reasons.append("wide_spread")
            result["candidates"].append(
                {
                    "id": _hash(["binance", "BTCUSDT"]),
                    "venue": "binance",
                    "instrument": "BTCUSDT",
                    "title": "BTC public spot regime and liquidity screen",
                    "category": "spot_regime",
                    "status": "blocked" if reasons else "research_required",
                    "reasons": reasons,
                    "observed_at": depth_source["observed_at"],
                    "available_at": depth_source["available_at"],
                    "expires_at": depth_source["observed_at"] + 60,
                    "exchange_timestamp": None,
                    "timestamp_basis": "REST receipt time; depth endpoint provides no exchange timestamp",
                    "features": features,
                    "liquidity": liquidity,
                    "costs": {"known": False, "reason": "account_tier_fee_not_publicly_verified"},
                    **net_edge_payload(
                        {
                            "estimated_probability": None,
                            "best_ask": None,
                            "fee_at_p": None,
                        }
                    ),
                    "execution_authorized": False,
                    "research_requirements": [
                        "registered_strategy",
                        "cost_aware_out_of_sample_validation",
                        "paper_forward_test",
                    ],
                    "sources": [history_source, depth_source],
                }
            )
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
        ) as error:
            self._error(result, "binance", error)

    async def _prediction(
        self, client: httpx.AsyncClient, result: dict[str, Any], limit: int
    ) -> None:
        try:
            rows, gamma_source = await self._get(
                client,
                result,
                GAMMA + "/markets",
                {
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError("invalid_market_list")
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError) as error:
            self._error(result, "polymarket_discovery", error)
            return
        for row in rows:
            try:
                market = parse_market(row, row.get("slug", ""), time.time())
                if not market.active:
                    continue
                maturity = _date(row.get("endDate"))
                info, info_source = None, None
                try:
                    info, info_source = await self._get(
                        client, result, CLOB + "/clob-markets/" + market.condition_id
                    )
                except (httpx.HTTPError, ValueError, TypeError) as error:
                    self._error(result, "polymarket_fee_info", error)
                for outcome in market.outcomes:
                    await self._outcome(
                        client,
                        result,
                        row,
                        market,
                        outcome,
                        maturity,
                        info,
                        [gamma_source, *([info_source] if info_source else [])],
                    )
            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
                OverflowError,
            ) as error:
                self._error(result, "polymarket_market", error)

    async def _outcome(
        self,
        client: httpx.AsyncClient,
        result: dict[str, Any],
        row: dict[str, Any],
        market: Any,
        outcome: Any,
        maturity: float | None,
        info: Any,
        sources: list[dict[str, Any]],
    ) -> None:
        try:
            body, source = await self._get(
                client, result, CLOB + "/book", {"token_id": outcome.token_id}
            )
            quote = parse_book(body, market, outcome, source["observed_at"])
            liquidity = book_liquidity(body, prediction=True)
            price = liquidity["buy_vwap"] or liquidity["ask"]
            costs = prediction_costs(info, outcome.token_id, price)
            reasons = [*quote.reasons]
            if market.restricted:
                reasons.append("market_restrictions_apply")
            if not liquidity["depth_sufficient"]:
                reasons.append("insufficient_depth")
            if liquidity["spread"] > 0.05:
                reasons.append("wide_spread")
            if not costs["known"]:
                reasons.append(costs["reason"])
            horizon = maturity - source["observed_at"] if maturity is not None else None
            if horizon is None or not 3600 <= horizon <= 180 * 86400:
                reasons.append("maturity_unknown_or_outside_research_window")
            if not row.get("resolutionSource") or not row.get("description"):
                reasons.append("resolution_rules_unverified")
            result["candidates"].append(
                {
                    "id": _hash(["polymarket", market.condition_id, outcome.token_id]),
                    "venue": "polymarket",
                    "instrument": outcome.token_id,
                    "condition_id": market.condition_id,
                    "slug": market.slug,
                    "title": market.question,
                    "outcome": outcome.name,
                    "category": "prediction_event",
                    "status": "blocked" if reasons else "research_required",
                    "reasons": list(dict.fromkeys(reasons)),
                    "observed_at": source["observed_at"],
                    "available_at": source["available_at"],
                    "expires_at": min(source["observed_at"] + 15, (quote.timestamp or 0) + 15),
                    "exchange_timestamp": quote.timestamp,
                    "maturity_at": maturity,
                    "horizon_seconds": horizon,
                    "liquidity": liquidity,
                    "costs": costs,
                    "market_probability": quote.midpoint,
                    **net_edge_payload(
                        {
                            "estimated_probability": None,
                            "best_ask": quote.ask,
                            "fee_at_p": costs.get("fee_per_share"),
                        }
                    ),
                    "execution_authorized": False,
                    "resolution": {
                        "source": str(row.get("resolutionSource", ""))[:2000],
                        "rules_sha256": _hash(row.get("description", "")),
                    },
                    "research_requirements": [
                        "independent_time_gated_forecast",
                        "resolved_event_calibration",
                        "settlement_and_fee_review",
                        "book_replay_and_paper_forward_test",
                    ],
                    "sources": [*sources, source],
                }
            )
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
        ) as error:
            self._error(result, "polymarket_orderbook", error)


class OpportunityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS opportunity_scans (
                    tenant TEXT NOT NULL, id TEXT NOT NULL, generated_at REAL NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(tenant,id));
                CREATE TABLE IF NOT EXISTS opportunity_observations (
                    tenant TEXT NOT NULL, id TEXT NOT NULL, observation_id TEXT NOT NULL,
                    scan_id TEXT NOT NULL, observed_at REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(tenant,observation_id));
                CREATE INDEX IF NOT EXISTS opportunity_latest ON opportunity_observations(tenant,id,observed_at DESC);
                CREATE TABLE IF NOT EXISTS opportunity_reviews (
                    seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, id TEXT NOT NULL,
                    status TEXT NOT NULL, note TEXT NOT NULL, reviewed_at REAL NOT NULL);
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, tenant_id: str, scan: dict[str, Any]) -> dict[str, Any]:
        identifier(tenant_id)
        if scan.get("tenant_id") != tenant_id or scan.get("execution_authorized") is not False:
            raise ValueError("invalid_scan_tenant_or_authorization")
        if scan.get("sha256") != _hash({k: v for k, v in scan.items() if k != "sha256"}):
            raise ValueError("scan_hash_mismatch")
        for item in scan["candidates"]:
            if item.get("observation_id") != _hash(
                {k: v for k, v in item.items() if k != "observation_id"}
            ):
                raise ValueError("observation_hash_mismatch")
            if item.get("execution_authorized") is not False or item.get("net_edge") is not None:
                raise ValueError("screen_cannot_authorize_trading_or_claim_alpha")
        for source in scan["sources"]:
            if source.get("payload_sha256") != _hash(source.get("payload")):
                raise ValueError("source_payload_hash_mismatch")
        with self._db() as db:
            existing = db.execute(
                "SELECT payload FROM opportunity_scans WHERE tenant=? AND id=?",
                (tenant_id, scan["id"]),
            ).fetchone()
            if existing and existing[0] != _json(scan):
                raise ValueError("immutable_scan_identity_collision")
            db.execute(
                "INSERT OR IGNORE INTO opportunity_scans VALUES(?,?,?,?)",
                (tenant_id, scan["id"], scan["generated_at"], _json(scan)),
            )
            for item in scan["candidates"]:
                db.execute(
                    "INSERT OR IGNORE INTO opportunity_observations VALUES(?,?,?,?,?,?)",
                    (
                        tenant_id,
                        item["id"],
                        item["observation_id"],
                        scan["id"],
                        item["observed_at"],
                        _json(item),
                    ),
                )
        return {k: v for k, v in scan.items() if k != "sources"}

    def list(self, tenant_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
        identifier(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_candidate_limit")
        with self._db() as db:
            rows = db.execute(
                """SELECT payload,scan_id FROM (
                SELECT payload,scan_id,observed_at,ROW_NUMBER() OVER(PARTITION BY id ORDER BY observed_at DESC,rowid DESC) AS rn
                FROM opportunity_observations WHERE tenant=?) WHERE rn=1 ORDER BY observed_at DESC LIMIT ?""",
                (tenant_id, limit),
            ).fetchall()
        return [
            {**self._current(tenant_id, json.loads(row[0])), "source_scan_id": row[1]}
            for row in rows
        ]

    def _current(self, tenant: str, value: dict[str, Any]) -> dict[str, Any]:
        value["stale"] = time.time() > value["expires_at"]
        with self._db() as db:
            review = db.execute(
                "SELECT status,note,reviewed_at FROM opportunity_reviews WHERE tenant=? AND id=? ORDER BY seq DESC LIMIT 1",
                (tenant, value["id"]),
            ).fetchone()
        value["review"] = dict(review) if review else None
        return value

    def get(self, tenant_id: str, candidate_id: str) -> dict[str, Any] | None:
        identifier(tenant_id)
        with self._db() as db:
            row = db.execute(
                "SELECT payload,scan_id FROM opportunity_observations WHERE tenant=? AND id=? ORDER BY observed_at DESC,rowid DESC LIMIT 1",
                (tenant_id, candidate_id),
            ).fetchone()
        return (
            {**self._current(tenant_id, json.loads(row[0])), "source_scan_id": row[1]}
            if row
            else None
        )

    def summary(self, tenant_id: str) -> dict[str, Any]:
        identifier(tenant_id)
        with self._db() as db:
            latest = db.execute(
                "SELECT payload FROM opportunity_scans WHERE tenant=? ORDER BY generated_at DESC,rowid DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            count = db.execute(
                "SELECT COUNT(*) FROM opportunity_scans WHERE tenant=?", (tenant_id,)
            ).fetchone()[0]
        scan = json.loads(latest[0]) if latest else None
        return {
            "scan_count": count,
            "generated_at": scan["generated_at"] if scan else None,
            "latest_scan": {k: v for k, v in scan.items() if k not in {"sources", "candidates"}}
            if scan
            else None,
            "candidates": self.list(tenant_id),
            "execution_authorized": False,
        }

    def latest_scan(self, tenant_id: str) -> dict[str, Any] | None:
        identifier(tenant_id)
        with self._db() as db:
            row = db.execute(
                "SELECT payload FROM opportunity_scans WHERE tenant=? ORDER BY generated_at DESC,rowid DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return {k: v for k, v in json.loads(row[0]).items() if k != "sources"} if row else None

    def scan_detail(self, tenant_id: str, scan_id: str) -> dict[str, Any] | None:
        identifier(tenant_id)
        with self._db() as db:
            row = db.execute(
                "SELECT payload FROM opportunity_scans WHERE tenant=? AND id=?",
                (tenant_id, scan_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def review(
        self, tenant_id: str, candidate_id: str, *, status: str, note: str = ""
    ) -> dict[str, Any]:
        if (
            status not in {"watching", "rejected", "archived"}
            or not isinstance(note, str)
            or len(note) > 2000
        ):
            raise ValueError("invalid_research_review")
        if self.get(tenant_id, candidate_id) is None:
            raise ValueError("candidate_not_found")
        with self._db() as db:
            db.execute(
                "INSERT INTO opportunity_reviews(tenant,id,status,note,reviewed_at) VALUES(?,?,?,?,?)",
                (tenant_id, candidate_id, status, note, time.time()),
            )
        return self.get(tenant_id, candidate_id) or {}
