"""News NLP sentiment tests."""

from __future__ import annotations

import pandas as pd

from qt.data.nlp import NewsItem, aggregate_daily, news_panic_z, score_headline


def test_score_bearish_negative() -> None:
    s = score_headline("Bitcoin crashes as exchange goes bankrupt amid liquidation panic")
    assert s < -0.5


def test_score_bullish_positive() -> None:
    s = score_headline("Bitcoin ETF approval triggers rally and recovery")
    assert s > 0.3


def test_negation_flips_polarity() -> None:
    pos = score_headline("Bitcoin rally")
    neg = score_headline("Bitcoin not a rally")
    assert pos > neg


def test_aggregate_daily_volume() -> None:
    items = [
        NewsItem(ts=pd.Timestamp("2024-01-01 10:00", tz="UTC"), title="bitcoin crash"),
        NewsItem(ts=pd.Timestamp("2024-01-01 14:00", tz="UTC"), title="bitcoin plunge"),
        NewsItem(ts=pd.Timestamp("2024-01-02 10:00", tz="UTC"), title="bitcoin rally"),
    ]
    df = aggregate_daily(items)
    assert df.shape[0] == 2
    assert df["news_volume"].iloc[0] == 2
    assert df["news_sentiment"].iloc[0] < df["news_sentiment"].iloc[1]


def test_news_panic_z_negative_on_drop() -> None:
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    # Stable mildly-positive sentiment then a single panic day.
    senti = pd.Series([0.5] * 29 + [-0.9], index=idx)
    z = news_panic_z(senti, window=20)
    assert z.dropna().iloc[-1] < -1
