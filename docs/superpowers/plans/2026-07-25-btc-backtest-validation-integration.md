# BTC Backtest Validation, Reporting, And QT Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete performance metrics, anti-overfitting validation, reproducible artifacts/reports, standalone CLI, QT compatibility, production packaging, real ten-year acceptance, and the final requirement audit.

**Architecture:** Validation runs immutable child `BacktestSpec` values through the public runner; reporting consumes results without mutating them; artifacts are atomically written under unique run IDs. QT adapts its existing APIs and commands to `btc_backtest`, while deployment installs both distributions.

**Tech Stack:** Python 3.10+, pandas, NumPy, SciPy, PyArrow, Pydantic, Jinja2, Typer, Rich, pytest, Hypothesis, Ruff, strict Mypy, Docker

## Global Constraints

- Annualization derives from timeframe and actual elapsed UTC time.
- Training, validation, and test windows are immutable and non-overlapping.
- Parameter selection cannot inspect the final test window.
- Purged splits include an explicit embargo.
- Randomized validation uses a recorded seed.
- Artifacts are immutable and include complete data/signal/package provenance.
- Existing QT import paths and command aliases remain valid.
- The package remains independently installable and never imports QT.
- Live acceptance uses fresh official network data, not cached-only evidence.
- No generated data, artifacts, builds, reports, caches, or secrets are committed.
- Every task ends with focused tests, `git diff --check`, a commit, and a push.

---

## File Map

- `reporting/metrics.py`: complete performance and risk metrics.
- `validation/models.py`: immutable validation configuration/results.
- `validation/splits.py`: expanding/rolling/purged time-series windows.
- `validation/walk_forward.py`: parameter selection and untouched tests.
- `validation/monte_carlo.py`: block bootstrap.
- `validation/sensitivity.py`: parameter surfaces and multiple-testing diagnostics.
- `validation/stress.py`: cost, delay, missing-bar, and provider-outage stress.
- `reporting/artifacts.py`: atomic Parquet/JSON run bundles.
- `reporting/html.py`: standalone attributed HTML report.
- `cli.py`: complete data, strategy, validation, and signal commands.
- `src/qt/backtest/strategy_backtest.py`: compatibility façade.
- `src/qt/cli.py`: preserved QT aliases backed by the package.
- `pyproject.toml`, `start.sh`, `Dockerfile.platform`, CI workflows: dual-distribution install.
- `tests/integration/test_ten_year_backtest.py`: live duration/performance acceptance.
- `docs/btc-backtest.md`: operator and custom-strategy guide.

### Task 1: Complete Timeframe-Aware Metrics

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/reporting/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/reporting/metrics.py`
- Create: `packages/btc-backtest/tests/reporting/test_metrics.py`
- Create: `packages/btc-backtest/tests/reporting/test_metrics_properties.py`

**Interfaces:**
- Produces: `PerformanceMetrics` frozen model.
- Produces: `BenchmarkComparison` frozen model.
- Produces: `compute_metrics(result: BacktestResult) -> PerformanceMetrics`.
- Produces: `compare_benchmarks(primary, buy_hold, fixed_dca) -> BenchmarkComparison`.
- Produces: `periods_per_year(index: DatetimeIndex) -> float`.

- [ ] **Step 1: Write failing hand-calculated/timeframe tests**

```python
def test_annualization_uses_actual_daily_spacing() -> None:
    equity = pd.Series(
        [100.0, 101.0, 102.01],
        index=pd.date_range("2024-01-01", periods=3, freq="1D", tz="UTC"),
    )
    metrics = compute_metrics(result_with_equity(equity))
    assert metrics.total_return == pytest.approx(0.0201)
    assert metrics.periods_per_year == pytest.approx(365.25)


def test_drawdown_duration_and_cost_attribution() -> None:
    result = result_fixture(
        equity=[100, 120, 90, 95, 121],
        fees=[1, 2],
        slippage=[0.5, 0.25],
        funding=[3],
    )
    metrics = compute_metrics(result)
    assert metrics.max_drawdown == pytest.approx(-0.25)
    assert metrics.max_drawdown_bars == 2
    assert metrics.total_fees == Decimal("3")
    assert metrics.total_slippage == Decimal("0.75")
    assert metrics.total_funding == Decimal("3")


def test_benchmark_comparison_reports_excess_return_and_cost() -> None:
    comparison = compare_benchmarks(
        metrics(total_return=0.20, total_fees=20),
        metrics(total_return=0.25, total_fees=5),
        metrics(total_return=0.15, total_fees=10),
    )
    assert comparison.excess_vs_buy_hold == pytest.approx(-0.05)
    assert comparison.excess_vs_fixed_dca == pytest.approx(0.05)
    assert comparison.incremental_fees_vs_buy_hold == Decimal("15")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/reporting/test_metrics.py packages/btc-backtest/tests/reporting/test_metrics_properties.py -q
```

Expected: import failure for reporting metrics.

- [ ] **Step 3: Implement complete metrics**

Compute total return, CAGR, annualized volatility, Sharpe, Sortino, Calmar,
Omega, maximum/average drawdown and duration, exposure, turnover, fees,
slippage, funding, trade count, win rate, profit factor, expectancy, holding
period, historical VaR/CVaR at configured confidence, and monthly/yearly
tables. Return finite zero values plus warnings where denominators or samples
are insufficient; never return NaN/Infinity in JSON fields. Benchmark
comparison reports return, drawdown, risk-adjusted, turnover, and cost
differences against `buy_and_hold` and `fixed_dca`.

- [ ] **Step 4: Run reporting/property tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/reporting/test_metrics.py packages/btc-backtest/tests/reporting/test_metrics_properties.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: daily/hourly annualization, empty-trade, drawdown, cost, and finite-value properties pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): compute complete performance metrics"
git push origin codex/quantdinger-platform-upgrade
```

### Task 2: Immutable Validation Models And Purged Splits

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/validation/__init__.py`
- Create: `packages/btc-backtest/src/btc_backtest/validation/models.py`
- Create: `packages/btc-backtest/src/btc_backtest/validation/splits.py`
- Create: `packages/btc-backtest/tests/validation/test_splits.py`

**Interfaces:**
- Produces: `Window`, `SplitMode`, `ValidationSpec`, `ValidationResult`.
- Produces: `expanding_splits`, `rolling_splits`, and `purged_splits`.

- [ ] **Step 1: Write failing split-isolation tests**

```python
def test_purged_split_has_no_overlap_and_respects_embargo() -> None:
    index = pd.date_range("2020-01-01", periods=100, freq="1D", tz="UTC")
    split = purged_splits(index, train_bars=60, test_bars=20, purge_bars=5, embargo_bars=3)[0]
    assert split.train.end <= split.purge.start
    assert split.purge.end <= split.test.start
    assert split.next_eligible_start >= split.test.end + pd.Timedelta(days=3)


def test_validation_spec_rejects_test_window_used_for_selection() -> None:
    with pytest.raises(ValidationError, match="final test"):
        ValidationSpec(
            selection_end=utc("2024-12-31"),
            final_test_start=utc("2024-12-01"),
            final_test_end=utc("2025-01-31"),
        )
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/validation/test_splits.py -q
```

Expected: import failure for validation models/splits.

- [ ] **Step 3: Implement closed-open split generators**

Models validate UTC ordering and positive sizes. Generators return immutable
index ranges, never positional views that can mutate the original. Expanding
keeps a fixed start; rolling keeps fixed training length; purged inserts purge
bars before every test and embargo bars before the next train eligibility.

- [ ] **Step 4: Run split/property tests**

```bash
.venv/bin/pytest packages/btc-backtest/tests/validation/test_splits.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: ordering, no-overlap, purge, embargo, insufficient-length, and UTC tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add purged time-series validation splits"
git push origin codex/quantdinger-platform-upgrade
```

### Task 3: Walk-Forward Parameter Selection

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/validation/walk_forward.py`
- Create: `packages/btc-backtest/tests/validation/test_walk_forward.py`
- Create: `packages/btc-backtest/tests/validation/test_final_test_isolation.py`

**Interfaces:**
- Produces: `ParameterCandidate`, `WindowEvaluation`, `WalkForwardResult`.
- Produces: `WalkForwardValidator(runner).run(spec, candidates)`.

- [ ] **Step 1: Write failing selection/final-test tests**

```python
def test_each_window_selects_on_train_and_scores_on_next_test(fake_runner) -> None:
    result = WalkForwardValidator(fake_runner).run(validation_spec(), candidates())
    assert all(item.selected_on == item.split.train for item in result.windows)
    assert all(item.scored_on == item.split.test for item in result.windows)


def test_mutating_final_test_cannot_change_selected_parameters(dataset, runner) -> None:
    first = validate(dataset, runner)
    mutated = mutate_final_test_only(dataset)
    second = validate(mutated, runner)
    assert first.selected_parameters == second.selected_parameters
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/validation/test_walk_forward.py packages/btc-backtest/tests/validation/test_final_test_isolation.py -q
```

Expected: import failure for walk-forward validator.

- [ ] **Step 3: Implement deterministic train/select/test flow**

Sort candidates by canonical parameter JSON. Run each on train data, choose
the objective with stable tie-breaker of lower drawdown then lower turnover
then canonical params, evaluate exactly once on its adjacent test, and reserve
the final test for one terminal evaluation after all selection completes.
Record every child data fingerprint and run ID.

- [ ] **Step 4: Run validation/no-leak suites**

```bash
.venv/bin/pytest packages/btc-backtest/tests/validation/test_walk_forward.py packages/btc-backtest/tests/validation/test_final_test_isolation.py packages/btc-backtest/tests/engine/test_no_lookahead.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: deterministic selection, untouched final test, tie-break, and insufficient-window tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): validate strategies walk forward"
git push origin codex/quantdinger-platform-upgrade
```

### Task 4: Monte Carlo, Sensitivity, Stress, And Multiple Testing

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/validation/monte_carlo.py`
- Create: `packages/btc-backtest/src/btc_backtest/validation/sensitivity.py`
- Create: `packages/btc-backtest/src/btc_backtest/validation/stress.py`
- Create: `packages/btc-backtest/tests/validation/test_monte_carlo.py`
- Create: `packages/btc-backtest/tests/validation/test_sensitivity.py`
- Create: `packages/btc-backtest/tests/validation/test_stress.py`

**Interfaces:**
- Produces: `BlockBootstrap.run(returns, simulations, block_size, seed)`.
- Produces: `SensitivityAnalyzer.run(base_spec, grid)`.
- Produces: `StressRunner.run(base_spec, scenarios)`.
- Produces: `StressEvaluation(base_metrics, scenario_metrics, scenarios)`.
- Produces: `MultipleTestingDiagnostic`.

- [ ] **Step 1: Write failing deterministic/stress tests**

```python
def test_block_bootstrap_is_seeded_and_preserves_length() -> None:
    first = BlockBootstrap.run(returns(), simulations=100, block_size=5, seed=7)
    second = BlockBootstrap.run(returns(), simulations=100, block_size=5, seed=7)
    assert first == second
    assert all(len(path) == len(returns()) for path in first.paths)


def test_cost_stress_cannot_improve_equity() -> None:
    evaluation = StressRunner(runner()).run(
        spec(), [CostStress(fee_multiplier=2, slippage_multiplier=3)]
    )
    assert evaluation.scenario_metrics[0].final_equity <= evaluation.base_metrics.final_equity


def test_multiple_testing_reports_adjusted_significance() -> None:
    result = multiple_testing([0.01, 0.02, 0.20], method="holm")
    assert result.adjusted_p_values[0] >= result.raw_p_values[0]
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/validation/test_monte_carlo.py packages/btc-backtest/tests/validation/test_sensitivity.py packages/btc-backtest/tests/validation/test_stress.py -q
```

Expected: import failures for validation analyzers.

- [ ] **Step 3: Implement bounded validation engines**

Use stationary or fixed block bootstrap without reshuffling individual bars.
Sensitivity validates parameter bounds and canonical grid order. Stress
scenarios include fee/slippage multipliers, one-bar execution delay,
deterministic missing-bar masks, and declared provider outage intervals.
Multiple-testing diagnostics implement Holm-adjusted p-values and report the
number of attempted variants.

- [ ] **Step 4: Run complete validation suite**

```bash
.venv/bin/pytest packages/btc-backtest/tests/validation -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: seed determinism, finite distributions, grid bounds, stress monotonicity, outage behavior, and adjusted statistics pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): add robust strategy validation"
git push origin codex/quantdinger-platform-upgrade
```

### Task 5: Immutable Artifacts And HTML Report

**Files:**
- Create: `packages/btc-backtest/src/btc_backtest/reporting/artifacts.py`
- Create: `packages/btc-backtest/src/btc_backtest/reporting/html.py`
- Create: `packages/btc-backtest/src/btc_backtest/reporting/templates/report.html.j2`
- Create: `packages/btc-backtest/tests/reporting/test_artifacts.py`
- Create: `packages/btc-backtest/tests/reporting/test_html.py`
- Modify: `packages/btc-backtest/pyproject.toml`

**Interfaces:**
- Produces: `RunManifest`, `ArtifactBundle`.
- Produces: `ArtifactWriter.write(result, metrics, validation, root) -> ArtifactBundle`.
- Produces: `render_html(bundle) -> str`.

- [ ] **Step 1: Write failing artifact completeness/immutability tests**

```python
EXPECTED_FILES = {
    "run.json", "data_manifest.json", "equity.parquet", "positions.parquet",
    "orders.parquet", "fills.parquet", "trades.parquet", "signals.parquet",
    "metrics.json", "validation.json", "report.html",
}


def test_writer_creates_complete_attributed_bundle(tmp_path, complete_result) -> None:
    bundle = ArtifactWriter().write(complete_result, metrics(), validation(), tmp_path)
    assert {path.name for path in bundle.run_dir.iterdir()} == EXPECTED_FILES
    run = json.loads((bundle.run_dir / "run.json").read_text())
    assert run["data_fingerprint"] == complete_result.data_manifest.normalized_sha256
    assert run["signal_fingerprint"]


def test_writer_never_overwrites_existing_run(tmp_path, complete_result) -> None:
    first = ArtifactWriter().write(complete_result, metrics(), validation(), tmp_path)
    second = ArtifactWriter().write(complete_result, metrics(), validation(), tmp_path)
    assert first.run_dir != second.run_dir
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/reporting/test_artifacts.py packages/btc-backtest/tests/reporting/test_html.py -q
```

Expected: import failures for artifact/report modules.

- [ ] **Step 3: Implement atomic bundle and standalone report**

Derive run ID from UTC timestamp plus deterministic spec/data digest; add a
collision suffix; write all files in a temporary sibling directory; validate
schemas/fingerprints; atomically rename. `run.json` contains versions, Git
revision, seed, costs, fill policy, strategy parameters, data/signal hashes,
and file hashes. HTML embeds no remote script and shows provenance,
in/validation/out-of-sample sections, benchmarks, costs, drawdowns, warnings,
and every contributing signal.

- [ ] **Step 4: Run reporting and wheel-data tests**

Add Jinja2 to runtime dependencies and template package data, then run:

```bash
.venv/bin/pytest packages/btc-backtest/tests/reporting -q
.venv/bin/python -m build packages/btc-backtest
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: bundle, schema, atomic failure, no-overwrite, HTML escaping, and wheel template tests pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): write reproducible backtest reports"
git push origin codex/quantdinger-platform-upgrade
```

### Task 6: Complete Standalone CLI

**Files:**
- Modify: `packages/btc-backtest/src/btc_backtest/cli.py`
- Modify: `packages/btc-backtest/tests/test_cli.py`
- Create: `packages/btc-backtest/tests/test_cli_end_to_end.py`

**Interfaces:**
- Produces all CLI commands from the approved design.

- [ ] **Step 1: Write failing CLI matrix tests**

```python
@pytest.mark.parametrize(
    "args",
    [
        ["data", "sync", "--help"],
        ["data", "inspect", "--help"],
        ["strategies", "list"],
        ["strategies", "describe", "sma_crossover"],
        ["run", "sma_crossover", "--help"],
        ["run-custom", "--help"],
        ["validate", "sma_crossover", "--help"],
        ["signals", "collect", "--help"],
        ["signals", "top", "--help"],
    ],
)
def test_documented_commands_exist(args) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0


def test_real_run_defaults_to_no_synthetic_fallback(cli, empty_provider) -> None:
    result = cli.invoke(app, ["run", "sma_crossover", "--provider", "empty"])
    assert result.exit_code == 2
    assert "real data coverage" in result.stdout
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/test_cli.py packages/btc-backtest/tests/test_cli_end_to_end.py -q
```

Expected: missing validate/run options and real-data semantics fail.

- [ ] **Step 3: Implement commands as thin public-API adapters**

Add required provider/symbol/timeframe/start/end/years/cache/output/JSON
options. Resolve `--years` to a calendar interval ending at explicit `--end`
or current UTC. `run` computes metrics and writes artifacts. `validate` writes
validation artifacts. Synthetic fixtures require `--synthetic` and print a
prominent label. Typed failures exit 2; unexpected failures retain traceback
only with `--debug`.

- [ ] **Step 4: Run CLI/package regression**

```bash
.venv/bin/pytest packages/btc-backtest/tests/test_cli.py packages/btc-backtest/tests/test_cli_end_to_end.py -q
.venv/bin/ruff check packages/btc-backtest
.venv/bin/mypy --strict packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: all documented commands and end-to-end fixture runs pass.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest
git commit -m "feat(backtest): complete standalone CLI"
git push origin codex/quantdinger-platform-upgrade
```

### Task 7: QT Compatibility Façade

**Files:**
- Modify: `src/qt/backtest/strategy_backtest.py`
- Modify: `src/qt/backtest/__init__.py`
- Modify: `src/qt/cli.py`
- Create: `tests/test_btc_backtest_compatibility.py`
- Modify: `tests/test_strategy_backtest.py`
- Modify: `tests/test_reporting_benchmark.py`
- Modify: `tests/test_dashboard_strategies.py`

**Interfaces:**
- Preserves: `canonical_strategy`, `run_strategy_backtest`,
  `synthetic_btc_ohlcv`, `synthetic_funding`, `BacktestOutcome`,
  `write_strategy_backtest_artifacts`.
- Maps legacy IDs: `dca -> smart_dca`, `trend -> sma_crossover`,
  `carry -> funding_basis_carry`, `wick -> wick_catcher`.

- [ ] **Step 1: Write failing legacy-parity tests**

```python
@pytest.mark.parametrize(
    ("legacy", "new"),
    [("dca", "smart_dca"), ("trend", "sma_crossover"),
     ("carry", "funding_basis_carry"), ("wick", "wick_catcher")],
)
def test_legacy_alias_executes_new_engine(legacy, new, fixture_ohlcv) -> None:
    outcome = run_strategy_backtest(legacy, fixture_ohlcv, allow_synthetic=False)
    assert outcome.strategy == legacy
    assert outcome.engine_strategy == new
    assert outcome.synthetic is False


def test_legacy_artifact_summary_keeps_required_fields(tmp_path, fixture_ohlcv) -> None:
    path = write_strategy_backtest_artifacts(
        run_strategy_backtest("trend", fixture_ohlcv, allow_synthetic=False), tmp_path
    )
    summary = json.loads((path / "summary.json").read_text())
    assert {"strategy", "synthetic", "bars", "metrics", "data_fingerprint"} <= summary.keys()
```

- [ ] **Step 2: Run legacy suites and verify RED**

```bash
.venv/bin/pytest tests/test_btc_backtest_compatibility.py tests/test_strategy_backtest.py tests/test_reporting_benchmark.py tests/test_dashboard_strategies.py -q
```

Expected: compatibility fields/new engine mapping fail.

- [ ] **Step 3: Replace internals with one-way adapter**

Keep public dataclasses/functions or compatible aliases, translate pandas
inputs into `LocalParquetProvider`/in-memory dataset requests, call
`btc_backtest.BacktestRunner`, translate complete metrics and artifacts back
to legacy fields, and retain explicit synthetic helpers for demos. No new
package code imports QT. CLI help expands to the new catalog while old aliases
remain accepted.

- [ ] **Step 4: Run focused QT/package compatibility**

```bash
.venv/bin/pytest tests/test_btc_backtest_compatibility.py tests/test_strategy_backtest.py tests/test_reporting_benchmark.py tests/test_dashboard_strategies.py packages/btc-backtest/tests/test_package_boundary.py -q
.venv/bin/ruff check src/qt/backtest src/qt/cli.py tests/test_btc_backtest_compatibility.py
.venv/bin/mypy src tests packages/btc-backtest/src packages/btc-backtest/tests
```

Expected: legacy API, CLI, benchmark, dashboard, and package boundary pass.

- [ ] **Step 5: Commit and push**

```bash
git add src/qt/backtest src/qt/cli.py tests packages/btc-backtest
git commit -m "feat(backtest): connect QT to independent engine"
git push origin codex/quantdinger-platform-upgrade
```

### Task 8: Dual-Distribution Development, CI, And Production Install

**Files:**
- Modify: `pyproject.toml`
- Modify: `start.sh`
- Modify: `Dockerfile.platform`
- Modify: `.github/workflows/ci.yml`
- Modify: `deploy/platform-entrypoint.sh`
- Create: `tests/integration/test_btc_backtest_packaging.py`
- Modify: `tests/test_deploy_scripts.py`

**Interfaces:**
- QT distribution requires `btc-backtest==0.1.0`.
- Development, CI, and platform image install the local `btc-backtest` wheel
  before or alongside QT.

- [ ] **Step 1: Write failing clean-install/container contract tests**

```python
def test_clean_wheels_install_together(tmp_path, built_wheels) -> None:
    venv = create_venv(tmp_path)
    pip_install(venv, *built_wheels)
    output = run(venv, ["python", "-c", "import qt, btc_backtest; print(btc_backtest.__version__)"])
    assert output.strip() == "0.1.0"


def test_external_entry_point_installs_and_runs_in_clean_environment(
    tmp_path, built_wheels, fixture_plugin_wheel
) -> None:
    venv = create_venv(tmp_path)
    pip_install(venv, *built_wheels, fixture_plugin_wheel)
    output = run(venv, ["btc-backtest", "strategies", "describe", "external_fixture", "--json"])
    assert '"id": "external_fixture"' in output


def test_platform_dockerfile_installs_both_distributions() -> None:
    source = Path("Dockerfile.platform").read_text()
    assert "btc_backtest-0.1.0" in source
    assert "qt-0.1.0" in source
```

- [ ] **Step 2: Run packaging tests and verify RED**

```bash
.venv/bin/pytest tests/integration/test_btc_backtest_packaging.py tests/test_deploy_scripts.py -q
```

Expected: missing package build/install flow fails.

- [ ] **Step 3: Implement reproducible dual-wheel install**

Add `btc-backtest==0.1.0` to QT metadata. `start.sh` installs the nested
package editable before QT. CI builds the nested wheel and root wheel, installs
both from the local wheel directory, then runs both suites. Docker wheelhouse
builds both distributions with pinned build inputs; runtime installs
`btc-backtest==0.1.0` and `qt==0.1.0` using `--no-index --find-links`.
Build and install the external fixture plugin wheel in the clean-environment
test to prove real entry-point discovery. Entrypoint verifies both installed
versions without changing role ownership.

- [ ] **Step 4: Run packaging/deployment gates**

```bash
.venv/bin/python -m build packages/btc-backtest
.venv/bin/python -m build .
.venv/bin/pytest tests/integration/test_btc_backtest_packaging.py tests/test_deploy_scripts.py -q
docker build --target runtime -f Dockerfile.platform .
docker run --rm --entrypoint python qt-platform:test -c 'import qt, btc_backtest; print(btc_backtest.__version__)'
```

Expected: clean wheel install and container import both packages at version `0.1.0`.

- [ ] **Step 5: Commit and push**

```bash
git add pyproject.toml start.sh Dockerfile.platform .github deploy tests
git commit -m "build: package independent backtester with QT"
git push origin codex/quantdinger-platform-upgrade
```

### Task 9: Genuine Ten-Year Live And Performance Acceptance

**Files:**
- Create: `packages/btc-backtest/tests/integration/test_ten_year_backtest.py`
- Create: `packages/btc-backtest/tests/performance/test_ten_year_performance.py`
- Modify: `packages/btc-backtest/pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Proves real Bitstamp `BTC/USD` daily/hourly coverage.
- Proves cached execution performance budgets.

- [ ] **Step 1: Write failing calendar-coverage tests**

```python
START = datetime(2016, 7, 25, tzinfo=timezone.utc)
END = datetime(2026, 7, 25, tzinfo=timezone.utc)


@pytest.mark.integration
@pytest.mark.parametrize(("timeframe", "slots"), [("1d", 3652), ("1h", 87648)])
def test_real_bitstamp_ten_year_coverage(timeframe, slots, tmp_path) -> None:
    request = DataRequest(
        provider="bitstamp", symbol="BTC/USD", timeframe=timeframe,
        start=START, end=END, require_complete=True,
    )
    dataset = registry().fetch(request, DataCache(tmp_path))
    missing = sum(gap.missing_bars for gap in dataset.manifest.gaps)
    assert len(dataset.frame) + missing == slots
    assert missing / slots <= 0.001
    assert dataset.manifest.delivered_start == START
    assert dataset.manifest.delivered_end == END
    assert dataset.manifest.real_data is True


@pytest.mark.performance
def test_cached_daily_and_hourly_runtime(real_cached_datasets) -> None:
    assert timed_run("sma_crossover", real_cached_datasets.daily) < 5
    assert timed_run("sma_crossover", real_cached_datasets.hourly) < 60
```

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest packages/btc-backtest/tests/integration/test_ten_year_backtest.py -q -m integration
.venv/bin/pytest packages/btc-backtest/tests/performance/test_ten_year_performance.py -q -m performance
```

Expected: coverage or performance requirements fail until provider pagination,
gap semantics, and runner performance are complete.

- [ ] **Step 3: Fix only measured coverage/performance causes**

Profile before changing code. Permit page batching and vectorized immutable
feature preparation, but keep event ordering and point-in-time access
unchanged. Treat genuine exchange maintenance gaps according to a reviewed
provider-specific calendar, include every missing slot in the manifest, and
do not fabricate candles. Delivered bars plus declared gaps must equal the
full ten-year calendar, with missing ratio at or below 0.1%.

- [ ] **Step 4: Run live, performance, and twenty-strategy matrix**

```bash
.venv/bin/pytest packages/btc-backtest/tests/integration/test_ten_year_backtest.py -q -m integration
.venv/bin/pytest packages/btc-backtest/tests/performance/test_ten_year_performance.py -q -m performance
.venv/bin/pytest packages/btc-backtest/tests/strategies/test_golden_matrix.py -q
```

Expected: fresh live ten-year daily/hourly data validates, performance budgets pass, and all twenty run from the cached daily dataset.

- [ ] **Step 5: Commit and push**

```bash
git add packages/btc-backtest .github/workflows/ci.yml
git commit -m "test(backtest): prove ten-year real-data runs"
git push origin codex/quantdinger-platform-upgrade
```

### Task 10: Documentation And Requirement-Level Completion Audit

**Files:**
- Create: `docs/btc-backtest.md`
- Modify: `README.md`
- Modify: `docs/ROADMAP.md`
- Create: `docs/verification/btc-backtest-acceptance.md`
- Create: `tests/test_btc_backtest_docs.py`
- Modify: `docs/superpowers/plans/2026-07-25-btc-backtest-master.md`

**Interfaces:**
- Produces operator/custom-strategy documentation.
- Produces direct evidence mapping for all fourteen acceptance criteria.

- [ ] **Step 1: Write failing documentation contract test**

```python
def test_documentation_covers_public_workflows_and_all_algorithms() -> None:
    text = Path("docs/btc-backtest.md").read_text()
    for command in (
        "btc-backtest data sync", "btc-backtest run", "btc-backtest run-custom",
        "btc-backtest validate", "btc-backtest signals top",
    ):
        assert command in text
    for strategy_id in BUILTIN_STRATEGY_IDS:
        assert f"`{strategy_id}`" in text
    assert "not a profitability guarantee" in text.lower()
```

- [ ] **Step 2: Run documentation test and verify RED**

```bash
.venv/bin/pytest tests/test_btc_backtest_docs.py -q
```

Expected: missing guide/test fails.

- [ ] **Step 3: Write guide and acceptance evidence**

Document install, real-data sync, ten-year daily/hourly run, all 20 algorithms,
custom module and entry-point examples, signals and credentials, validation,
artifacts, QT aliases, provider limitations, point-in-time semantics, and
risk/profitability caveats. For each design acceptance criterion, record the
exact command, date, exit code, count, and artifact path in
`docs/verification/btc-backtest-acceptance.md`. Mark plan checkboxes only from
fresh command evidence.

- [ ] **Step 4: Run the complete final audit**

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
```

Expected: all offline/live/performance/root/package/static/build/container
checks pass with no untracked generated artifacts.

- [ ] **Step 5: Commit, push, and verify remote**

```bash
git add README.md docs packages/btc-backtest/tests tests/test_btc_backtest_docs.py
git commit -m "docs: document independent BTC backtesting"
git push origin codex/quantdinger-platform-upgrade
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/codex/quantdinger-platform-upgrade)"
```

## Final Exit Gate

The goal is complete only after Task 10's fresh audit proves all fourteen
acceptance criteria and the evidence document contains no missing, skipped, or
indirect requirement. If a live provider, container build, PostgreSQL suite,
performance budget, compatibility suite, or artifact contract is unproven,
keep the goal active and continue implementation.
