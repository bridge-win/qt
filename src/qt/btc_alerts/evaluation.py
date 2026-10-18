"""Reproducible numeric research and prospective labels, never historical AI claims."""

from __future__ import annotations

import bisect
import json
import math
import random
import time
from collections import defaultdict
from itertools import pairwise
from statistics import mean
from typing import Any

from .market import BINANCE, PublicMarket
from .models import TIMEFRAMES, Bar, Candidate, Policy, Quote, digest
from .signals import evaluate
from .store import Store, encode


def resample(bars: list[Bar], seconds: int) -> list[Bar]:
    groups: dict[int, list[Bar]] = defaultdict(list)
    for bar in bars:
        groups[bar.open_time // seconds * seconds].append(bar)
    result = []
    for timestamp, rows in sorted(groups.items()):
        expected = list(range(timestamp, timestamp + seconds, 900))
        if [row.open_time for row in rows] != expected:
            continue
        result.append(Bar(
            open_time=timestamp, open=rows[0].open, high=max(row.high for row in rows),
            low=min(row.low for row in rows), close=rows[-1].close,
            volume=sum(row.volume for row in rows),
        ))
    return result


def outcome(candidate: Candidate, bars: list[Bar], policy: Policy) -> dict[str, Any]:
    end = candidate.bar_close + policy.horizon_hours * 3600
    # A full bar of delay prevents assuming a fill at an already-past opening price.
    future = [bar for bar in bars if candidate.bar_close + 900 <= bar.open_time < end]
    if not future or future[-1].open_time + 900 < end:
        return {"status": "incomplete_outcome"}
    if any(b.open_time != a.open_time + 900 for a, b in pairwise(future)):
        return {"status": "incomplete_outcome"}
    if future[0].open_time != candidate.bar_close + 900:
        return {"status": "incomplete_outcome"}
    entry = future[0].open
    allowed = min(policy.max_price_deviation, 0.25 * candidate.atr_1h / candidate.reference_price)
    if abs(entry / candidate.reference_price - 1) > allowed:
        return {"status": "no_entry_price_moved"}
    sign = 1 if candidate.direction == "long" else -1
    risk = sign * (entry - candidate.invalidation_price)
    if risk <= 0:
        return {"status": "no_entry_invalidated"}
    exit_price, exited_at, reason = future[-1].close, end, "time_exit"
    for bar in future:
        if sign * (bar.open - candidate.invalidation_price) <= 0:
            exit_price, exited_at, reason = bar.open, bar.open_time, "gap_stop"
            break
        stop_hit = bar.low <= candidate.invalidation_price if sign == 1 else bar.high >= candidate.invalidation_price
        target_hit = bar.high >= candidate.target_price if sign == 1 else bar.low <= candidate.target_price
        # OHLC cannot establish which happened first: choose the adverse outcome.
        if stop_hit:
            exit_price, exited_at, reason = candidate.invalidation_price, bar.open_time + 900, "stop"
            break
        if target_hit:
            exit_price, exited_at, reason = candidate.target_price, bar.open_time + 900, "target"
            break
    cost = entry * candidate.cost_bps / 10000
    result = (sign * (exit_price - entry) - cost) / (risk + cost)
    return {
        "status": "labelled", "net_r": result, "entry_price": entry, "exit_price": exit_price,
        "entry_at": future[0].open_time, "exit_at": exited_at, "reason": reason,
        "label": "avoided_loss_vs_btc_hold" if candidate.direction == "reduce_long" else "virtual_directional_return",
        "funding_complete": candidate.direction != "short",
    }


def confidence_interval(rows: list[dict[str, Any]], *, seed: int = 7, samples: int = 5000) -> tuple[float, float]:
    weeks: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        weeks[int(row["entry_at"]) // (7 * 86400)].append(float(row["net_r"]))
    groups = list(weeks.values())
    if len(groups) < 8:
        return (-math.inf, math.inf)
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        selected = [value for _ in groups for value in rng.choice(groups)]
        estimates.append(mean(selected))
    estimates.sort()
    return estimates[int(samples * 0.025)], estimates[min(samples - 1, int(samples * 0.975))]


def trend_baseline(bars: list[Bar], test_start: int, policy: Policy) -> dict[str, Any]:
    """Long/cash EMA20/50 baseline with closed hours, delayed fills and costs."""
    hours = resample(bars, 3600)
    signals: list[bool] = []
    closes: list[int] = []
    fast = slow = 0.0
    for index, bar in enumerate(hours):
        if index == 19:
            fast = mean(item.close for item in hours[:20])
        elif index > 19:
            fast += 2 / 21 * (bar.close - fast)
        if index == 49:
            slow = mean(item.close for item in hours[:50])
        elif index > 49:
            slow += 2 / 51 * (bar.close - slow)
        signals.append(index >= 49 and fast > slow)
        closes.append(bar.open_time + 3600)
    equity = peak = 1.0
    drawdown = 0.0
    holding = False
    previous_close: float | None = None
    switches = 0
    one_side_cost = policy.round_trip_cost_bps / 2 / 10000
    for bar in bars:
        if bar.open_time < test_start:
            continue
        # Signal was available a full 15 minutes before this virtual entry.
        index = bisect.bisect_right(closes, bar.open_time - 900) - 1
        desired = index >= 0 and signals[index]
        if holding and previous_close is not None:
            equity *= bar.open / previous_close
        if desired != holding:
            equity *= 1 - one_side_cost
            switches += 1
        holding = desired
        if holding:
            equity *= bar.close / bar.open
        previous_close = bar.close
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    if holding:
        equity *= 1 - one_side_cost
        switches += 1
        drawdown = max(drawdown, 1 - equity / peak)
    return {
        "rule": "spot long/cash; closed 1h EMA20 > EMA50; 15m delay; no Codex",
        "net_return": equity - 1, "max_drawdown": drawdown,
        "side_transactions": switches, "costs_included": True,
    }


def download_spot(market: PublicMarket, days: int, cutoff: int) -> list[Bar]:
    cursor = (cutoff - days * 86400) * 1000
    rows: list[Bar] = []
    for _ in range(math.ceil(days * 96 / 1000) + 2):
        data = market.get(BINANCE + "/klines", {
            "symbol": "BTCUSDT", "interval": "15m", "startTime": cursor,
            "endTime": cutoff * 1000 - 1, "limit": 1000,
        })
        if not data:
            break
        new = [Bar(open_time=int(row[0]) // 1000, open=float(row[1]), high=float(row[2]),
                   low=float(row[3]), close=float(row[4]), volume=float(row[5])) for row in data]
        if new[0].open_time * 1000 < cursor:
            raise ValueError("history_cursor_regressed")
        rows.extend(new)
        cursor = (new[-1].open_time + 900) * 1000
        if cursor >= cutoff * 1000:
            break
        time.sleep(0.15)
    if (
        not rows or rows[-1].open_time + 900 != cutoff
        or any(b.open_time != a.open_time + 900 for a, b in pairwise(rows))
    ):
        raise ValueError("historical_coverage_incomplete")
    return rows


def research_results(bars: list[Bar], policy: Policy, cutoff: int) -> dict[str, Any]:
    test_start = cutoff - 180 * 86400
    if not bars or bars[0].open_time > test_start - 365 * 86400:
        raise ValueError("at_least_545_days_history_required")
    series = {"15m": bars, "1h": resample(bars, 3600), "4h": resample(bars, 14400)}
    closing = {key: [bar.open_time + TIMEFRAMES[key] for bar in value] for key, value in series.items()}
    labels: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    next_entry: dict[tuple[str, str], float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for bar in bars:
        as_of = bar.open_time + 900
        if as_of < test_start or as_of + policy.horizon_hours * 3600 > cutoff:
            continue
        packet = {}
        for timeframe, timeframe_bars in series.items():
            index = bisect.bisect_right(closing[timeframe], as_of)
            packet[timeframe] = timeframe_bars[max(0, index - 300):index]
        quote = Quote(venue="binance", product="spot", symbol="BTCUSDT", bid=bar.close, ask=bar.close, observed_at=float(as_of))
        try:
            candidates = evaluate(packet, quote, policy, float(as_of))
        except ValueError as error:
            counts[str(error)] += 1
            continue
        for candidate in candidates:
            counts["triggered_candidates"] += 1
            if candidate.net_reward_risk < policy.min_net_reward_risk:
                counts["cost_gate_rejected"] += 1
                continue
            index = bisect.bisect_left(closing["15m"], as_of)
            value = outcome(candidate, bars[index:index + policy.horizon_hours * 4 + 2], policy)
            counts[value["status"]] += 1
            if value["status"] != "labelled":
                continue
            for trigger in candidate.triggers:
                key = trigger.strategy_id, candidate.direction
                if value["entry_at"] < next_entry[key]:
                    counts["overlapping_events_purged"] += 1
                    continue
                labels[key].append({**value, "candidate_id": candidate.candidate_id})
                next_entry[key] = value["exit_at"] + 900
    results = []
    for strategy in ("trend", "recovery", "wick", "overheat"):
        for direction in ("long", "reduce_long", "short"):
            rows = labels[strategy, direction]
            low, high = confidence_interval(rows)
            valid = len(rows) >= 100 and low > 0 and direction != "short"
            results.append({
                "strategy_id": strategy, "direction": direction, "status": "passed" if valid else "insufficient_evidence",
                "oos_events": len(rows), "mean_net_r": mean([row["net_r"] for row in rows]) if rows else None,
                "ci95_lower": low if math.isfinite(low) else None,
                "ci95_upper": high if math.isfinite(high) else None,
                "costs_included": True, "purged": True, "funding_complete": direction != "short",
                "reason": "historical_perpetual_cost_data_not_available" if direction == "short" else "frozen_oos_numeric_rules_only",
                "outcomes_sha256": digest(rows),
            })
    test = [bar for bar in bars if bar.open_time >= test_start]
    hold = test[-1].close / test[0].open - 1 - policy.round_trip_cost_bps / 10000
    return {
        "schema_version": 1, "strategy_version": policy.strategy_version,
        "data_sha256": digest([bar.model_dump() for bar in bars]),
        "train_end": test_start - 90 * 86400 - policy.horizon_hours * 3600,
        "validation_end": test_start - policy.horizon_hours * 3600,
        "frozen_test_start": test_start, "frozen_test_end": cutoff,
        "threshold_selection": "fixed engineering hypotheses; no optimization on final test",
        "assumed_entry_delay_seconds": 900, "counts": dict(counts), "results": results,
        "baselines": {"cash_return": 0, "btc_buy_and_hold_net_return": hold,
                      "simple_trend": trend_baseline(bars, test_start, policy),
                      "without_codex": "all numeric-gate candidates above; historical AI performance is not evaluated"},
        "promotion_requires_forward_shadow": True,
    }


def run_research(store: Store, days: int = 730) -> dict[str, Any]:
    if not 730 <= days <= 3650:
        raise ValueError("research_days_out_of_range")
    _, policy = store.policy()
    cutoff = int(time.time() // 900) * 900
    root = store.path.parent / "research"
    root.mkdir(exist_ok=True)
    path = root / f"binance-spot-15m-{days}-{cutoff}.json"
    if path.exists():
        bars = [Bar.model_validate(row) for row in json.loads(path.read_text())]
    else:
        market = PublicMarket()
        try:
            bars = download_spot(market, days, cutoff)
        finally:
            market.close()
        path.write_text(encode([bar.model_dump() for bar in bars]))
    result = research_results(bars, policy, cutoff)
    result["retrieved_at"] = time.time()
    result["source"] = BINANCE + "/klines"
    artifact = root / f"research-{policy.strategy_version}-{cutoff}.json"
    artifact.write_text(encode(result))
    for item in result["results"]:
        store.save_validation(policy.strategy_version, item["strategy_id"], item["direction"], time.time(), {
            **item, "data_sha256": result["data_sha256"], "test_start": result["frozen_test_start"],
            "test_end": cutoff, "artifact_sha256": digest(result),
        })
    store.put("last_research", {"at": time.time(), "strategy_version": policy.strategy_version,
                                "artifact_sha256": digest(result), "counts": result["counts"]})
    return {key: value for key, value in result.items() if key != "counts"} | {"counts": result["counts"]}


def label_forward_events(store: Store, market: PublicMarket, now: float) -> None:
    _, policy = store.policy()
    with store.connection() as db:
        rows = db.execute(
            "SELECT event_id,payload FROM events WHERE outcome IS NULL AND json_extract(payload,'$.strategy_version')=? AND json_extract(payload,'$.bar_close')<? ORDER BY created_at DESC LIMIT 100",
            (policy.strategy_version, now - policy.horizon_hours * 3600),
        ).fetchall()
    for row in rows:
        candidate = Candidate.model_validate_json(row["payload"])
        bars = market.cache.get((candidate.product, "15m"), [])
        value = outcome(candidate, bars, policy)
        if value["status"] != "labelled":
            continue
        with store.connection() as db:
            db.execute("UPDATE events SET outcome=? WHERE event_id=? AND outcome IS NULL", (encode(value), row["event_id"]))
