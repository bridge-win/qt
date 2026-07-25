# Independent BTC Backtest Platform Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the independently installable BTC backtest package, real ten-year data, twenty built-in algorithms, custom strategy SDK, hybrid network signals, comprehensive validation, and QT compatibility defined in the approved design.

**Architecture:** Build `packages/btc-backtest` as a package that never imports `qt`, then connect QT through a one-way compatibility adapter. Work is split into four independently reviewable plans: foundation/data/engine, strategy catalog, signals, and validation/reporting/QT integration.

**Tech Stack:** Python 3.10+, pandas 2.1+, NumPy 1.26+, PyArrow 14+, Pydantic 2.5+, HTTPX 0.27+, Typer 0.12+, Rich 13.7+, pytest 8+, Hypothesis 6.100+, Ruff, strict Mypy, setuptools

## Global Constraints

- `btc_backtest` must never import `qt`.
- Real data is the default; synthetic data requires an explicit flag and label.
- Ten-year acceptance covers real Bitstamp `BTC/USD` daily and hourly OHLCV from `2016-07-25T00:00:00Z` through `2026-07-25T00:00:00Z`.
- A real-only run fails on incomplete coverage, invalid data, or provider failure.
- All timestamps are timezone-aware UTC; all requested intervals are closed-open.
- Every run is deterministic for identical data, configuration, package version, and seed.
- Signal availability is gated by `observed_at`; future-known data cannot affect earlier decisions.
- The package supports spot long/flat and a declared short-perpetual leg for funding-basis carry only.
- The twenty-strategy requirement is exact and excludes `buy_and_hold`, `capitulation`, and `wick_catcher`.
- No secrets or generated market data are committed.
- Existing QT CLI, dashboard, benchmark, and artifact readers remain compatible.
- Every implementation task follows red-green-refactor and ends with a focused commit and push.

---

## Plan Dependency Map

Execute these plans in order:

1. [`2026-07-25-btc-backtest-foundation-data-engine.md`](2026-07-25-btc-backtest-foundation-data-engine.md)
   - produces the installable package, typed models, validated data, cache,
     provider registry, real Bitstamp/Binance adapters, engine, accounting,
     custom SDK, public runner, and foundation CLI;
   - exit gate: a custom buy-and-hold strategy runs on cached real data and
     produces deterministic accounting events.
2. [`2026-07-25-btc-backtest-strategies.md`](2026-07-25-btc-backtest-strategies.md)
   - consumes the strategy/engine contracts;
   - produces the exact twenty built-ins, benchmarks, QT-specific strategies,
     catalog discovery, contract tests, and golden tests;
   - exit gate: all twenty are discoverable and pass the shared suite.
3. [`2026-07-25-btc-backtest-signals.md`](2026-07-25-btc-backtest-signals.md)
   - consumes `DataRequest`, `StrategyContext`, and artifact contracts;
   - produces normalized signals, historical stores, providers, ranking,
     reliability calibration, signed webhook ingestion, and strategy access;
   - exit gate: five source types rank point-in-time observations with complete
     attribution and future-signal isolation.
4. [`2026-07-25-btc-backtest-validation-integration.md`](2026-07-25-btc-backtest-validation-integration.md)
   - consumes all earlier contracts;
   - produces metrics, walk-forward/purged validation, Monte Carlo, sensitivity,
     stress testing, immutable artifacts, HTML reports, complete CLI, QT
     compatibility, live ten-year acceptance, build/install verification, and
     documentation;
   - exit gate: every design acceptance criterion has fresh evidence.

## Cross-Plan Interfaces

The following names are frozen across all four plans:

```python
from btc_backtest.api import BacktestRunner
from btc_backtest.data.models import DataRequest, MarketBundle, MarketDataset
from btc_backtest.engine.models import (
    BacktestResult,
    BacktestSpec,
    Fill,
    Order,
    OrderIntent,
    PortfolioSnapshot,
)
from btc_backtest.strategies.base import (
    FinalizationContext,
    InitializationContext,
    Strategy,
    StrategyContext,
    StrategyMetadata,
)
from btc_backtest.signals.models import RankedSignal, SignalObservation
from btc_backtest.validation.models import ValidationResult, ValidationSpec
```

Breaking one of these imports requires updating all later plans and the design
before implementation continues.

## Execution Discipline

For each plan:

1. run its baseline command and record the result;
2. execute tasks in order with the listed red and green commands;
3. inspect the actual staged diff before each commit;
4. run the plan exit gate;
5. push the current branch;
6. update this master plan only if an interface or acceptance requirement
   changes.

Do not batch unrelated subsystem changes into one commit. Generated Parquet,
JSON, HTML, cache, coverage, and build files stay ignored.

## Acceptance Coverage

| Design criterion | Direct implementation and proof |
| --- | --- |
| Independent clean install | Foundation Tasks 1/12; Integration Task 8 |
| Public API and CLI | Foundation Task 12; Integration Task 6 |
| Live ten-year BTC/USD run | Foundation Task 5; Integration Task 9 |
| No silent synthetic fallback | Foundation Tasks 2/4/12; Integration Task 6 |
| Exact twenty algorithms | Strategy Tasks 2–13 and exit gate |
| Three additional strategies | Strategy Task 12 |
| External custom entry point | Foundation Tasks 10/12; Integration Task 8 |
| Five signal-source types | Signal Tasks 3–9 and exit gate |
| Future-bar/signal isolation | Foundation Task 11; Signal Tasks 2/9 |
| Complete robust validation | Integration Tasks 2–5 |
| Accounting and daily/hourly metrics | Foundation Tasks 8/9; Integration Task 1 |
| QT regressions through adapter | Integration Task 7 |
| Static/build/live release gates | Integration Tasks 8–10 |
| Committed and pushed evidence | Every task Step 5; final remote check |

## Final Completion Audit

After all four plan exit gates pass:

- map each of the fourteen design acceptance criteria to a command and artifact;
- run every command fresh from a clean worktree and a clean virtual
  environment;
- run live network acceptance instead of relying on cached proof alone;
- compare `HEAD` to the remote branch;
- leave the active goal open if any criterion lacks direct evidence.
