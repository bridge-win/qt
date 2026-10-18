"""Small, cited strategy cards. Literature is context, not validation of our rules."""

from __future__ import annotations

from typing import Any

from .models import digest

SOURCES: dict[str, dict[str, Any]] = {
    "liu-tsyvinski-2018": {
        "title": "Risks and Returns of Cryptocurrency",
        "url": "https://www.nber.org/papers/w24877",
        "published_at": None, "publication_month": "2018-08", "available_at": "2026-10-18",
        "finding": "Historical cryptocurrency returns exhibit time-series momentum; this does not validate a 15-minute rule.",
    },
    "moskowitz-ooi-pedersen-2012": {
        "title": "Time Series Momentum",
        "url": "https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum",
        "published_at": "2012-05-01", "available_at": "2012-05-01",
        "finding": "Evidence concerns diversified futures and longer horizons, not a validated BTC intraday rule.",
    },
    "intraday-momentum-reversal-2022": {
        "title": "Intraday return predictability in the cryptocurrency markets: Momentum, reversal, or both",
        "url": "https://www.sciencedirect.com/science/article/pii/S1062940822000833",
        "published_at": None, "publication_year": 2022, "available_at": "2026-10-18",
        "finding": "Intraday momentum and reversal depend on market conditions; neither is an unconditional forecast. Exact publication date not retained; this card is not point-in-time historical evidence.",
    },
    "bailey-backtest-2015": {
        "title": "The Probability of Backtest Overfitting",
        "url": "https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
        "published_at": "2015-02-27", "available_at": "2015-02-27",
        "finding": "Selecting many strategies on a finite sample can overfit; retain all trials and independent evaluation.",
    },
    "binance-market-data": {
        "title": "Binance Market Data Only URLs",
        "url": "https://developers.binance.com/en/docs/products/spot/faqs/market_data_only",
        "published_at": None, "available_at": "2026-10-18",
        "finding": "Official keyless spot data; authentication and account state are not part of these endpoints.",
    },
    "okx-public-data": {
        "title": "OKX Public Data",
        "url": "https://app.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history",
        "published_at": None, "available_at": "2026-10-18",
        "finding": "Funding history has limited retention; unavailable historical costs cannot be replaced with zero.",
    },
}

CARDS: dict[str, dict[str, Any]] = {
    "trend": {
        "name": "趋势突破", "mechanism": "Continuation after a closed-bar range breakout with higher-timeframe alignment.",
        "sources": ["liu-tsyvinski-2018", "moskowitz-ooi-pedersen-2012"],
        "invalidation": "Range re-entry, opposing higher-timeframe trend, stale data or cost failure.",
        "counterexample": "A breakout may reverse in a range or after a liquidity shock.",
        "rule": "15m close beyond the prior lookback high/low; volume >= breakout_volume times prior median; 1h/4h EMA alignment.",
    },
    "recovery": {
        "name": "极端回撤恢复", "mechanism": "A price recovery after locally extreme selling, not oversold alone.",
        "sources": ["intraday-momentum-reversal-2022"],
        "invalidation": "Failure to recover the previously broken boundary or a new local low.",
        "counterexample": "Persistent bear trends can remain oversold and continue falling.",
        "rule": "Distress RSI and volume coincide in the latest three bars; close recovers that bar's prior range low and rises.",
    },
    "wick": {
        "name": "插针回收", "mechanism": "Closed-bar rejection outside the prior price range.",
        "sources": ["intraday-momentum-reversal-2022"],
        "invalidation": "Incomplete bar, no recovery into the old range, or renewed break of the extreme.",
        "counterexample": "An apparent wick can be a data error or the beginning of a trend.",
        "rule": "Wick/body, wick/range and range/ATR thresholds all pass; close returns into the prior range.",
    },
    "overheat": {
        "name": "过热转弱", "mechanism": "Loss of short-horizon strength after an extreme, conditioned on higher timeframes.",
        "sources": ["intraday-momentum-reversal-2022", "liu-tsyvinski-2018"],
        "invalidation": "Momentum resumes or higher-timeframe weakness is absent.",
        "counterexample": "Strong uptrends can remain overbought; RSI alone is not a short signal.",
        "rule": "Prior RSI >= overheat_rsi, current RSI < recovery threshold, close below prior three lows, 1h/4h weakness.",
    },
}


def strategy_cards() -> list[dict[str, Any]]:
    result = []
    for identifier, card in CARDS.items():
        value = {
            "strategy_id": identifier, **card, "asset": "BTC",
            "horizon": "4h-72h", "timeframes": ["15m", "1h", "4h"],
            "required_data": ["closed_ohlcv", "fresh_quote", "costs"],
            "rule_origin": "QT engineering hypothesis; numeric thresholds are not estimated by the cited papers",
            "validation_status": "unvalidated",
            "sources": [
                {"evidence_id": key, **SOURCES[key], "reviewed_at": "2026-10-18", "fetched_at": "2026-10-18",
                 "content_hash": digest(SOURCES[key]), "retained_content": "curated_summary"}
                for key in [*card["sources"], "bailey-backtest-2015"]
            ],
        }
        value["content_hash"] = digest(value)
        result.append(value)
    return result


def evidence_for(strategy_ids: list[str], *, perpetual: bool) -> list[dict[str, Any]]:
    cards = [card for card in strategy_cards() if card["strategy_id"] in strategy_ids]
    source = "okx-public-data" if perpetual else "binance-market-data"
    return [
        *[{"evidence_id": f"card:{card['strategy_id']}", **card} for card in cards],
        {"evidence_id": source, **SOURCES[source], "content_hash": digest(SOURCES[source])},
    ]
