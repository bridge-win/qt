"""Frozen native parity evidence; it never upgrades or activates a deployment."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _economic_evidence(native: object) -> dict:
    from qt.nautilus.executor import _decision_traces, _equity_series

    fields = [name for name in ("side", "status", "order_type", "time_in_force", "quantity",
                                "filled_qty", "avg_px", "price", "commissions", "ts_init", "ts_last",
                                "is_reduce_only", "is_post_only") if name in native.fills.columns]
    return {
        "nav": _equity_series(native.traces, native=native),
        "decisions": _decision_traces(native.traces),
        "fills": json.loads(native.fills.loc[:, fields].to_json(orient="records", date_format="iso", date_unit="ns")),
        "account_invariants": dict(native.account_invariants),
        "fees": dict(native.metrics["costs"]),
        "terminal_equity": str(native.terminal_equity),
    }


def fixture(expected_version: str) -> dict:
    import numpy as np
    import pandas as pd
    from btc_backtest.engine.models import InstrumentKind, OrderIntent, OrderSide, OrderType
    from btc_backtest.strategies.base import StrategyMetadata
    from btc_backtest.strategies.registry import default_strategy_registry

    from qt.experiments.native import builtin_strategy_code_hash
    from qt.nautilus.executor import _decision_traces, _initializer
    from qt.nautilus.models import ExecutionAssumptions
    from qt.nautilus.runner import InstrumentDefinition, NautilusDataFrameRunner

    installed = importlib.metadata.version("nautilus_trader")
    if installed != expected_version:
        raise ValueError("native comparison engine version mismatch")
    index = pd.date_range("2020-01-01", periods=240, freq="1h", tz="UTC")
    close = np.round(100 + np.sin(np.arange(240) / 4) * 10, 2)
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                          "close": close, "volume": np.full(240, 100.0)}, index=index)
    assumptions = ExecutionAssumptions(initial_cash=Decimal("1000"), maker_fee=Decimal("0.001"),
                                       taker_fee=Decimal("0.001"), seed=7)

    def sma(data):
        source = default_strategy_registry().create("sma_crossover", {"fast_window": 3, "slow_window": 9})
        return NautilusDataFrameRunner().run(
            frame=data, strategy=source, assumptions=assumptions,
            instrument=InstrumentDefinition(timestamp_at_close=True), decision_start=index[30],
            initialize=_initializer(source, {"strategy_params": dict(source.parameters)},
                                    {"provider": "fixture", "symbol": "BTC/USDT", "timeframe": "1h", "market": "spot"},
                                    data, assumptions),
        )

    native = sma(frame)
    evidence = _economic_evidence(native)
    prefix = sma(frame.iloc[:180].copy())
    changed_future = frame.copy()
    changed_future.loc[index[180]:, ["open", "high", "low", "close"]] += 1_000_000
    poisoned = sma(changed_future)
    decisions = evidence["decisions"]
    prefix_decisions = _decision_traces(prefix.traces)
    poisoned_decisions = _decision_traces(poisoned.traces)
    if decisions[:150] != prefix_decisions or decisions[:150] != poisoned_decisions[:150]:
        raise ValueError("native_prefix_or_future_mutation_changed_historical_decisions")

    class RoundTrip:
        metadata = StrategyMetadata(id="fee_round_trip", version="1", description="Fee contract fixture",
                                    warmup_bars=0, supported_timeframes=("1h",))

        def __init__(self, maker_entry):
            self.parameters, self.maker_entry = {}, maker_entry
            self.sent = self.exited = False

        def on_bar(self, context):
            if not self.sent:
                self.sent = True
                return (OrderIntent(instrument=InstrumentKind.SPOT, side=OrderSide.BUY,
                                    order_type=OrderType.LIMIT if self.maker_entry else OrderType.MARKET,
                                    base_quantity=Decimal("1"), limit_price=Decimal("95") if self.maker_entry else None,
                                    reason="fixed_entry"),)
            if not self.exited and context.portfolio.position(InstrumentKind.SPOT).quantity == 1:
                self.exited = True
                return (OrderIntent(instrument=InstrumentKind.SPOT, side=OrderSide.SELL,
                                    order_type=OrderType.MARKET, base_quantity=Decimal("1"), reason="fixed_exit"),)
            return ()

    fee_frame = pd.DataFrame({"open": [100.0] * 5, "high": [101.0] * 5,
                              "low": [99.0, 94.0, 99.0, 99.0, 99.0], "close": [100.0] * 5,
                              "volume": [100.0] * 5}, index=index[:5])
    fee_scenarios = {}
    for maker_entry in (False, True):
        for charged in (False, True):
            rates = (Decimal("0.0025"), Decimal("0.01")) if charged else (Decimal("0"), Decimal("0"))
            run = NautilusDataFrameRunner().run(
                frame=fee_frame, strategy=RoundTrip(maker_entry),
                assumptions=ExecutionAssumptions(initial_cash=Decimal("1000"), maker_fee=rates[0], taker_fee=rates[1]),
                instrument=InstrumentDefinition(timestamp_at_close=True),
            )
            expected_fee = (Decimal("1.2375") if maker_entry else Decimal("2")) if charged else Decimal("0")
            expected_equity = Decimal("1005") if maker_entry else Decimal("1000")
            if (len(run.fills) != 2 or Decimal(run.metrics["costs"]["fees"]) != expected_fee
                    or run.terminal_equity != expected_equity - expected_fee
                    or run.account_invariants["status"] != "verified"
                    or Decimal(run.account_invariants["actual_base_balance"]) != 0):
                raise ValueError("native_maker_taker_fee_or_cash_semantics_changed")
            key = ("maker_entry" if maker_entry else "market_entry") + ("_charged" if charged else "_zero")
            fee_scenarios[key] = _economic_evidence(run)
    slipped = NautilusDataFrameRunner().run(
        frame=fee_frame, strategy=RoundTrip(False), instrument=InstrumentDefinition(timestamp_at_close=True),
        assumptions=ExecutionAssumptions(initial_cash=Decimal("1000"), one_tick_slippage=True,
                                         slippage_probability=1.0),
    )
    if ([Decimal(str(value)) for value in slipped.fills["avg_px"]] != [Decimal("100.01"), Decimal("99.99")]
            or Decimal(slipped.metrics["costs"]["fees"]) != Decimal("0.2")
            or slipped.terminal_equity != Decimal("999.78")):
        raise ValueError("native_one_tick_slippage_was_omitted_or_compounded")
    fee_scenarios["one_tick_market_charged"] = _economic_evidence(slipped)
    evidence["fee_scenarios"] = fee_scenarios
    evidence["causality"] = {"prefix_decisions_unchanged": True, "future_mutation_unchanged": True}
    # Runtime IDs and build timestamps are excluded. Financial data and decision
    # times stay exact; no tolerance can hide commission or account differences.
    sources = [Path(__file__), Path(sys.modules[NautilusDataFrameRunner.__module__].__file__)]
    sources.extend(sources[-1].parent / name for name in ("adapter.py", "models.py", "executor.py"))
    adapter_hash = canonical_hash({
        "adapter": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "registered_strategy": builtin_strategy_code_hash("sma_crossover"),
    })
    return {"schema_version": "qt.native-parity.v2", "engine_version": installed, "python": sys.version.split()[0],
            "fixture_sha256": canonical_hash({"sma": frame.to_json(), "fees": fee_frame.to_json(),
                                               "fee_cases": list(fee_scenarios), "seed": 7}),
            "adapter_sha256": adapter_hash, "fee_policy": dict(native.metrics["fee_policy"]),
            "evidence_sha256": canonical_hash(evidence),
            "evidence": evidence, "upgrade_accepted": False,
            "reason": "frozen parity evidence requires independent difference review and full acceptance"}


def compare(paths: list[Path]) -> dict:
    baseline, candidate = [json.loads(path.read_text()) for path in paths]
    if baseline.get("engine_version") != "2.0.0rc4" or candidate.get("engine_version") != "2.0.0rc7":
        raise ValueError("comparison_requires_rc4_baseline_and_rc7_candidate")
    required = {"nav", "decisions", "fills", "account_invariants", "fees", "terminal_equity", "fee_scenarios", "causality"}
    for report in (baseline, candidate):
        if (report.get("schema_version") != "qt.native-parity.v2"
                or not isinstance(report.get("evidence"), dict)
                or not required.issubset(report["evidence"])
                or any(not re.fullmatch(r"[a-f0-9]{64}", str(report.get(key, "")))
                       for key in ("fixture_sha256", "adapter_sha256", "evidence_sha256"))
                or canonical_hash(report["evidence"]) != report["evidence_sha256"]):
            raise ValueError("native_comparison_report_incomplete_or_hash_mismatch")
    if baseline["fixture_sha256"] != candidate["fixture_sha256"]:
        raise ValueError("comparison_fixtures_differ")
    if baseline["adapter_sha256"] != candidate["adapter_sha256"]:
        raise ValueError("comparison_adapter_sources_differ")
    changed = [key for key in sorted(set(baseline["evidence"]) | set(candidate["evidence"]))
               if key not in baseline["evidence"] or key not in candidate["evidence"]
               or baseline["evidence"][key] != candidate["evidence"][key]]
    return {"baseline_version": baseline["engine_version"], "candidate_version": candidate["engine_version"],
            "fixture_sha256": baseline["fixture_sha256"], "economic_parity": not changed,
            "changed_fields": changed, "upgrade_accepted": False,
            "adapter_sha256": baseline["adapter_sha256"],
            "baseline_evidence_sha256": baseline["evidence_sha256"],
            "candidate_evidence_sha256": candidate["evidence_sha256"],
            "reason": "full regression, causal, fill, recovery and compatibility acceptance still required"}


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--expected-version", choices=["2.0.0rc4", "2.0.0rc7"])
    group.add_argument("--compare", nargs=2, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.compare:
        report = compare(args.compare)
    else:
        # Native Rust initialization writes its banner directly to fd 1, even
        # with BacktestEngine logging disabled. Keep machine JSON on stdout.
        sys.stdout.flush()
        original_stdout = os.dup(sys.stdout.fileno())
        try:
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            report = fixture(args.expected_version)
            sys.stdout.flush()
        finally:
            os.dup2(original_stdout, sys.stdout.fileno())
            os.close(original_stdout)
    if str(args.output) == "-":
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "evidence"}))


if __name__ == "__main__":
    main()
