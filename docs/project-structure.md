# QT project structure / 项目文件夹结构

Main directories and entry files; generated caches and individual strategy
files are omitted. [Product guide](product-guide.md) · [Architecture](technical-architecture.md).

展示主要目录和入口文件，省略生成缓存与单个策略文件。[产品说明](product-guide.md) · [技术架构](technical-architecture.md)。

```text
qt/
├── README.md                         Overview and existing guides / 概览与已有指南
├── AGENTS.md                         Repository workflow rules / 仓库工作流规范
├── pyproject.toml                    Python package, extras and qt CLI / Python 包、可选依赖与命令行
├── start.sh                          Strategy dashboard/paper launcher / 策略看板与模拟盘启动器
├── Dockerfile.btc-agent               Isolated monitoring + Codex images / 独立监控与 Codex 镜像
├── docker-compose.btc-agent.yml       Agent worker/reviewer and state volume / 智能体执行器、审核器与状态卷
├── Dockerfile.platform               Older PostgreSQL platform image / 旧版 PostgreSQL 平台镜像
├── docker-compose.platform*.yml       Older platform deployment / 旧版平台部署
├── requirements*.txt / *.lock         Runtime/build dependency sets / 运行与构建依赖
├── config/                           Strategy YAML and environment templates / 策略 YAML 与环境模板
├── src/qt/
│   ├── cli.py                        qt commands: data, backtest, status / 数据、回测与状态命令
│   ├── core/                         Types, settings, logging, provenance / 类型、配置、日志与来源记录
│   ├── data/                         Market/provider ingestion and Parquet store / 行情采集与 Parquet 存储
│   ├── indicators/                   Price, volatility, derivatives, on-chain factors / 价格、波动、衍生品与链上因子
│   ├── signal/                       Composite and multi-timeframe signal engines / 综合与多周期信号引擎
│   ├── risk/                         Sizing, stops and risk decisions / 仓位计算、止损与风控决策
│   ├── strategies/                   Strategy gallery, runners and simulations / 策略库、运行器与模拟
│   ├── intel/                        Opportunity discovery and ranking / 机会发现与排序
│   ├── execution/                    Shared broker interface, paper/guarded live / 统一交易接口、模拟与受控实盘
│   ├── portfolio/                    Durable position and P&L ledger / 持久化持仓与盈亏账本
│   ├── monitoring/                   Alerts, heartbeat and watchdog supervision / 提醒、心跳与进程监护
│   ├── backtest/                     QT engines, validation and package adapters / QT 引擎、验证与包适配
│   ├── reporting/                    Benchmark comparisons / 基准收益比较
│   ├── dashboard/                    Older local dashboard and v2 endpoints / 旧版本地看板与 v2 接口
│   ├── workbench/                    v3 API, worker, datasets, operations, plugins / v3 接口、执行器、数据集、运维与插件
│   ├── lab/                          Versions, learning, experiments and comparison / 版本、学习、实验与比较
│   ├── research/                     Durable queue, execution and validation services / 持久任务队列、执行与验证服务
│   ├── nautilus/                     Native research engine and fill/report bridge / 原生研究引擎与成交/报告桥接
│   ├── strategy_ports/               Source-strategy compatibility adapters / 来源策略兼容适配器
│   ├── legacy/                       Retained source families and reference workflows / 保留的原始策略与参考流程
│   ├── platform/                     Older PostgreSQL API/worker/control plane / 旧版 PostgreSQL 接口、执行与控制层
│   └── btc_agent/                    Separate Codex BTC experiment / 独立 Codex BTC 实验
│       ├── __main__.py / config.py   CLI and validated operational settings / 命令行与经校验的运行配置
│       ├── market.py / signals.py    Public data and completed-daily rules / 公开数据与完整日线规则
│       ├── reviewer.py              Headless Codex schema review + HTTP service / 非交互 Codex 结构化审核与 HTTP 服务
│       ├── service.py                Monitoring, risk gates and reconciliation / 监控、风控门禁与对账
│       ├── broker.py / paper.py      Testnet-only broker and virtual execution / 仅测试环境交易接口与模拟执行
│       ├── store.py                  SQLite intents, fills, reviews and inventory / SQLite 订单意图、成交、审核与持仓
│       └── backtest.py               Rule-only diagnostic replay / 仅规则的诊断回放
├── packages/btc-backtest/
│   ├── src/btc_backtest/             Standalone data/strategies/engine/signals/reports / 独立数据、策略、引擎、信号与报告
│   ├── examples/                     Custom strategy examples / 自定义策略示例
│   └── tests/                        Package-level verification / 包级验证
├── web/
│   ├── src/App.tsx / api.ts          Research routes and HTTP client / 研究页面路由与 HTTP 客户端
│   ├── src/pages/                    Learning, data, strategies, experiments, comparison / 学习、数据、策略、实验与比较页面
│   ├── src/charts/ / editor/         Charts and strategy editing / 图表与策略编辑
│   ├── src/edge/ / edge/             Edge support and tests / 边缘服务支持与测试
│   └── package.json / vite.config.ts React/TypeScript tooling and API proxy / 前端工具链与 API 代理
├── scripts/                          Data, API, worker, monitoring and report launchers / 数据、接口、执行器、监控与报告入口
├── tests/                            QT unit, integration, native and agent tests / QT 单元、集成、原生与智能体测试
├── deploy/                           SSH/systemd/Caddy and deployment helpers / 远程部署、服务管理与网关配置
├── cloudflare/                       Worker, static assets and authentication config / 边缘 Worker、静态资源与鉴权配置
├── migrations/ / alembic.ini         Older PostgreSQL schema migrations / 旧版 PostgreSQL 数据库迁移
├── .github/workflows/                CI and deployment automation / 持续集成与自动部署
├── docs/                             Product, research, operations and acceptance / 产品、研究、运维与验收文档
└── data/                             Local datasets/state/artifacts; not application source / 本地数据、状态与结果，非应用源码
    ├── parquet/                      Historical datasets / 历史数据集
    ├── workbench/                    Workbench databases and reports / 工作台数据库与报告
    └── runtime/                      Heartbeats, local agent settings and diagnostics / 心跳、本地智能体配置与诊断结果
```

Start reading: `web/src/App.tsx` → `workbench/api.py` → `workbench/worker.py`
for research; `btc_agent/__main__.py` → `service.py` for the agent;
`scripts/run_all.py` → `strategies/runner.py` for the older paper dashboard.
`legacy/` preserves provenance and is not a set of services to start together.
Keep credentials, generated datasets, databases, caches and build outputs out of Git.

阅读顺序：研究工作台从 `web/src/App.tsx` → `workbench/api.py` → `workbench/worker.py`；
智能体从 `btc_agent/__main__.py` → `service.py`；旧版模拟看板从 `scripts/run_all.py` → `strategies/runner.py`。
`legacy/` 用于保留来源实现，不应将其中所有服务同时启动。凭据、生成数据、数据库、缓存和构建输出不应提交到 Git。
