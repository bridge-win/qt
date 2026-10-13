# QT product guide

Scope: repository capabilities as of 2026-10-13. QT combines BTC strategy
research, paper monitoring, and an isolated Codex trading experiment.
Research results and sandbox fills do not establish profitable live trading.

## What is implemented

| Product area | Features | Entry / boundary |
| --- | --- | --- |
| Research workbench v3 | Learning, datasets, strategy cloning/editing/versioning, experiments, comparison, decision traces, wick events, validation/optimization, notes and artifacts | React UI `:5173`, FastAPI `:8877`; research execution only |
| Independent backtesting | Built-in/custom strategies, data sync, benchmarks, walk-forward, sensitivity, cost stress and reports | `btc-backtest` CLI; usable without QT |
| Strategy monitoring | DCA, capitulation, trend, carry, wick, overheating and cycle rules; opportunity scans, portfolio ledger, email/Telegram alerts | `./start.sh`, dashboard `:8765`; paper by default |
| Codex BTC agent | Five-minute monitoring, completed daily signals, AI entry approval/veto, bounded orders, protective exits, durable reconciliation, dust accounting and restart recovery | Separate Docker worker/reviewer; paper or Binance Spot Testnet only |
| Deployment support | Docker, systemd/SSH scripts, optional Cloudflare authentication and private R2 reports | Different deployment paths; configuration is required |

The older `execution/live.py` broker has guarded real-exchange code. It is
separate from the Codex agent and does not make any research result live-ready.
The agent currently uses price/volume; its AI has no news, on-chain or macro feed.

## What you need to provide

| Goal | Required resources / access |
| --- | --- |
| Local research | Python 3.12 for native research, Node/npm, historical OHLCV data, persistent storage; install the local `btc-backtest` package first |
| Paper monitoring | Python 3.10+ and network access to selected public providers; advanced datasets may need provider API keys |
| Codex paper entries | Docker Compose, working public Binance access, `CODEX_API_KEY`; otherwise monitoring works but entries cannot be approved |
| Actual sandbox orders | Dedicated `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET`, virtual BTC/USDT balances, plus Codex authentication |
| Continuous operation | An awake machine or server, persistent state, backups and operational checks; Docker restarts processes but does not prevent host sleep |
| Optional notifications / cloud | SMTP or Telegram credentials; server/SSH and authentication setup; R2 credentials only if publishing reports |

Choose order/exposure limits and an acceptable loss policy before running an
experiment. Store keys locally in ignored environment files; never paste them
into chat. Binance Spot Testnet tests exchange orders; Bitcoin chain testnet
tests wallet transfers and is not implemented here.

## How to use

Run commands from the repository root. Use the workflow you need; they do not
all start through the same command.

### Research workbench

Create an isolated native environment and install dependencies:

```bash
mkdir -p data/runtime data/parquet data/workbench
python3.12 -m venv data/runtime/research-venv
data/runtime/research-venv/bin/pip install -e packages/btc-backtest
data/runtime/research-venv/bin/pip install -e '.[dev,native-research]'
```

Run each service in a separate terminal:

```bash
data/runtime/research-venv/bin/python scripts/run_workbench_api.py --parquet-root data/parquet --state-root data/workbench
data/runtime/research-venv/bin/python scripts/run_workbench_worker.py --parquet-root data/parquet --state-root data/workbench
cd web && npm ci && npm run dev
```

Open `http://127.0.0.1:5173`: select/import a ready dataset → clone a strategy
→ save a version → run an experiment → inspect costs, drawdown and decisions
→ compare on the same dataset. An empty data directory needs a supported
import/sync first. API documentation: `http://127.0.0.1:8877/docs`.

### Strategy dashboard / standalone backtest

```bash
./start.sh setup
./start.sh daemon
data/runtime/research-venv/bin/btc-backtest strategies list
data/runtime/research-venv/bin/btc-backtest data sync --provider bitstamp --symbol BTC/USD --timeframe 1d --years 10
data/runtime/research-venv/bin/btc-backtest run sma_crossover --provider bitstamp --symbol BTC/USD --timeframe 1d --years 10
```

Dashboard: `http://127.0.0.1:8765`. Tune `config/strategies/*.yaml`; configure
optional alerts in `.env`. Use `./start.sh status` to inspect and
`./start.sh stop` to stop the background instance. Detailed guides: [backtesting](btc-backtest.md) and
[workbench](workbench-architecture-and-deployment.md).

### Codex BTC agent

Copy the template only if the local environment file does not already exist:

```bash
mkdir -p data/runtime
test -f data/runtime/btc-agent.env || cp config/btc-agent.env.example data/runtime/btc-agent.env
chmod 600 data/runtime/btc-agent.env
docker compose --env-file data/runtime/btc-agent.env -f docker-compose.btc-agent.yml up -d --build
docker compose -f docker-compose.btc-agent.yml exec worker python -m qt.btc_agent status
```

Default: paper mode, 25-USDT order/daily-buy limits, 100-USDT entry exposure cap,
one active position and seven days between buys. Run
`docker compose -f docker-compose.btc-agent.yml exec worker python -m qt.btc_agent halt`
to stop all new orders,
including exits; it does not liquidate. After configuring keys and setting
`BTC_AGENT_MODE=testnet`, follow the [agent guide](codex-btc-agent.md) for the
authenticated round-trip, restart and resume commands. The agent has no web UI.

## TODO / acceptance still needed

- [ ] Complete authenticated Codex inference and actual sandbox buy/sell fills;
  [recorded acceptance](verification/codex-btc-agent-acceptance.md) currently covers public connectivity, tests and Docker paper operation.
- [ ] Complete a 24-hour soak, outage/restart recovery and state-backup checks.
- [ ] Add agent health/failure notifications and a consolidated status UI.
- [ ] Add timestamped news/on-chain/derivatives context and record resolved model identity before comparing AI versions.
- [ ] Validate frozen rules out of sample with realistic execution; the initial
  rule-only replay has two closed trades and cannot establish an edge.
- [ ] Verify optional plugin isolation, R2 downloads and authenticated cloud
  access in the chosen deployment; retained source code is not operational acceptance.

See [folder map](project-structure.md) and [technical diagrams](technical-architecture.md).
