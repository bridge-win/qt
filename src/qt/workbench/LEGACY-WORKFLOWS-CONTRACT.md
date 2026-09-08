# Qt5 legacy workflows: worker contract

`qt.workbench.legacy_workflows` is the only intended worker-facing entry
point for the original Qt5 gallery workflows. It uses
`qt.strategy_ports.gallery` directly and neither starts a source runner nor
calls `fetch_data()`.

## Catalog and safety

Call `legacy_workflow_catalog()` or `legacy_workflow_definition(workflow_id)`
to retrieve the versioned source identity, required historical datasets,
accepted source parameters, JSON result shape, and manual/replay semantics.

The catalog IDs are:

- Live evaluation/manual proposal: `qt5_smart_dca`, `qt5_capitulation`,
  `qt5_weekly_trend`, `qt5_basis_carry`, `qt5_wick_catcher`.
- Historical replay only: `qt5_sim_smart_dca`, `qt5_sim_weekly_trend`,
  `qt5_sim_basis_carry`, `qt5_sim_wick_catcher`.

Every call uses `execution_mode="research"`. Any other value is rejected.
The function never invokes providers, scanners, retained strategy runners,
risk engines, brokers, or live/paper order APIs. A caller must supply the
versioned snapshots itself.

## Call shape

```python
payload = run_legacy_workflow(
    "qt5_wick_catcher",
    data={"ohlcv": historical_ohlcv},
    decision_at=bar_close,
    available_at=bar_close,
    input_versions=(DataVersion("ohlcv", "sha-or-version", bar_close),),
    parameters={"rung_quote": 100.0},
)
```

`GalleryCausalInput` copies time-indexed inputs and clips them to
`decision_at`; all `DataVersion.available_at` values must be at or before the
decision. Batch replays require every catalog-required dataset and never
synthesize missing OHLCV or funding.

## Results

Live IDs return the original `EvaluationResult` serialized under
`payload["evaluation"]`. A non-null opportunity is also represented as
`payload["proposal"]`, an explicit `manual_operator_proposal`; it is not an
order intent. Basis carry remains a paired spot/perp proposal and WickCatcher
retains its original ladder/take-profit values in proposal details.
`payload["causal_decision_at"]` and `payload["causal_available_at"]` name the
immutable snapshot boundary; they are intentionally distinct from the source
class's `EvaluationResult.ts` clock value.

Simulator IDs return source `StrategyResult` components under
`payload["replay"]`: full equity, long/short target-weight series, trades,
and diagnostics as JSON-safe timestamped records. They remain separate from
live qt5 workflows and are research replays only.

Run validation with:

```sh
./.venv/bin/python -m pytest tests/test_workbench_legacy_workflows.py -q
./.venv/bin/python -m ruff check src/qt/workbench/legacy_workflows.py tests/test_workbench_legacy_workflows.py
```
