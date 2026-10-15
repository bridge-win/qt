"""Bounded official Gamma/CLOB public reads. No wallets, signatures, or orders."""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
MAX_BYTES = 1024 * 1024
MAX_AGE_SECONDS = 15.0
FUTURE_TOLERANCE_SECONDS = 2.0
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CONDITION = re.compile(r"0x[0-9a-fA-F]{64}")
_TOKEN = re.compile(r"[0-9]{1,78}")


class PublicDataError(ValueError):
    """Sanitized error; never includes upstream bodies or untrusted URLs."""


class OutcomeQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str
    token_id: str
    bid: float | None = Field(default=None, ge=0, le=1)
    ask: float | None = Field(default=None, ge=0, le=1)
    midpoint: float | None = Field(default=None, ge=0, le=1)
    timestamp: float | None = None
    ready: bool = False
    reasons: list[str] = Field(default_factory=list)


class PredictionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    exchange: Literal["polymarket"] = "polymarket"
    environment: Literal["production-public"] = "production-public"
    product: Literal["prediction"] = "prediction"
    slug: str
    question: str
    condition_id: str
    version: Literal["v1", "v2"]
    read_at: float
    active: bool
    restricted: bool = False
    ready: bool = False
    reasons: list[str] = Field(default_factory=list)
    outcomes: list[OutcomeQuote]
    live_execution_enabled: Literal[False] = False


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not 1 <= len(slug) <= 200 or not _SLUG.fullmatch(slug):
        raise PublicDataError("invalid_market_slug")
    return slug


def _array(value: Any, *, encoded: bool = False) -> list[Any]:
    if encoded and isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            raise PublicDataError("invalid_market_array") from None
    if not isinstance(value, list):
        raise PublicDataError("invalid_market_array")
    return value


def parse_market(payload: Any, slug: str, now: float) -> PredictionSnapshot:
    validate_slug(slug)
    if not isinstance(payload, dict) or payload.get("slug") != slug:
        raise PublicDataError("market_identity_mismatch")
    condition = payload.get("conditionId")
    if not isinstance(condition, str) or not _CONDITION.fullmatch(condition):
        raise PublicDataError("invalid_condition_id")
    # Missing version is the legacy Gamma v1 schema, never a v2 heuristic.
    version = payload.get("version", "v1")
    if not isinstance(version, str) or version not in {"v1", "v2"}:
        raise PublicDataError("unsupported_market_version")
    names = _array(payload.get("outcomes"), encoded=True)
    tokens = _array(
        payload.get("positionIds") if version == "v2" else payload.get("clobTokenIds"),
        encoded=version == "v1",
    )
    if len(names) != 2 or len(tokens) != len(names):
        raise PublicDataError("unsupported_outcome_mapping")
    if any(not isinstance(name, str) or not 1 <= len(name) <= 200 for name in names):
        raise PublicDataError("invalid_outcome_names")
    if len(set(names)) != len(names) or len(set(str(token) for token in tokens)) != len(tokens):
        raise PublicDataError("duplicate_outcome_identity")
    if any(not isinstance(token, str) or not _TOKEN.fullmatch(token) or int(token) >= 2**256 for token in tokens):
        raise PublicDataError("invalid_outcome_token")
    question = payload.get("question")
    if not isinstance(question, str) or not 1 <= len(question) <= 2000:
        raise PublicDataError("invalid_market_question")
    for field in ("active", "closed", "acceptingOrders"):
        if not isinstance(payload.get(field), bool):
            raise PublicDataError("missing_market_status")
    for field in ("archived", "restricted", "enableOrderBook"):
        if field in payload and not isinstance(payload[field], bool):
            raise PublicDataError("invalid_market_status")
    reasons = []
    if not payload["active"]:
        reasons.append("market_inactive")
    if payload["closed"]:
        reasons.append("market_closed")
    if not payload["acceptingOrders"]:
        reasons.append("market_not_accepting_orders")
    if payload.get("archived", False):
        reasons.append("market_archived")
    if payload.get("enableOrderBook", True) is False:
        reasons.append("orderbook_disabled")
    return PredictionSnapshot(
        slug=slug, question=question, condition_id=condition, version=cast(Literal["v1", "v2"], version),
        read_at=now, active=not reasons, restricted=payload.get("restricted", False),
        reasons=[*reasons, *(["market_restrictions_apply"] if payload.get("restricted", False) else [])],
        outcomes=[OutcomeQuote(name=name, token_id=token) for name, token in zip(names, tokens, strict=True)],
    )


def _finite(value: Any, *, price: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PublicDataError("invalid_book_number")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise PublicDataError("invalid_book_number") from None
    if not math.isfinite(result) or result < 0 or (price and result > 1):
        raise PublicDataError("invalid_book_number")
    return result


def parse_book(payload: Any, market: PredictionSnapshot, outcome: OutcomeQuote, now: float) -> OutcomeQuote:
    if not isinstance(payload, dict) or payload.get("asset_id") != outcome.token_id or payload.get("market") != market.condition_id:
        raise PublicDataError("orderbook_identity_mismatch")
    stamp = _finite(payload.get("timestamp")) / 1000
    bids: list[float] = []
    asks: list[float] = []
    for field, prices in (("bids", bids), ("asks", asks)):
        rows = payload.get(field)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise PublicDataError("invalid_orderbook_levels")
        for row in rows:
            if not isinstance(row, dict):
                raise PublicDataError("invalid_orderbook_level")
            price = _finite(row.get("price"), price=True)
            size = _finite(row.get("size"))
            if size > 0:
                prices.append(price)
    bid, ask = max(bids, default=None), min(asks, default=None)
    if bid is not None and ask is not None and bid > ask:
        raise PublicDataError("crossed_orderbook")
    reasons = []
    if now - stamp > MAX_AGE_SECONDS:
        reasons.append("orderbook_stale")
    if stamp > now + FUTURE_TOLERANCE_SECONDS:
        reasons.append("orderbook_future")
    if bid is None or ask is None:
        reasons.append("orderbook_not_two_sided")
    return outcome.model_copy(update={
        "bid": bid, "ask": ask,
        "midpoint": (bid + ask) / 2 if bid is not None and ask is not None else None,
        "timestamp": stamp, "ready": not reasons, "reasons": reasons,
    })


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None) -> Any:
    try:
        async with client.stream("GET", url, params=params, follow_redirects=False) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise PublicDataError("public_response_too_large")
        return json.loads(raw)
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
        raise PublicDataError("public_market_data_unavailable") from None


async def fetch_snapshot(slug: str, *, client: httpx.AsyncClient | None = None) -> PredictionSnapshot:
    validate_slug(slug)
    if client is None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10), limits=httpx.Limits(max_connections=2), follow_redirects=False) as owned:
            return await fetch_snapshot(slug, client=owned)
    market = parse_market(await _get_json(client, f"{GAMMA_URL}/markets/slug/{slug}"), slug, time.time())
    if not market.active:
        return market
    quotes = []
    for outcome in market.outcomes:
        try:
            body = await _get_json(client, f"{CLOB_URL}/book", {"token_id": outcome.token_id})
            quotes.append(parse_book(body, market, outcome, time.time()))
        except PublicDataError:
            quotes.append(outcome.model_copy(update={"reasons": ["orderbook_unavailable"]}))
    market.outcomes = quotes
    market.read_at = time.time()
    # Fetching the second outcome must not renew the first one's lifetime.
    for quote in market.outcomes:
        if quote.timestamp is not None and market.read_at - quote.timestamp > MAX_AGE_SECONDS:
            quote.ready = False
            quote.reasons = list(dict.fromkeys([*quote.reasons, "orderbook_stale"]))
    market.ready = all(quote.ready for quote in quotes)
    market.reasons = [*market.reasons, *([] if market.ready else ["outcome_data_unready"])]
    return market
