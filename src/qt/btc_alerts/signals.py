"""Closed-bar research rules. Every crossing records its measurements and limits."""

from __future__ import annotations

import math
from itertools import pairwise
from statistics import median

from .knowledge import evidence_for
from .models import TIMEFRAMES, Bar, Candidate, Direction, Policy, Quote, Trigger, digest


def validate_bars(bars: list[Bar], timeframe: str, now: float, minimum: int = 210) -> None:
    seconds = TIMEFRAMES[timeframe]
    if len(bars) < minimum:
        raise ValueError("insufficient_closed_history")
    for index, bar in enumerate(bars):
        if bar.open_time % seconds or bar.open_time + seconds > now:
            raise ValueError("unaligned_or_unclosed_bar")
        if index and bar.open_time != bars[index - 1].open_time + seconds:
            raise ValueError("noncontiguous_bars")
    if not 0 <= now - bars[-1].open_time - seconds <= seconds + 90:
        raise ValueError("stale_bars")


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("insufficient_indicator_history")
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for item in values[period:]:
        value += alpha * (item - value)
    return value


def rsi(values: list[float], period: int = 14) -> list[float]:
    result = [50.0] * len(values)
    if len(values) <= period:
        return result
    changes = [b - a for a, b in pairwise(values)]
    gain = sum(max(value, 0) for value in changes[:period]) / period
    loss = sum(max(-value, 0) for value in changes[:period]) / period
    for index in range(period, len(values)):
        if index > period:
            change = changes[index - 1]
            gain = (gain * (period - 1) + max(change, 0)) / period
            loss = (loss * (period - 1) + max(-change, 0)) / period
        result[index] = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return result


def atr(bars: list[Bar], period: int = 14) -> float:
    ranges = [
        max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
        for previous, current in pairwise(bars)
    ]
    if len(ranges) < period:
        raise ValueError("insufficient_atr_history")
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def quote_is_current(quote: Quote, policy: Policy, now: float) -> bool:
    return (
        0 <= now - quote.observed_at <= policy.quote_max_age_seconds
        and (quote.source_at is None or -2 <= now - quote.source_at <= policy.quote_max_age_seconds)
        and quote.spread_bps <= policy.max_spread_bps
    )


def revalidate(candidate: Candidate, quote: Quote, policy: Policy, now: float) -> list[str]:
    reasons = []
    if candidate.strategy_version != policy.strategy_version:
        reasons.append("strategy_version_changed")
    if not candidate.bar_close <= now < candidate.expires_at:
        reasons.append("candidate_expired_or_future")
    if (candidate.venue, candidate.product, candidate.symbol) != (quote.venue, quote.product, quote.symbol):
        reasons.append("quote_identity_mismatch")
    if not quote_is_current(quote, policy, now):
        reasons.append("quote_stale_or_spread")
    allowed = min(policy.max_price_deviation, 0.25 * candidate.atr_1h / candidate.reference_price)
    if abs(quote.mid / candidate.reference_price - 1) > allowed:
        reasons.append("price_moved")
    if candidate.direction == "short":
        if quote.mark_price is None or quote.funding_cost_bps is None or quote.funding_as_of is None:
            reasons.append("perpetual_evidence_missing")
        elif not 0 <= now - quote.funding_as_of <= 24 * 3600:
            reasons.append("funding_stale")
    price = quote.ask if candidate.direction == "long" else quote.bid
    sign = 1 if candidate.direction == "long" else -1
    reward = sign * (candidate.target_price - price)
    risk = sign * (price - candidate.invalidation_price)
    current_cost_bps = policy.round_trip_cost_bps
    if candidate.direction == "short":
        current_cost_bps += quote.funding_cost_bps or 0
    cost = price * max(candidate.cost_bps, current_cost_bps) / 10_000
    if risk <= 0 or (reward - cost) / (risk + cost) < policy.min_net_reward_risk:
        reasons.append("net_reward_risk")
    return reasons


def evaluate(bars: dict[str, list[Bar]], quote: Quote, policy: Policy, now: float) -> list[Candidate]:
    for timeframe in TIMEFRAMES:
        validate_bars(bars[timeframe], timeframe, now)
    if not quote_is_current(quote, policy, now):
        raise ValueError("quote_stale_or_spread")
    fast = bars["15m"]
    latest, previous = fast[-1], fast[-2]
    t = policy.thresholds
    prior = fast[-t.lookback - 1:-1]
    lower, upper = min(item.low for item in prior), max(item.high for item in prior)
    prior_volume = median(item.volume for item in prior)
    ratio = latest.volume / prior_volume if prior_volume > 0 else 0
    strengths = rsi([item.close for item in fast])
    volatility = atr(bars["1h"])
    micro_atr = atr(fast[:-1])
    if min(volatility, micro_atr) <= 0:
        raise ValueError("zero_volatility")
    higher = [[item.close for item in bars[timeframe]] for timeframe in ("1h", "4h")]
    ema_pairs = [(ema(values, t.fast_ema), ema(values, t.slow_ema)) for values in higher]
    uptrend = all(a > b for a, b in ema_pairs)
    downtrend = all(a < b for a, b in ema_pairs)
    bullish: list[Trigger] = []
    bearish: list[Trigger] = []

    def trigger(identifier: str, observed: dict[str, float | bool], limits: dict[str, float]) -> Trigger:
        return Trigger.model_validate({"strategy_id": identifier, "observations": observed, "thresholds": limits})

    trend_observed: dict[str, float | bool] = {
        "close": latest.close, "prior_high": upper, "prior_low": lower,
        "volume_multiple": ratio, "ema_alignment_up": uptrend, "ema_alignment_down": downtrend,
        "ema_1h_fast": ema_pairs[0][0], "ema_1h_slow": ema_pairs[0][1],
        "ema_4h_fast": ema_pairs[1][0], "ema_4h_slow": ema_pairs[1][1],
    }
    if ratio >= t.breakout_volume:
        if latest.close > upper and uptrend:
            bullish.append(trigger("trend", trend_observed, {"volume_multiple": t.breakout_volume, "lookback": float(t.lookback)}))
        if latest.close < lower and downtrend:
            bearish.append(trigger("trend", trend_observed, {"volume_multiple": t.breakout_volume, "lookback": float(t.lookback)}))
    for index in range(len(fast) - 3, len(fast)):
        history = fast[index - t.lookback:index]
        boundary = min(item.low for item in history)
        volume = median(item.volume for item in history)
        multiple = fast[index].volume / volume if volume > 0 else 0
        if (
            strengths[index] <= t.distress_rsi and multiple >= t.distress_volume
            and fast[index].low < boundary < latest.close and latest.close > previous.close
        ):
            bullish.append(trigger("recovery", {
                "distress_rsi": strengths[index], "volume_multiple": multiple,
                "broken_boundary": boundary, "distress_low": fast[index].low,
                "close": latest.close, "previous_close": previous.close,
            }, {"rsi_max": t.distress_rsi, "volume_multiple": t.distress_volume}))
            break
    body = max(abs(latest.close - latest.open), latest.close * 1e-8)
    spread = latest.high - latest.low
    for direction, wick in (
        ("long", min(latest.open, latest.close) - latest.low),
        ("short", latest.high - max(latest.open, latest.close)),
    ):
        if (
            spread > 0 and wick >= t.wick_body * body and wick / spread >= t.wick_fraction
            and spread >= t.wick_atr * micro_atr and lower < latest.close < upper
            and (latest.low < lower if direction == "long" else latest.high > upper)
        ):
            item = trigger("wick", {
                "wick_body": wick / body, "wick_fraction": wick / spread,
                "range_atr": spread / micro_atr, "close": latest.close,
                "prior_low": lower, "prior_high": upper,
            }, {"wick_body": t.wick_body, "wick_fraction": t.wick_fraction, "range_atr": t.wick_atr})
            (bullish if direction == "long" else bearish).append(item)
    three_low = min(item.low for item in fast[-4:-1])
    if (
        strengths[-2] >= t.overheat_rsi and strengths[-1] < t.overheat_recovery_rsi
        and latest.close < three_low and downtrend
    ):
        bearish.append(trigger("overheat", {
            "previous_rsi": strengths[-2], "current_rsi": strengths[-1],
            "close": latest.close, "previous_three_low": three_low, "higher_timeframes_weak": downtrend,
        }, {"previous_rsi_min": t.overheat_rsi, "current_rsi_max": t.overheat_recovery_rsi}))
    if bullish and bearish:
        raise ValueError("opposing_triggers")
    groups: list[tuple[Direction, list[Trigger]]] = (
        [("short", bearish)] if quote.product == "perpetual"
        else [("long", bullish), ("reduce_long", bearish)]
    )
    snapshot_hash = digest({key: [item.model_dump() for item in value] for key, value in bars.items()})
    result = []
    for direction, triggers in groups:
        if not triggers:
            continue
        sign = 1 if direction == "long" else -1
        price = latest.close
        cost_bps = policy.round_trip_cost_bps
        missing = []
        if direction == "short":
            if quote.mark_price is None or quote.funding_cost_bps is None:
                missing.append("perpetual_mark_or_funding_missing")
            else:
                cost_bps += quote.funding_cost_bps
        target = price + sign * t.target_atr * volatility
        stop = price - sign * t.stop_atr * volatility
        if min(target, stop) <= 0:
            continue
        cost = price * cost_bps / 10_000
        net_rr = (t.target_atr * volatility - cost) / (t.stop_atr * volatility + cost)
        if not math.isfinite(net_rr):
            raise ValueError("nonfinite_reward_risk")
        evidence = evidence_for([item.strategy_id for item in triggers], perpetual=direction == "short")
        bar_close = latest.open_time + TIMEFRAMES["15m"]
        identity = {
            "strategy_version": policy.strategy_version, "venue": quote.venue,
            "product": quote.product, "symbol": quote.symbol, "direction": direction, "bar_close": bar_close,
        }
        result.append(Candidate(
            candidate_id=digest(identity)[:32], strategy_version=policy.strategy_version,
            venue=quote.venue, product=quote.product, symbol=quote.symbol,
            direction=direction, bar_close=bar_close,
            expires_at=bar_close + policy.candidate_ttl_seconds,
            reference_price=price, atr_1h=volatility, invalidation_price=stop,
            target_price=target, net_reward_risk=net_rr, cost_bps=cost_bps,
            triggers=triggers, data_snapshot_hash=snapshot_hash,
            evidence_ids=[str(item["evidence_id"]) for item in evidence] + ["market", "validation"],
            missing_evidence=missing,
        ))
    return result
