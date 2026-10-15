# QT workbench `/api/v3` web contract

The API is same-origin (`/api/v3`). All responses are JSON. The browser sends
cookies for Cloudflare Access and never sends origin, R2, provider, or trading
credentials. A `401`/`403` means Access/authorization; a `503` means the
protected research origin is offline; a `404` means a partial deployment is
behind the requested UI and must be displayed, not replaced with local data.

## Shared conventions

- List endpoints accept `cursor`, `limit` (maximum 200), `from`, `to`, and
  optional domain filters. Series and traces require a bounded `from`/`to` and
  a server-side `downsample`/`max_points` option. Responses use
  `{ "items": [], "next_cursor": null }`.
- Mutating requests accept `Idempotency-Key`. Long work returns `202` with
  `{ "job": { "job_id", "status", "stage", "progress" } }`, where
  `progress` is an integer percentage from `0` through `100`, and a
  `Location: /api/v3/jobs/{job_id}` header.
- Errors are `{ "detail": { "code", "message", "request_id", "fields" } }`.
  `409 version_conflict` includes `{ "current_version", "current" }`.
- Formal return, drawdown, risk, execution and validation metrics are supplied
  by the service only. The web app may render them, but must never calculate
  them from chart points.

## Catalog and data

| Endpoint | Web request | Required response / use |
| --- | --- | --- |
| `GET /capabilities` | `category`, `cursor`, `limit` | Evidence-first rows with `id`, category, `source_repository`, `source_commit`, `source_path`, `source_sha256`, `target_path`, `entry_point`, `migration_status`, `data_validation_status`, notes and test evidence. |
| `GET /catalog`, `/indicators`, `/signals` | search, family, version | Definitions, formulas, required data, warm-up, `available_at` semantics, provenance and examples. |
| `GET /learning-docs` | `cursor`, `limit=1..200` | Versioned documentation `{items,next_cursor}` for catalog signals and indicator families. Each item may include `formula_zh`, `condition_zh`, `effective_parameters`, `ignored_legacy_parameters`, `cross_semantics`, `warmup`, `available_at`, `nan_behavior`, and `caveats`; render supplied fields rather than claiming they are unavailable. |
| `GET /datasets`, `/sources` | filters | Dataset coverage, partitions, quality/gap report, fingerprint, status; provider credential/subscription status but never secret values. |
| `POST /staging/uploads` | Raw ≤1 MiB CSV/Parquet body, CSV `Content-Type: text/csv`, Parquet `application/vnd.apache.parquet` (or octet-stream plus `X-Upload-Format: parquet`) | Returns server-generated opaque `{object_key,format,size_bytes}`. Browser input never chooses a server path. |
| `POST /imports` | `{source:"staging",dataset_id,format,object_key}` plus idempotency key | `202` common job. `object_key` must be returned by staging; CSV/Parquet require a timestamp column and backend validates time/OHLCV quality before atomic publish. |
| `POST /sync-jobs` | `{source,dataset_id,symbol,timeframe,from,to}` plus idempotency key | `from`/`to` are timezone-aware ISO-8601 timestamps, max 366 days. `symbol` is an explicit market identity (for example `BTC/USDT`); the service validates it and encodes its storage key. Returns a `202` common job using an allow-listed provider adapter. |

## Strategies and rule composition

| Endpoint | Web request | Required response / use |
| --- | --- | --- |
| `GET /strategies` | cursor/filter | Read-only built-ins and user drafts, latest immutable version, provenance and rule summary. |
| `POST /strategies` | Custom: `{name,kind:"rules"|"plugin"|"ensemble"|"regime_switch",...content}`. Built-in clone: **only** `{name,base_strategy_id}` | Draft plus initial immutable version. A built-in clone omits editor content so the service preserves its exact `builtin_identity`, `builtin_version`, and parameters. |
| `GET /strategies/{id}`, `GET /strategy-versions/{id}` | - | Parameters, condition AST, code, validation state, immutable version metadata. |
| `GET /strategies/{id}/versions`, `GET /strategy-versions/{version_id}` | select a draft then an immutable version | Version list/content, including revision, message and validation state. The profile route `GET /strategies/{id}` belongs to the catalog and is not used for draft editing. |
| `POST /strategies/{id}/versions` | `{expected_version,message,content}` | Returns the `StrategyVersion` object directly (including `version_id` and `revision`), not `{version: ...}`. `content` is rules/ensemble/regime/plugin payload. |
| `POST /strategies/validate` | `{content:{kind,rules?,parameters?,code?,ensemble?,regimes?}}` | Field errors plus static rule/code validation; no execution or package installation. |

Rules use typed AST nodes: `and`, `or`, `not`, `crosses_above`,
`crosses_below`, `sustained_for`, and `signal`. Each signal includes its
timeframe and parameter object. The service rejects future/unclosed bars.

## Experiments, results, comparison, traces and archive

| Endpoint | Web request | Required response / use |
| --- | --- | --- |
| `POST /experiments` | `{strategy_version_id,dataset_id,from?,to?,precision,costs,validation,seed,market}` | `202` durable common-queue job. `market` is `spot` or `perpetual`; `costs.leverage` is bounded to 1–3, while spot is 1x. Bar-only OHLCV requires zero spread/slippage/funding/borrow; corresponding execution assumptions need trade/order-book data. No client-side estimated-row field is accepted. |
| `GET /experiments`, `/jobs/{job_id}` | cursor / job id | Status `queued/running/cancelling/cancelled/succeeded/failed/interrupted`, stage, integer `progress` `0..100`, request/job/run IDs and logs cursor. |
| `POST /jobs/{job_id}/cancel`, `POST /experiments/{id}/reproduce` | cancellation / `{}` | Cancel request / 202 job using fixed source fingerprints and strategy version. |
| `GET /results/{result_id}`, `/results` | bounded list/detail | Native saved result: `metrics`, `costs`, `monthly_returns`, and `series.{returns,equity,drawdown}` keyed by timestamp. The browser renders these values only. |
| `POST /compare` | `{result_ids,baseline_id?,max_points}` | Same-condition comparison. Response has `metrics`, `series` keyed by `result_id`, `monthly_returns`, `costs`, `correlation`, `comparable_result_ids`, and `incomparable`; each series contains timestamp-keyed equity/returns/drawdown. |
| `GET /results/{result_id}/traces` | `cursor?,window=1..200,from?,to?` | Paged native DecisionTrace wrapper `{sequence,known_at?,trace}`. `trace` uses `timestamp`, `reasons`, `no_trade_cause`, `observed_values`, and `intent_count`; UI must not relabel absent fields as condition truth/orders. The `/traces` alias accepts `experiment_id,cursor,limit,from,to`. |
| `GET /artifacts`, `/artifacts/{id}` | cursor / artifact id | Reproducibility manifests and protected download URL or proxied content metadata. |
| `POST /notes` | `{entity_type,entity_id,body,expected_version?}` | Versioned research note. |

## Optimization, validation, events, portfolio and runtime

| Endpoint | Web request | Required response / use |
| --- | --- | --- |
| `POST /optimizations`, `GET /optimizations/{id}` | `{strategy_version_id,experiment,search_space,sampler,objective,budget,seed}` | `search_space` is a non-empty bounded object of parameter-name to candidate arrays; sampler is `grid`, `random`, or `tpe`; objective is `sharpe`, `calmar`, or `net_return`. Returns `202`, then every trial, candidate, sensitivity and evidence. |
| `POST /validation`, `GET /validation/{id}` | `{strategy_version_id,experiment,profile,seed}` | `profile` is `quick` or `standard`; the immutable version and full experiment specification are required. Returns walk-forward/OOS/DSR/PBO evidence, with `not_applicable` where statistics are invalid. |
| `GET /events/wicks` | `dataset_id,from,to` with timezone-aware range, max 366 days | Event evidence from persisted OHLCV only. |
| `GET /opportunities`, `/portfolio` | bounded filters | Opportunity reasons and server-side account/risk ledger. No frontend PnL calculation. |
| `GET /runtime`, `POST /runtime/commands`, `GET /audit` | `POST` body is only `{command:"paper.pause"}` or `{command:"paper.resume"}` | research/paper state, health/heartbeat and audited constrained commands. No research pause, reconcile, live enable, order, flatten or kill command exists. |

## Edge handoff

The Worker must worker-first `/api/*` and `/artifacts/*`, authenticate every
host including workers.dev/preview, verify the Access JWT signature, issuer,
audience and expiry, proxy only to the configured protected origin with service
credentials, and read R2 only below allow-listed report prefixes. It must not
turn API/artifact 404s into SPA HTML.

## Read-only BTC spot monitor

`GET /api/v3/btc-monitor` is the sole monitor endpoint. It inherits the existing
workbench origin/Access authentication and returns `Cache-Control: no-store`.
It reads only `state_root/btc-monitor/latest.json`; requests never create the
monitor directory/database or start a collector. A missing worker is harmless to
other workbench features. See [部署与运维](../docs/btc-monitor.md).

The response is `{snapshot,read_status,read_reasons,server_time,
snapshot_age_seconds,snapshot_max_age_seconds,future_tolerance_seconds,
valid_until,data_valid_until,live_execution_enabled:false}`. All timestamps are
UTC epoch seconds. `read_status` is `ok`, `degraded`, or `unavailable`; missing,
malformed, oversized (>1 MiB), incompatible, non-finite or non-regular/symlink
files return `snapshot:null` and a safe diagnostic code, without raw file or
exception text. This diagnostic state uses HTTP 200; authorization/transport
errors retain ordinary HTTP error semantics.

Schema-version-1 snapshots contain:

- `config_version`, `boot_id`, `published_at` and explicit source identity:
  Binance `spot` `BTCUSDT` / `BTC/USDT`, `price_type:trade`. No futures/mark-price
  meaning is implied.
- `health`: separate `alive`, `data_ready`, `assessment_ready`, status/reasons,
  coverage, last core/persistence progress, errors, and `channels` keyed by
  trade/quote/candle. Each channel provides source and receive timestamps/ages,
  consumer backlog, readiness, and `source_time_known`. Spot book ticker has no
  exchange timestamp, so only that quote channel may use receive-only freshness.
- `micro`: latest price, spread in bps and bounded 5/15/60-second window metrics.
- `assessment`: stable assessment ID/revision, as-of/expiry, state
  `INSUFFICIENT_DATA|DATA_UNHEALTHY|UNSUITABLE|CANDIDATE`, reasons, gates with
  observed value/threshold, cost/range evidence, research range/levels,
  gross/net spacing, missing budget/inventory/actual fee and validation inputs,
  monitor/research allowed actions and execution blocked actions.
- `recent_events`: bounded wick revisions with stable event ID, state
  `WATCH|CONFIRMED|INVALIDATED|EXPIRED`, direction, baseline/extreme, recovery
  fraction, as-of/expiry and source identity.
- `context`: closed-candle/grid-history diagnostics, conservative freshness
  limits, `sensitivity_preset:"balanced"`, `tick_size_assumed:true`, numeric
  `tick_size`, `thresholds_calibrated:false`, and `notifications_configured`
  boolean only. The preset and assumed tick are research metadata, without
  validated venue constraints or calibrated profitability. No credential values.

Every GET recomputes UTC ages, worker progress, per-channel source/receive
freshness, nonnegative bounded backlog and assessment expiry. The read side
caps snapshot age at 10 seconds, future skew at 2 seconds, source ages at
5/5/125 seconds and backlog at 2 seconds (stricter configured limits apply).
A heartbeat cannot renew old assessment evidence. Invalid positives become
`DATA_UNHEALTHY`; affected active wick events are shown as expired diagnostic
history. Disk history is never rewritten by the reader. Assessment-only expiry
preserves separate source/process health when those remain fresh.

`data_valid_until` is the read side's source/snapshot deadline; `valid_until`
also includes the assessment's own expiry and evidence deadline. The UI polls
every 2 seconds, advances these deadlines locally from server time plus elapsed
monotonic client time (receipt anchors stay with cached responses across
page remounts, charging the full request round-trip duration against freshness),
and hides a retained candidate on a failed fetch or expiry without
waiting for a successful new poll. Gate outcomes on historical data must be
labeled as recorded evidence. No UI/API operation can place or size an order.
