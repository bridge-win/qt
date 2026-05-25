"""Lightweight news / social sentiment NLP.

Pure-Python (no transformers / spaCy) so the package stays small. We use
a hand-curated bullish/bearish lexicon plus VADER-style polarity scoring
on titles. Good enough as a regime gate; replace with FinBERT / sentence
embeddings when a heavier ML pipeline is justified.

References:
- Hutto & Gilbert (2014), VADER (a parsimonious rule-based model for
  sentiment analysis of social media text).
- Loughran & McDonald (2011) financial lexicon — supplements VADER with
  domain-specific polarity (used the LM negative dictionary inspiration).
- Da, Engelberg, Gao (2011) "In Search of Attention" — Google Trends-style
  attention spikes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

# A compact, domain-tuned lexicon. Weights in ~[-2, +2].
LEXICON: dict[str, float] = {
    # bullish
    "rally": 1.5, "surge": 1.6, "soar": 1.7, "breakout": 1.3, "bullish": 1.4,
    "bottom": 1.0, "buy": 0.8, "accumulate": 1.0, "halving": 0.9,
    "etf": 1.0, "approval": 1.4, "adoption": 1.0, "all-time": 1.5,
    "recover": 1.2, "rebound": 1.4, "support": 0.6, "upgrade": 0.8,
    # bearish
    "crash": -2.0, "plunge": -1.9, "collapse": -2.0, "tumble": -1.6, "dump": -1.5,
    "selloff": -1.7, "panic": -2.0, "fear": -1.4, "bearish": -1.4,
    "liquidate": -1.5, "liquidation": -1.5, "bankruptcy": -2.0, "hack": -1.8,
    "exploit": -1.7, "scam": -1.6, "ban": -1.5, "lawsuit": -1.3, "subpoena": -1.0,
    "regulation": -0.4, "sec": -0.5, "sue": -1.0, "fraud": -1.8,
    "default": -1.5, "insolvent": -2.0, "freeze": -1.4, "wipeout": -1.9,
    # neutral but informative
    "halt": -0.8, "delay": -0.6, "investigation": -0.7,
}


# Negation handling (simple)
NEGATIONS = {"not", "no", "never", "without", "n't"}
TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]+")


@dataclass(frozen=True)
class NewsItem:
    ts: pd.Timestamp
    title: str
    source: str = ""


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def score_headline(text: str) -> float:
    """Return a polarity score in roughly [-1, 1]."""

    tokens = _tokenize(text)
    if not tokens:
        return 0.0
    raw = 0.0
    n_hits = 0
    for i, tok in enumerate(tokens):
        w = LEXICON.get(tok)
        if w is None:
            continue
        flip = -1.0 if i > 0 and tokens[i - 1] in NEGATIONS else 1.0
        # ALL-CAPS amplifier in the original text (not lowered)
        amp = 1.25 if any(tok.upper() in text.split() for _ in [1]) else 1.0
        raw += w * flip * amp
        n_hits += 1
    if n_hits == 0:
        return 0.0
    # Squash to [-1, 1] via tanh; division by sqrt(n) reduces multi-keyword bias.
    return math.tanh(raw / math.sqrt(n_hits))


def aggregate_daily(items: Iterable[NewsItem]) -> pd.DataFrame:
    """Daily sentiment + volume aggregates.

    Output columns: ``news_sentiment``, ``news_volume`` indexed by UTC day.
    """

    rows: list[tuple[pd.Timestamp, float]] = []
    for it in items:
        rows.append((pd.Timestamp(it.ts).tz_convert("UTC"), score_headline(it.title)))
    if not rows:
        return pd.DataFrame(columns=["news_sentiment", "news_volume"])
    df = pd.DataFrame(rows, columns=["ts", "score"]).set_index("ts").sort_index()
    daily = df["score"].resample("1D").agg(["mean", "count"])
    daily.columns = ["news_sentiment", "news_volume"]
    return daily


def news_panic_z(daily_sentiment: pd.Series, window: int = 30) -> pd.Series:
    """Z-score of news sentiment; <= -2 indicates panic news cluster."""

    import numpy as np
    mu = daily_sentiment.rolling(window).mean()
    sd = daily_sentiment.rolling(window).std(ddof=0).replace(0, np.nan)
    return ((daily_sentiment - mu) / sd).rename("news_sentiment_z")
