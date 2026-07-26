# BTC backtest acceptance evidence

Scope: branch `codex/quantdinger-platform-upgrade`, repository
`/Users/kwt/x/qt`, evidence recorded on 2026-07-26 Asia/Shanghai.

Latest pushed evidence commit for the ten-year acceptance tranche:
`abf01af test(backtest): prove ten-year real-data runs`.

This document maps the fourteen design acceptance criteria to current
implementation evidence. It is an audit log, not an investment claim.

| Criterion | Current direct evidence |
| --- | --- |
| Independent clean install | `packages/btc-backtest/pyproject.toml` defines the standalone `btc-backtest` package and script; `tests/integration/test_btc_backtest_packaging.py` verifies clean dual-wheel install; prior packaging gate built `packages/btc-backtest` and root wheels, installed both, and imported `qt, btc_backtest` in the runtime image. |
| Public API and CLI | `packages/btc-backtest/src/btc_backtest/api.py` exports `BacktestRunner`; `packages/btc-backtest/src/btc_backtest/cli.py` exposes `data`, `strategies`, `run`, `run-custom`, `validate`, and `signals`; `packages/btc-backtest/tests/test_cli.py` verifies documented commands. |
| Live ten-year BTC/USD run | Command: `.venv/bin/pytest packages/btc-backtest/tests/integration/test_ten_year_backtest.py -q -m integration`; exit code `0`; result `2 passed in 74.26s (0:01:14)`; verifies Bitstamp `BTC/USD` daily/hourly for `2016-07-25T00:00:00Z` through `2026-07-25T00:00:00Z`, counts `3652` and `87648`, real-data manifests, and delivered interval equality. |
| No silent synthetic fallback | `DataRequest.require_real`, provider metadata, CLI `--synthetic`, and `packages/btc-backtest/tests/test_cli.py::test_synthetic_provider_requires_explicit_synthetic_flag` reject synthetic provider use without an explicit flag. |
| Exact twenty algorithms | `packages/btc-backtest/src/btc_backtest/strategies/registry.py` defines `BUILTIN_STRATEGY_IDS` with exactly 20 IDs; command `.venv/bin/pytest packages/btc-backtest/tests/strategies/test_golden_matrix.py -q`; exit code `0`; result `47 passed in 23.37s`. |
| Three additional strategies | `EXTRA_STRATEGY_IDS` defines `buy_and_hold`, `capitulation`, and `wick_catcher`; they are included in the golden catalog test but excluded from the 20-count built-in requirement. |
| External custom entry point | `packages/btc-backtest/src/btc_backtest/strategies/loader.py` loads `file.py:ClassName` and package entry points; `packages/btc-backtest/tests/test_cli.py::test_run_custom_cli_exports_json` verifies `btc-backtest run-custom`; packaging tests install the fixture plugin exposing `external_fixture`. |
| Five signal-source types | Signal providers cover sentiment (`alternative.py`), on-chain (`coinmetrics.py`), Binance derivatives funding, open interest, long/short ratio, taker flow (`binance.py`), local archives, generic HTTP archives, and signed webhooks; `packages/btc-backtest/tests/signals` validates models, providers, ranking, store, webhook, and strategy integration. |
| Future-bar/signal isolation | `packages/btc-backtest/tests/engine/test_no_lookahead.py` proves future bar mutation cannot change past events; `packages/btc-backtest/tests/signals/test_signal_no_lookahead.py` proves signal availability is gated by `observed_at`; both are covered by package tests. |
| Complete robust validation | `packages/btc-backtest/src/btc_backtest/validation` implements splits, walk-forward, Monte Carlo, sensitivity, stress, and diagnostics; `packages/btc-backtest/tests/validation` contains the regression suite for those contracts. |
| Accounting and daily/hourly metrics | `packages/btc-backtest/src/btc_backtest/engine/accounting.py`, `engine/fills.py`, and `reporting/metrics.py` provide deterministic accounting and timeframe-aware metrics; reporting/property tests cover annualization, drawdown, fees, funding, finite values, and benchmark comparison. |
| QT regressions through adapter | `src/qt/backtest/strategy_backtest.py` preserves QT legacy APIs and maps aliases to package strategies; focused compatibility tests include `tests/test_btc_backtest_compatibility.py`, `tests/test_strategy_backtest.py`, `tests/test_reporting_benchmark.py`, and `tests/test_dashboard_strategies.py`. |
| Static/build/live release gates | Recent Task 9 gates: live acceptance `2 passed in 74.26s`, performance `2 passed in 44.73s`, golden matrix `47 passed`; lint, mypy, `git diff --check`, and workflow YAML parse all passed before commit `abf01af`. |
| Committed and pushed evidence | `git log -1 --oneline --decorate` after push showed `abf01af (HEAD -> codex/quantdinger-platform-upgrade, origin/codex/quantdinger-platform-upgrade) test(backtest): prove ten-year real-data runs`; branch status was clean and tracking `origin/codex/quantdinger-platform-upgrade`. |

## Commands recorded for the latest tranche

| Command | Exit | Evidence |
| --- | ---: | --- |
| `.venv/bin/pytest packages/btc-backtest/tests/integration/test_ten_year_backtest.py -q -m integration` | 0 | `2 passed in 74.26s (0:01:14)` |
| `.venv/bin/pytest packages/btc-backtest/tests/performance/test_ten_year_performance.py -q -m performance` | 0 | `2 passed in 44.73s` |
| `.venv/bin/pytest packages/btc-backtest/tests/strategies/test_golden_matrix.py -q` | 0 | `47 passed in 23.37s` |
| `.venv/bin/pytest packages/btc-backtest/tests/engine/test_runner.py packages/btc-backtest/tests/engine/test_no_lookahead.py packages/btc-backtest/tests/strategies/test_contract.py packages/btc-backtest/tests/strategies/test_moving_average.py -q` | 0 | `24 passed in 1.89s` |
| `.venv/bin/ruff check` on changed Python files | 0 | `All checks passed!` |
| `.venv/bin/mypy` on changed Python files | 0 | `Success: no issues found in 6 source files` |
| `.venv/bin/python -c 'from pathlib import Path; import yaml; yaml.safe_load(Path(".github/workflows/ci.yml").read_text()); print("workflow yaml parsed")'` | 0 | `workflow yaml parsed` |
| `git diff --check` | 0 | no output |
| `git push` | 0 | `fb94ecd..abf01af  codex/quantdinger-platform-upgrade -> codex/quantdinger-platform-upgrade` |

## Final release audit command set

Before marking the whole objective complete, rerun the full final gate from a
clean worktree:

```bash
.venv/bin/pytest packages/btc-backtest/tests -q
QT_TEST_POSTGRES_URL=postgresql+psycopg://qt:qt@127.0.0.1:55432/qt_test .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src tests deploy/create_platform_env.py packages/btc-backtest/src packages/btc-backtest/tests
.venv/bin/python -m build packages/btc-backtest
.venv/bin/python -m build .
.venv/bin/pytest packages/btc-backtest/tests/integration/test_ten_year_backtest.py -q -m integration
.venv/bin/pytest packages/btc-backtest/tests/performance/test_ten_year_performance.py -q -m performance
docker build --target runtime -f Dockerfile.platform .
git diff --check
git status --short --branch
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/codex/quantdinger-platform-upgrade)"
```
