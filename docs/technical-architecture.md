# QT technical architecture

The repo contains distinct runtimes. The research workbench, strategy monitor
and Codex agent have separate execution paths and state; they share selected
libraries rather than a single trading controller.
[Product guide](product-guide.md) · [Folder map](project-structure.md).

## Research workbench v3

```mermaid
flowchart TD
  UI["web/src: React + TypeScript + Vite"] --> API["workbench/api.py: FastAPI /api/v3"]
  API --> LAB["lab/: versions, experiments, notes, learning"]
  API --> OPS["workbench/: datasets, operations, artifacts"]
  LAB --> DB[("research.sqlite3: SQLite WAL")]
  OPS --> DB
  DB --> Q["research/: leases, heartbeat, recovery, job queue"]
  Q --> W["workbench/worker.py: one research worker"]
  W --> N["nautilus/: native orders, fills, accounting"]
  W --> R["research/ + lab/: validation and comparison"]
  N --> PORTS["strategy_ports/: source-specific adapters"]
  PORTS --> LEGACY["legacy/: retained original strategies"]
  DATA[("Parquet datasets + fingerprints")] --> N
  DATA --> R
  N --> OUT["Local reports and causal decision traces"]
  R --> OUT
  OUT --> API
  OUT -. optional publication .-> R2["Private R2 artifacts"]
```

The API persists submissions; the worker owns execution and recovery. The UI
renders results, it does not run Python simulations. Native work needs Python
3.12+ and the declared research extras. SQLite is the v3 queue/metadata store;
PostgreSQL belongs to the older `platform/` runtime. Built-in and compatible
ported strategies run through the native bridge; custom plugins require the
separate isolated runtime and admission checks.

## Shared quant libraries and older strategy monitor

```mermaid
flowchart LR
  FEED["data/: market, macro, derivatives, sentiment"] --> FACT["indicators/ + signal/: features and decisions"]
  FACT --> STRAT["strategies/ + intel/: opportunities"]
  STRAT --> RISK["risk/: sizing, stops, gates"]
  RISK --> BROKER["execution/: paper default, guarded live"]
  BROKER --> BOOK["portfolio/: durable ledger"]
  STRAT --> MON["monitoring/: heartbeat and alerts"]
  BOOK --> DASH["dashboard/: FastAPI + local UI"]
  MON --> DASH
  FEED --> BT["backtest/ + research/: historical execution"]
  BT --> PKG["btc-backtest: standalone engine and validation"]
  PKG --> REPORT["Metrics, benchmarks and artifacts"]
```

`core/` supplies shared types/settings/logging; `reporting/` compares outcomes.
`scripts/run_all.py` runs enabled strategy/scanner threads; `run_service.py`
supervises the older paper service. The independent package owns its own data,
strategies, accounting, signals and reports. `strategy_ports/` preserves source
semantics rather than treating similarly named strategies as identical.

## Isolated Codex BTC agent

```mermaid
flowchart TD
  CLI["__main__.py + config.py: lifecycle and settings"] --> LOOP["service.py: monitoring and risk controller"]
  M["market.py: production public BTC candles"] --> S["signals.py: completed daily extreme rules"]
  S --> LOOP
  LOOP -->|"entry candidate over HTTP"| AI["reviewer.py: Codex CLI, strict approve/reason JSON"]
  AI -->|"approve or veto"| LOOP
  LOOP --> DB[("store.py: SQLite WAL, intent before submit")]
  LOOP --> E["broker.py: Binance Spot Testnet IOC limit"]
  LOOP --> P["paper.py: persistent virtual fills"]
  E -->|"client-ID lookup, fills and fees"| DB
  P --> DB
  DB -->|"position, dust, pending orders, budgets"| LOOP
  M --> REPLAY["backtest.py: rule-only next-open diagnostic"]
```

- Worker container: exchange keys and persistent state; one writer per mode.
- Reviewer container: only Codex authentication, no broker keys or state volume;
  fresh temporary home, disabled tools, schema validation and timeout.
- AI reviews new entries. Stops/trailing/time exits use fresh execution quotes
  without waiting for AI; a kill file blocks all new orders.
- Uncertain submission blocks further orders until reconciled. Terminal partial
  fills update inventory once; confirmed unsellable residuals remain tracked as dust.
- Signals use production public history; testnet liquidity supplies execution
  quotes. Modes are limited to paper/testnet, with separate SQLite ledgers.

## Deployment boundaries

```mermaid
flowchart LR
  USER["Browser"] --> EDGE["Caddy or Cloudflare Access + Worker"]
  EDGE --> WEB["Built React static files"]
  EDGE --> API["Loopback FastAPI workbench API"]
  API --> STATE[("SQLite / Parquet / local artifacts")]
  STATE --> WORKER["Linux research worker / systemd"]
  DOCKER["Separate Docker Compose project"] --> AGENT["BTC agent worker + reviewer"]
  AGENT --> VOL[("Named agent state volume")]
```

Cloud configuration does not prove a deployed/authenticated service. Native
backtests run on the compute node, not the edge Worker. The older platform
Compose path uses SQLAlchemy/PostgreSQL and migrations; it is optional and
independent of v3 and the agent. Agent containers publish no host ports and
restart unless stopped; an awake host and external operational checks are still
needed. [Agent acceptance](verification/codex-btc-agent-acceptance.md) records
which integrations were tested and which remain pending.
