# QT capability fusion — integration checklist

This is the review checklist for the approved fusion plan. It is deliberately
organised by P0–P8 product workflows, not by AST symbols or file counts. The
machine-readable source audit is an appendix to `/api/v3/capabilities`; it is
not completion evidence.

Status vocabulary: **code** means the retained/new entry point is implemented;
**native** means the real Nautilus or legacy runtime behavior was exercised;
**Docker** means the isolated-plugin boundary was exercised; **Cloudflare**
means an authenticated deployed edge was observed. A check in one column does
not imply a check in another.

| Plan | Product workflow and callable entry point | Owner | Code | Native / Docker evidence | Cloudflare / remaining gap |
| --- | --- | --- | --- | --- | --- |
| P0 | Capability learning: `GET /api/v3/capabilities`, `/catalog`, `/indicators`, `/signals`; source audit remains separate | lead | In progress: semantic matrix, exact manifest and audit appendix exist | N/A | Every preserved family still needs an explicit product-flow/adaptor/evidence decision; no AST count is treated as completion |
| P1 | Bar research: `POST /api/v3/experiments` → `scripts/run_workbench_worker.py` → `NautilusResearchExecutor` | lead + native | Implemented queue, immutable requests, data fingerprints, cost/feed rejection and attempt fencing | ARM real `BacktestEngine` fill/fee tests; 5y 1h + 10y 4h acceptance reported by native owner (11.574s, 75s CI budget) | Still must prove each source strategy adapter, multi-leg/tick/book/funding paths separately; production node capacity remains bounded |
| P2 | Chinese Web workbench: React → authenticated Worker → FastAPI; reports: `GET /api/v3/artifacts` then `/artifacts/<run>/<name>` | web + lead | Secure canonical `web/edge/worker.ts` is root entry; API has job/reproduce/result/artifact contracts; publisher permits only locally attested artifact-root files and exact private keys | Hostile path/symlink/special-file/hash/size and plugin-relative descriptor tests pass; local API/worker browser E2E is still in progress | **Blocked:** expired Cloudflare auth/no API token. No deployment URL or R2 publication has been observed |
| P3 | Source families: QT strategies/sim/evaluation, btc-qt S0/S1/S2/ladder, btc-quant Freqtrade/catalog/evolution, providers/scanners/risk/ops | source-port + lead | In progress: preserved source, readable attribution, exact ports/factories arriving; live remains disabled | btc-qt and Freqtrade parity suites are owner evidence; only native-factory-tested ports may become executable | Need final per-family mapping to a retained simulation/evaluation/native/legacy-runtime flow; unavailable data/legacy runtime stays explicit |
| P4 | Learn/edit/version/execute: `/strategies`, `/strategy-versions`, `/validate`, `/indicator-docs`, immutable plugin versions | lab + operations + lead | Lab CRUD/validation and documentation routes exist; worker injects Docker-only plugin runtime | Rule executor E2E crash was fixed at metadata warmup construction; plugin isolation has dedicated tests | Need final real rule/builtin/ensemble/regime execution graph and Docker acceptance; no plugin is runnable on the host |
| P5 | Compare/replay/optimize/validate: `/results`, `/results/{id}/traces`, `/compare`, `/optimizations`, `/validation` | lab + lead | Lab routes are mounted; common queue dispatches job types | Optuna/DSR/WF tests owned by Lab; native canonical metric integration pending final retest | Need a completed native result through compare/trace/artifact path and exact legacy research reproduction coverage |
| P6 | Data/ops/events/portfolio/paper: `/datasets`, `/sources`, `/imports`, `/sync-jobs`, `/events`, `/opportunities`, `/portfolio`, `/runtime`, `/commands`, `/audit` | operations + native + lead | Operations router uses the common queue and paper control protocol; live route is absent | Provider/import and operation tests are owner evidence | Tick/book event replay, funding, partial fills, exchange/testnet lifecycle, and recovery remain incomplete/explicitly unavailable; live is disabled |
| P7 | Full product acceptance and recovery: restart, stale attempt fencing, worker lock, data/version reproducibility, constrained-node safety | lead + all owners | Queue heartbeats, fencing, cancellation, stale recovery, one-process lock and resource policy implemented | Repository/worker regression evidence exists; native performance gate added by native owner | Requires final full-suite, browser workflow, Docker/plugin acceptance, source-family gap review, and deployment/recovery observations |
| P8 | Review, commit, push and deploy evidence | lead | Not started: no commit or push while integration changes are active | Final commands/results must be recorded before commit | **Blocked:** Cloudflare authentication. Push remains held for final parent review |

## Current bounded deployment policy

- Production workbench worker concurrency is one; queue limit is 20; the
  small node has a 512 MiB worker ceiling. Multi-year minute/tick studies are
  rejected or require a dedicated node.
- Dataset market and feed precision are explicit. The current catalog declares
  bar data only; trade/order-book requests fail closed until a matching feed
  manifest and native executor path are present.
- Historical dataset freshness is reported separately from immutable data
  validity, so a reproducible fixed-window run does not become invalid merely
  because a refresh is older than 48 hours.
- R2 report URLs are emitted only after a configured publisher writes a private
  `reports/<run_id>/...` key. Without credentials, artifacts remain visibly
  unpublished rather than receiving guessed links.
- The optional workbench deployment installs a loopback-only API unit and one
  bounded worker. R2 credentials are read only from `/opt/qt/.env.workbench`;
  the publisher never accepts a plugin-supplied host path or object key.

## Evidence index

- Focused root integration regression (after unified queue/API work):
  `./.venv/bin/python -m pytest tests/test_workbench.py tests/test_workbench_worker.py tests/test_research_repository.py tests/integration/test_btc_backtest_packaging.py -q`
- Dataset/API catalog contract regression:
  `./.venv/bin/python -m pytest tests/test_research_datasets.py tests/test_research_service.py tests/test_workbench.py -q`
- R2 attestation and private artifact-index regression:
  `./.venv/bin/python -m pytest tests/test_workbench_reports.py tests/test_workbench_worker.py -q`
- Native owner reported ARM real-engine suite and the multi-year timing above;
  rerun its exact `tests/nautilus` command in the native environment before P7.
- Cloudflare deployment has no evidence yet because authentication is expired.
