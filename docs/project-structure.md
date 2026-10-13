# QT project structure

Main directories and entry files; generated caches and individual strategy
files are omitted. [Product guide](product-guide.md) · [Architecture](technical-architecture.md).

```text
qt/
├── README.md                         Overview and existing guides
├── AGENTS.md                         Repository workflow rules
├── pyproject.toml                    Python package, extras and qt CLI
├── start.sh                          Strategy dashboard/paper launcher
├── Dockerfile.btc-agent               Isolated monitoring + Codex images
├── docker-compose.btc-agent.yml       Agent worker/reviewer and state volume
├── Dockerfile.platform               Older PostgreSQL platform image
├── docker-compose.platform*.yml       Older platform deployment
├── requirements*.txt / *.lock         Runtime/build dependency sets
├── config/                           Strategy YAML and environment templates
├── src/qt/
│   ├── cli.py                        qt commands: data, backtest, status
│   ├── core/                         Types, settings, logging, provenance
│   ├── data/                         Market/provider ingestion and Parquet store
│   ├── indicators/                   Price, volatility, derivatives, on-chain factors
│   ├── signal/                       Composite and multi-timeframe signal engines
│   ├── risk/                         Sizing, stops and risk decisions
│   ├── strategies/                   Strategy gallery, runners and simulations
│   ├── intel/                        Opportunity discovery and ranking
│   ├── execution/                    Shared broker interface, paper/guarded live
│   ├── portfolio/                    Durable position and P&L ledger
│   ├── monitoring/                   Alerts, heartbeat and watchdog supervision
│   ├── backtest/                     QT engines, validation and package adapters
│   ├── reporting/                    Benchmark comparisons
│   ├── dashboard/                    Older local dashboard and v2 endpoints
│   ├── workbench/                    v3 API, worker, datasets, operations, plugins
│   ├── lab/                          Versions, learning, experiments and comparison
│   ├── research/                     Durable queue, execution and validation services
│   ├── nautilus/                     Native research engine and fill/report bridge
│   ├── strategy_ports/               Source-strategy compatibility adapters
│   ├── legacy/                       Retained source families and reference workflows
│   ├── platform/                     Older PostgreSQL API/worker/control plane
│   └── btc_agent/                    Separate Codex BTC experiment
│       ├── __main__.py / config.py   CLI and validated operational settings
│       ├── market.py / signals.py    Public data and completed-daily rules
│       ├── reviewer.py              Headless Codex schema review + HTTP service
│       ├── service.py                Monitoring, risk gates and reconciliation
│       ├── broker.py / paper.py      Testnet-only broker and virtual execution
│       ├── store.py                  SQLite intents, fills, reviews and inventory
│       └── backtest.py               Rule-only diagnostic replay
├── packages/btc-backtest/
│   ├── src/btc_backtest/             Standalone data/strategies/engine/signals/reports
│   ├── examples/                     Custom strategy examples
│   └── tests/                        Package-level verification
├── web/
│   ├── src/App.tsx / api.ts          Research routes and HTTP client
│   ├── src/pages/                    Learning, data, strategies, experiments, comparison
│   ├── src/charts/ / editor/         Charts and strategy editing
│   ├── src/edge/ / edge/             Edge support and tests
│   └── package.json / vite.config.ts React/TypeScript tooling and API proxy
├── scripts/                          Data, API, worker, monitoring and report launchers
├── tests/                            QT unit, integration, native and agent tests
├── deploy/                           SSH/systemd/Caddy and deployment helpers
├── cloudflare/                       Worker, static assets and authentication config
├── migrations/ / alembic.ini         Older PostgreSQL schema migrations
├── .github/workflows/                CI and deployment automation
├── docs/                             Product, research, operations and acceptance
└── data/                             Local datasets/state/artifacts; not application source
    ├── parquet/                      Historical datasets
    ├── workbench/                    Workbench databases and reports
    └── runtime/                      Heartbeats, local agent settings and diagnostics
```

Start reading: `web/src/App.tsx` → `workbench/api.py` → `workbench/worker.py`
for research; `btc_agent/__main__.py` → `service.py` for the agent;
`scripts/run_all.py` → `strategies/runner.py` for the older paper dashboard.
`legacy/` preserves provenance and is not a set of services to start together.
Keep credentials, generated datasets, databases, caches and build outputs out of Git.
