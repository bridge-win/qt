"""Frozen native parity evidence; it never upgrades or activates a deployment."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from decimal import Decimal
from pathlib import Path


def fixture(expected_version: str) -> dict:
    import numpy as np
    import pandas as pd
    from btc_backtest.strategies.registry import default_strategy_registry

    from qt.nautilus.executor import _decision_traces, _equity_series, _initializer
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
    source = default_strategy_registry().create("sma_crossover", {"fast_window": 3, "slow_window": 9})
    native = NautilusDataFrameRunner().run(
        frame=frame, strategy=source, assumptions=assumptions,
        instrument=InstrumentDefinition(timestamp_at_close=True), decision_start=index[30],
        initialize=_initializer(source, {"strategy_params": dict(source.parameters)},
                                {"provider": "fixture", "symbol": "BTC/USDT", "timeframe": "1h", "market": "spot"},
                                frame, assumptions),
    )
    # IDs and engine build timestamps are not economic parity comparisons.
    fields = [name for name in ("side", "status", "filled_qty", "avg_px", "commissions", "ts_init")
              if name in native.fills.columns]
    evidence = {
        "nav": _equity_series(native.traces, native=native),
        "decisions": _decision_traces(native.traces),
        "fills": json.loads(native.fills.loc[:, fields].to_json(orient="records", date_format="iso", date_unit="ns")),
        "account_invariants": dict(native.account_invariants),
        "fees": dict(native.metrics["costs"]),
        "terminal_equity": str(native.terminal_equity),
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {"engine_version": installed, "python": sys.version.split()[0],
            "fixture_sha256": hashlib.sha256(frame.to_json().encode()).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "evidence": evidence, "upgrade_accepted": False,
            "reason": "frozen parity evidence requires independent difference review and full acceptance"}


def compare(paths: list[Path]) -> dict:
    baseline, candidate = [json.loads(path.read_text()) for path in paths]
    if baseline.get("engine_version") != "2.0.0rc4" or candidate.get("engine_version") != "2.0.0rc7":
        raise ValueError("comparison_requires_rc4_baseline_and_rc7_candidate")
    if baseline["fixture_sha256"] != candidate["fixture_sha256"]:
        raise ValueError("comparison_fixtures_differ")
    changed = [key for key in baseline["evidence"]
               if baseline["evidence"][key] != candidate["evidence"].get(key)]
    return {"baseline_version": baseline["engine_version"], "candidate_version": candidate["engine_version"],
            "fixture_sha256": baseline["fixture_sha256"], "economic_parity": not changed,
            "changed_fields": changed, "upgrade_accepted": False,
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
