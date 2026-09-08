# QT research lab API contract

Mount `build_lab_router(LabSettings(state_root=settings.state_root,
parquet_root=settings.parquet_root), research_repository=research_repository,
executor=executor)` at `/api/v3`, or pass a pre-built service. Its exact factory signature is:

```python
build_lab_router(
    settings: LabSettings,
    *,
    executor: LabExecutionAdapter | None = None,
    repository: LabRepository | None = None,
    research_repository: ResearchRepository | None = None,
    service: LabService | None = None,
) -> APIRouter
```

All lab submissions use the shared `ResearchRepository` and its `research_jobs` table. Lab owns
only `lab_` strategy/version/note/trial/result metadata; it does not create, claim, fence, or
cancel a second queue. `enqueue_optimization` and `enqueue_validation` call
`ResearchRepository.enqueue_idempotent` with an immutable spec of the form
`{"job_type":"lab_optimization"|"lab_validation", "payload": {...}}`.

The lead worker is the sole queue consumer. After it claims a shared job it must dispatch either
kind by copying the persisted spec and adding `lab_job_id=job["job_id"]` in memory, then call
`LabService.execute(dispatch_spec, progress, cancelled)`. The worker owns `complete`, `fail`,
`cancel_running`, heartbeat, and attempt fencing. Its progress callback must call
`ResearchRepository.update_progress(job_id, stage, percent, attempt_id=attempt_id)` and
`heartbeat_attempt`; its cancellation callback must call
`is_cancel_requested(job_id, attempt_id=attempt_id)`. `lab_job_id` is needed only to key durable
Optuna trial rows and is deliberately not written back into the shared immutable spec. The API
process never executes plugin source or computes a return.

The injected adapter receives top-level experiment fields (including `dataset_id`) plus
`lab_execution_kind`, immutable `lab_strategy_version`, and `parameter_overrides`. The current
`NautilusResearchExecutor.execute` must be extended by the engine owner to recognize these
fields, construct causal rules/ensembles/regime switches, and dispatch plugin versions through
the `IsolatedPluginRuntime.execute_plugin(strategy_version, experiment, parameter_overrides,
progress, cancelled)` worker-side interface. That runtime must provide the planned no-network,
non-root, read-only-data, resource-limited environment; this lab slice deliberately contains no
API-server plugin execution or package installation.

For result detail and comparison, the native executor must publish this canonical payload; lab
does not invent missing conditions or derive returns from closed trades:

```json
{
  "run_id": "immutable-native-run-id",
  "configuration": {"dataset_fingerprint": "...", "cashflow": "...", "risk_budget": "...", "execution_model": "nautilus", "benchmark": "...", "cost_model": {"fee_bps": 10}},
  "data": {"fingerprint": "...", "symbol": "BTC/USDT", "timeframe": "1h", "start": "...", "end": "..."},
  "metrics": {"net_return": 0.0, "sharpe": 0.0, "calmar": 0.0, "max_drawdown": 0.0},
  "metric_reasons": {"optional_undefined_metric": "why it is undefined"},
  "costs": {"fees": 0.0},
  "account_invariants": {"status": "verified", "violations": []},
  "series": {"returns": {"timestamp": 0.0}, "equity": {"timestamp": 10000.0}, "drawdown": {"timestamp": 0.0}},
  "monthly_returns": {"YYYY-MM": 0.0},
  "decision_traces": [{"timestamp": "timezone-aware ISO-8601"}]
}
```

Returns and equity must be marked-to-market portfolio observations, not closed-trade returns.
`calmar` must use CAGR over the observed elapsed period, not total return. Without every
comparison condition, lab returns an explicit incomparable result.

Native result JSON may not contain `NaN` or infinity. An undefined metric is represented as
`null` in `metrics` with a corresponding human-readable `metric_reasons` entry; it is never
silently converted to zero. Native execution must also publish `account_invariants` after
checking its account/ledger trace. Strict validation accepts only `status: "verified"` with an
empty `violations` list; balance or sizing errors must fail the run rather than being filtered
from its evidence.

For a `standard` validation job, the lab constructs real chronological folds from the shared
Parquet timestamps: `train -> purge -> validation -> embargo -> locked test`. Every candidate
is executed only on train and validation before selection; only the selected candidate executes
the test window. The native adapter must honor the supplied `from`, `to`, `lab_phase`,
`lab_fold`, `mode="lab_strategy_version"`, `lab_strategy_version.content`, and top-level
`parameter_overrides` fields. `lab_parameter_bindings` and the nested version copy are emitted
for audit compatibility, but top-level `parameter_overrides` is the executable binding contract.
A native adapter that ignores any of those
fields is rejected as incompatible with strict validation. Quick validation is explicitly
`diagnostic_only`. DSR is `not_applicable` unless comparable trial Sharpe-estimate variance is
provided; raw-return volatility is never used as that variance. PBO is explicitly
`not_applicable` for this strict selector: it runs only the validation-selected candidate on a
locked OOS window. `cscv_diagnostic=true` is separate from selection: it evaluates every
candidate only on already-locked OOS folds and invokes the existing legacy CSCV-style
approximation only with at least 8 candidates and 4 complete folds. It never substitutes
validation scores.

Same-window optimization provides the usable DSR workflow when native trial payloads include a
complete common comparability group and marked-to-market returns. Lab derives each trial Sharpe
on the explicit `per_bar_nonannualized` scale, estimates its `sample_variance_ddof_1` across
completed trial Sharpe estimates, and records attempted/completed trials plus the selected trial
in `multiple_testing_scope`. Adaptive walk-forward selection diagnostics and its stitched
selected OOS path stay separate: without a justified distribution of comparable alternative OOS
Sharpe estimates, its DSR remains `not_applicable`.

For `ensemble` and `regime_switch`, lab expands each referenced immutable version recursively
under `lab_strategy_version.content.ensemble[*].strategy_version` or
`...regimes[*].strategy_version`. Every nested node retains its exact `version_id` and `content`.
Cycles and graphs with more than three leaf strategies are rejected. The native strategy builder
must consume that execution graph; lab never replaces missing composite implementations with a
look-alike target-weight proxy.

The lead's existing `GET /jobs/{job_id}` and `POST /jobs/{job_id}/cancel` use only
`ResearchRepository`; there is no lab fallback. Its experiment overview identifies lab entries
by `spec.job_type`. `LabService.list_results(limit=50)` is the bounded aggregate input for the
lead-owned `GET /results` merge, including native `research_runs` not yet projected into lab.
This router deliberately avoids duplicate job paths, because FastAPI route order would otherwise
silently choose one handler.

For lead-owned direct experiment submission, call
`LabService.execution_version(version_id) -> JsonDict`. It returns the same immutable,
cycle-checked, three-leaf-bounded composite graph that lab job specs pass in
`lab_strategy_version`; callers must not import `_hydrate_execution_version` or submit an
unhydrated ensemble/regime version.

## Route ownership

This router owns `POST /strategies`, `GET /strategies`, `POST /strategies/validate`,
`GET|POST /strategies/{strategy_id}/versions`, and `GET /strategy-versions/{version_id}`.
It intentionally does **not** register `GET /strategies/{id}` because the lead's profile
catalog owns that exact route. The web client obtains lab draft detail from the version list
and a selected immutable `/strategy-versions/{version_id}`.

It also owns `/strategy-docs/{id}`, `/indicator-docs/{id}`, `/results/{id}`,
`/results/{id}/traces`, `/compare`, CRUD `/notes`, `/optimizations/{id}`, and
`/validation/{id}`. The web contract's plural `GET /traces` and `GET /results` remain
lead-owned aggregate routes; this router provides the corresponding immutable detail data.
`GET /traces?experiment_id=...&cursor=...&limit=...&from=...&to=...` is the Web alias:
`from` and `to` are an optional paired, timezone-aware ISO-8601 window; `limit` is 1--200 and
`cursor` is non-negative. Trace filters return `trace_timestamps_unavailable` rather than
silently filtering un-timestamped legacy traces.

## Supported strategy JSON

The following is the public request shape for both `POST /strategies` and a version save.
`POST /strategies` additionally requires `name`; `base_strategy_id` is a read-only builtin
to clone. `POST /strategies/{id}/versions` additionally requires `expected_version` and
optionally `message`.

```json
{
  "kind": "rules",
  "parameters": {
    "period": {"value": 14, "minimum": 2, "maximum": 100, "description": "RSI window"}
  },
  "rules": {
    "entry": {
      "kind": "and",
      "children": [
        {"kind": "signal", "signal": {"indicator": "rsi", "timeframe": "1h", "parameters": {"period": 14}}, "comparator": "<", "right": 30},
        {"kind": "crosses_above", "left": {"indicator": "sma", "timeframe": "1h", "parameters": {"window": 20}}, "right": {"indicator": "sma", "timeframe": "1h", "parameters": {"window": 60}}}
      ]
    },
    "exit": {"kind": "sustained_for", "bars": 2, "child": {"kind": "signal", "signal": {"indicator": "rsi", "timeframe": "1h"}, "comparator": ">", "right": 55}}
  }
}
```

`signal` normalizes to a comparison node. `crosses_above` and `crosses_below` normalize to
cross nodes. `sustained_for` requires an integer `bars` from 1 through 1000. Every signal
must provide `timeframe` (`current` is explicit when the form omits it), and execution must
only use completed bars; static validation returns a deterministic Chinese explainer but does
not claim a cross is true without historical prior-bar values.

`kind` accepts `rules`, `ensemble`, `regime_switch`, and `plugin`; `python` is accepted as a
web alias and is persisted as `plugin`. `ensemble` requires 2--3
immutable `strategy_version_id` members with weights summing exactly to 1. `regime_switch`
requires 1--3 named branches each with a typed `when` rule and immutable version. `plugin`
requires `code` and `plugin_api_version`; source is versioned but may execute only through the
injected isolated worker runtime, with no API-server `exec` or package installation.

## Notes and jobs

`POST /notes` accepts `{ "entity_type": "strategy_version"|"experiment"|"result",
"entity_id": "...", "strategy_version_id": "...", "body": "..." }`. For a
`strategy_version` note, `entity_id` supplies `strategy_version_id`; for other entity types,
`strategy_version_id` is required so every note remains reproducibly tied to an immutable
strategy version.

Optimization requests use `strategy_version_id`, `experiment`, `search_space`, `sampler`
(`grid`, `random`, or `tpe`), `objective`, `budget`, and `seed`. They return `202` and are
executed through the shared worker dispatch above. Optuna uses a persistent
`lab_optuna.sqlite3` study keyed to the shared job ID; Grid/Random/TPE all receive the request
seed. Every native-executor trial result, failure, and observed cancellation is stored in
`lab_optimization_trials`. Validation jobs use the same worker boundary and record native
executor results, never synthetic metrics.

Only a missing or non-finite objective is a recoverable Optuna trial failure; it is persisted as
`FAIL` and the bounded study continues. Executor/configuration failures and cancellation are
fatal to the shared job. A study with no finite completed trial fails clearly. On a Random/TPE
retry, lab derives a deterministic sampler seed from request seed, immutable job ID, and the
persisted trial cursor so the stream is reproducible without replaying its initial RNG sequence.

`LabService.import_native_result(result, decision_traces)` is the explicit result-publish hook
for Nautilus completions. `GET /results/{id}` and trace reads also fall back to the shared
`research_runs` table, so an existing native result is not hidden merely because it predates a
lab projection. The engine should include actual `decision_traces` in its native result payload.

`POST /compare` accepts `experiment_ids` and bounded `max_points`. It only emits equity,
drawdown, returns, monthly returns, costs, and correlation for results whose immutable
comparison group agrees on data fingerprint, instrument, timeframe, date period, cashflow,
risk budget, execution model, benchmark, and cost model. Missing conditions are reported in
`incomparable`; they are never silently represented as empty zero-valued metrics.

Plugin results remain viewable as exploratory evidence, but their runtime-provided
`temporal_integrity` is preserved. A plugin result with `status: "unverified"` or
`verified_leaderboard_eligible: false` is excluded from verified comparison, correlation, and
optimizer DSR evidence even when data/cost keys match. Docker isolation is not temporal proof.
Only a result that explicitly reports verified plugin integrity and supplies an independent
`causal_input_audit` (`status`, `independent`, `auditor`, and `evidence_id`) can participate.
Rules and built-ins are not defaulted to verified; their causal status must be evidenced
separately by their own execution trace/audit.

`GET /strategies` is sourced from `UnifiedCatalogProvider`, whose default QT adapter emits every
actual strategy registry entry and every preserved catalog profile, not a lab-maintained sample
registry. Built-ins are read-only. Cloning stores `mode: "builtin"` with the exact source
identity and source version; it does not rewrite a built-in as a look-alike rules proxy.
`GET /indicator-docs/{id}` is likewise delegated to the unified catalog source and returns a
404 for an undocumented indicator rather than inventing a formula. Documentation includes source
aware availability: `price`, `talib_standard`, `volatility`, `regime`, and `events` are derived
only after a completed market bar; `onchain`, `options`, `derivatives`, `sentiment`, and
`smartmoney` require provider-recorded `available_at` and may be revised. Lab never treats the
latter as available at candle close.

`GET /learning-docs` returns a bounded `{items, next_cursor}` list of all preserved catalog
signal documents plus indicator-family documents. A catalog signal item includes exact evaluator
identity/version, bilingual formula and threshold/cross semantics, declared/effective/ignored
parameters, warm-up and NaN behavior, caveats, provenance, and availability semantics. It is
learning material grounded in the preserved evaluator; it never treats a source filename, AST,
or a signal label as the explanation.
