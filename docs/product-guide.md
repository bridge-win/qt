# QT product guide / 产品功能说明书

Scope: repository capabilities as of 2026-10-13. QT combines BTC strategy
research, paper monitoring, and an isolated Codex trading experiment.
Research results and sandbox fills do not establish profitable live trading.

截至 2026-10-13，QT 包含 BTC 策略研究、模拟盘监控和独立的 Codex 交易实验。
研究结果或测试环境成交不能证明实盘盈利能力。

## What is implemented / 已实现功能

| Product area / 产品模块 | Features / 功能 | Entry / boundary / 入口与边界 |
| --- | --- | --- |
| Research workbench v3<br>研究工作台 v3 | Learning, datasets, strategy cloning/editing/versioning, experiments, comparison, decision traces, wick events, validation/optimization, notes and artifacts<br>学习、数据集、策略克隆/编辑/版本管理、实验与比较、决策轨迹、插针事件、验证/优化、笔记与结果文件 | React UI `:5173`, FastAPI `:8877`; research execution only<br>仅用于研究执行 |
| Independent backtesting<br>独立回测 | Built-in/custom strategies, data sync, benchmarks, walk-forward, sensitivity, cost stress and reports<br>内置/自定义策略、数据同步、基准比较、滚动验证、敏感性与成本压力测试、报告 | `btc-backtest` CLI; usable without QT<br>命令行可脱离 QT 独立使用 |
| Strategy monitoring<br>策略监控 | DCA, capitulation, trend, carry, wick, overheating and cycle rules; opportunity scans, portfolio ledger, email/Telegram alerts<br>定投、恐慌、趋势、套利、插针、过热和周期策略；机会扫描、持仓账本、邮件/Telegram 提醒 | `./start.sh`, dashboard `:8765`; paper by default<br>默认模拟盘 |
| Codex BTC agent<br>Codex BTC 智能体 | Five-minute monitoring, completed daily signals, AI entry approval/veto, bounded orders, protective exits, durable reconciliation, dust accounting and restart recovery<br>每五分钟监控、完整日线信号、AI 审批/否决买入、限额下单、保护性退出、持久化对账、零碎余额记账、重启恢复 | Separate Docker worker/reviewer; paper or Binance Spot Testnet only<br>独立执行/审核容器；仅模拟盘或币安现货测试环境 |
| Deployment support<br>部署支持 | Docker, systemd/SSH scripts, optional Cloudflare authentication and private R2 reports<br>容器部署、服务管理/远程部署脚本、可选 Cloudflare 鉴权和私有 R2 报告 | Different deployment paths; configuration is required<br>部署路径不同，均需相应配置 |

The older `execution/live.py` broker has guarded real-exchange code. It is
separate from the Codex agent and does not make any research result live-ready.
The agent currently uses price/volume; its AI has no news, on-chain or macro feed.

旧版 `execution/live.py` 含有受风控约束的真实交易接口，与 Codex 智能体独立。
这不代表研究结果已具备实盘条件。智能体目前仅使用价格和成交量，AI 尚未接入新闻、链上或宏观数据。

## What you need to provide / 需要你提供的资源与权限

| Goal / 目标 | Required resources / access / 所需资源与权限 |
| --- | --- |
| Local research / 本地研究 | Python 3.12 for native research, Node/npm, historical OHLCV data, persistent storage; install the local `btc-backtest` package first<br>原生研究需 Python 3.12、Node/npm、历史行情及持久存储；先安装仓库中的 `btc-backtest` 包 |
| Paper monitoring / 模拟盘监控 | Python 3.10+ and network access to selected public providers; advanced datasets may need provider API keys<br>Python 3.10+、可访问所选公开数据源的网络；高级数据可能需要供应商密钥 |
| Codex paper entries / Codex 模拟买入 | Docker Compose, working public Binance access, `CODEX_API_KEY`; otherwise monitoring works but entries cannot be approved<br>Docker Compose、可访问币安公开行情的网络及 `CODEX_API_KEY`；缺少密钥时仍可监控，但不能批准买入 |
| Actual sandbox orders / 测试环境下单 | Dedicated `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET`, virtual BTC/USDT balances, plus Codex authentication<br>专用测试环境密钥、虚拟 BTC/USDT 余额及 Codex 鉴权 |
| Continuous operation / 持续运行 | An awake machine or server, persistent state, backups and operational checks; Docker restarts processes but does not prevent host sleep<br>保持运行且不休眠的电脑或服务器、持久状态、备份和运维检查；Docker 无法阻止主机休眠 |
| Optional notifications / cloud / 可选通知与云服务 | SMTP or Telegram credentials; server/SSH and authentication setup; R2 credentials only if publishing reports<br>邮件或 Telegram 凭据；服务器/SSH 与鉴权配置；发布报告时才需要 R2 凭据 |

Choose order/exposure limits and an acceptable loss policy before running an
experiment. Store keys locally in ignored environment files; never paste them
into chat. Binance Spot Testnet tests exchange orders; Bitcoin chain testnet
tests wallet transfers and is not implemented here.

运行前确定下单/敞口限额与可接受的亏损规则。密钥仅存放在 Git 忽略的本地环境文件中，勿粘贴到聊天。
币安现货测试环境用于验证交易所订单；比特币链上测试网用于验证钱包转账，本项目尚未实现后者。

## How to use / 如何使用

Run commands from the repository root. Use the workflow you need; they do not
all start through the same command.

以下命令从仓库根目录执行。按需选择工作流，各工作流的启动入口不同。

### Research workbench / 研究工作台

Create an isolated native environment and install dependencies:

创建独立的原生研究环境并安装依赖：

```bash
mkdir -p data/runtime data/parquet data/workbench
python3.12 -m venv data/runtime/research-venv
data/runtime/research-venv/bin/pip install -e packages/btc-backtest
data/runtime/research-venv/bin/pip install -e '.[dev,native-research]'
```

Run each service in a separate terminal:

以下三个服务分别在独立终端运行：

```bash
data/runtime/research-venv/bin/python scripts/run_workbench_api.py --parquet-root data/parquet --state-root data/workbench
data/runtime/research-venv/bin/python scripts/run_workbench_worker.py --parquet-root data/parquet --state-root data/workbench
cd web && npm ci && npm run dev
```

Open `http://127.0.0.1:5173`: select/import a ready dataset → clone a strategy
→ save a version → run an experiment → inspect costs, drawdown and decisions
→ compare on the same dataset. An empty data directory needs a supported
import/sync first. API documentation: `http://127.0.0.1:8877/docs`.

打开 `http://127.0.0.1:5173`：选择/导入可用数据集 → 克隆策略 → 保存版本 → 运行实验
→ 检查成本、回撤与决策 → 在相同数据上比较。空数据目录需先导入或同步数据。
API 文档：`http://127.0.0.1:8877/docs`。

### Strategy dashboard / standalone backtest / 策略看板与独立回测

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

看板地址：`http://127.0.0.1:8765`。在 `config/strategies/*.yaml` 调整策略，在 `.env` 配置可选提醒。
用 `./start.sh status` 查看状态，用 `./start.sh stop` 停止后台实例。详见上方回测及工作台指南。

### Codex BTC agent / Codex BTC 智能体

Copy the template only if the local environment file does not already exist:

仅在本地环境文件不存在时复制模板：

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

默认模拟盘；单笔及每日买入限额均为 25 USDT，买入敞口上限 100 USDT，最多一个活跃持仓，两次买入间隔七天。
上方 `halt` 命令阻止所有新订单，包括退出订单，但不会平仓。配置密钥并设为 `BTC_AGENT_MODE=testnet` 后，
按[智能体指南](codex-btc-agent.md)完成鉴权买卖测试、重启及恢复。智能体目前没有网页界面。

## TODO / acceptance still needed / 待办与待验收事项

- [ ] Complete authenticated Codex inference and actual sandbox buy/sell fills;
  [recorded acceptance](verification/codex-btc-agent-acceptance.md) currently covers public connectivity, tests and Docker paper operation.
  <br>完成 Codex 鉴权调用和测试环境真实买卖成交；现有验收记录仅覆盖公开接口、测试与 Docker 模拟运行。
- [ ] Complete a 24-hour soak, outage/restart recovery and state-backup checks.
  <br>完成连续 24 小时运行、故障/重启恢复及状态备份检查。
- [ ] Add agent health/failure notifications and a consolidated status UI.
  <br>增加智能体健康/故障通知和统一状态界面。
- [ ] Add timestamped news/on-chain/derivatives context and record resolved model identity before comparing AI versions.
  <br>接入带时间戳的新闻/链上/衍生品信息，并记录实际模型标识，以便比较 AI 版本。
- [ ] Validate frozen rules out of sample with realistic execution; the initial
  rule-only replay has two closed trades and cannot establish an edge.
  <br>在样本外用真实执行假设验证固定规则；初始规则回放仅有两笔完整交易，不能证明策略优势。
- [ ] Verify optional plugin isolation, R2 downloads and authenticated cloud
  access in the chosen deployment; retained source code is not operational acceptance.
  <br>在所选部署中验证插件隔离、R2 下载和云端鉴权访问；保留源码不等于通过运行验收。

See [folder map](project-structure.md) and [technical diagrams](technical-architecture.md).

另见[项目结构](project-structure.md)与[技术架构图](technical-architecture.md)。
