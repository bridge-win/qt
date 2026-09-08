# QT 研究工作台：架构、模块与部署

## 定位与业务流程

一个研究优先的 BTC 工作台：学习规则 → 选择数据 → 克隆/编辑策略 → 固定版本 → 回测 → 看历史决策、成本与回撤 → 同条件比较 → 决定是否继续研究。系统不保证获利，研究结果不能直接等同于实盘可成交结果。

以 qt 为主项目，保留 btc-qt 的事件/插针策略以及 btc-quant 的信号、配置和研究逻辑。原始实现与新入口分层保存，不同时运行三套完整服务。

## 技术结构

```text
浏览器：React / TypeScript / Vite
  └─ FastAPI：查询、版本、提交任务
       ├─ SQLite WAL：策略版本、任务、结果索引、笔记
       └─ 一个 Python worker：按队列执行研究
            ├─ 规则/原版策略适配 → Nautilus 原生回测
            ├─ Parquet：历史数据与数据指纹
            └─ JSON/CSV/Parquet 报告：本地工件，配置后发布私有 R2
```

不是微服务集群：一个前端、一个 API、一个研究 worker；不用 Redis/Celery/Kafka 支撑核心研究队列。SQLite 适合当前单用户/小规模研究，PostgreSQL 仅属于保留的旧平台能力，不是 v3 启动依赖。

| 模块 | 目录 | 职责 |
| --- | --- | --- |
| Web | `web/src` | 学习、数据、编辑、实验、比较、决策、运行状态；Ant Design、TanStack Query、Router |
| 图表/编辑器 | `web/src/charts` 等 | Lightweight Charts、ECharts、CodeMirror；显示结果而非浏览器内执行 Python 回测 |
| API 与协调 | `src/qt/workbench` | FastAPI、统一队列、数据目录、工件安全、运行边界 |
| 研究实验室 | `src/qt/lab` | 不可变策略版本、规则/组合、学习文档、比较、优化/验证、笔记 |
| 原生执行 | `src/qt/nautilus` | 原生订单/成交、费用、账户对账、因果决策轨迹和报告 |
| 源策略适配 | `src/qt/strategy_ports` | 保留来源和原版事件语义，不把同名策略视作同一种算法 |
| 原始研究留存 | `src/qt/legacy` | 迁移参考、同源对照及旧工作流；代码保留不等于全部 Web 可执行 |
| 公共回测基础 | `packages/btc-backtest` | 数据模型、基础策略、指标、独立回测/验证工具 |
| 原 qt 模块 | `src/qt/data`、`indicators`、`signal`、`risk`、`intel` 等 | 数据供应商、因子、风控、扫描与旧版模拟研究能力 |
| 部署 | `cloudflare`、`deploy` | 边缘鉴权/静态资源与 Linux API/worker 进程管理 |

核心 Python 原生环境为 3.12，Nautilus 固定为 2.0.0rc4，TA-Lib/Optuna 为可选研究依赖。Nautilus 是预发布版本，因此现阶段应称“研究候选版本”，不应宣传为所有交易模式均已生产验收。

## 本地测试

页面：<http://127.0.0.1:5173/>；API：<http://127.0.0.1:8877/docs>。浏览器通过 Vite 的 `/api` 代理访问 API。

当前验收状态目录为 `/private/tmp/qt-workbench-e2e.1MIhiW/state`，数据目录为 `/private/tmp/qt-workbench-e2e.1MIhiW/parquet`。这是临时测试环境，不能作为长期保存重要研究的唯一副本。原始仓库历史数据未被替换。

以下是当前验收环境的重启命令，分别在三个终端运行；不要在已有进程上重复启动。

```bash
cd /Users/kwt/x/qt
PYTHONPATH=src:packages/btc-backtest/src /private/tmp/qt-nautilus-arm312/bin/python scripts/run_workbench_api.py --host 127.0.0.1 --port 8877 --parquet-root /private/tmp/qt-workbench-e2e.1MIhiW/parquet --state-root /private/tmp/qt-workbench-e2e.1MIhiW/state
```

```bash
cd /Users/kwt/x/qt
PYTHONPATH=src:packages/btc-backtest/src /private/tmp/qt-nautilus-arm312/bin/python scripts/run_workbench_worker.py --parquet-root /private/tmp/qt-workbench-e2e.1MIhiW/parquet --state-root /private/tmp/qt-workbench-e2e.1MIhiW/state
```

```bash
cd /Users/kwt/x/qt/web
QT_WORKBENCH_API_ORIGIN=http://127.0.0.1:8877 npm run dev -- --host 127.0.0.1 --port 5173
```

建议先测试：学习页查看 RSI/EMA 公式；策略页克隆内置策略并保存新版本；实验页选择现货数据、1 倍杠杆和手续费；结果页查看费用/回撤并复现；用相同数据和成本比较两个版本。不要将固定小时 K 线样例用于证明能买到针尖。

## Cloudflare 部署

```text
用户 → Cloudflare Access → Worker + 静态 Web
                              ├─ 私有 R2 报告
                              └─ Tunnel → Linux FastAPI → 单研究 worker
```

Cloudflare 托管网页、鉴权和访问入口，Python/Nautilus 的重回测在 Linux 计算节点执行，不放到 Worker 里。

部署顺序：

1. Linux 安装 Python 3.12、项目及研究依赖，设置持久数据/状态目录，配置 `qt-workbench-api` 与 `qt-workbench-worker` systemd 单元。
2. 检查节点容量；现有小节点的配置限制为单 worker，不部署长年分钟/逐笔研究。不要用旧版 paper 服务的内存建议代替原生回测容量评估。
3. 建立 Tunnel 与 Access，API 保持回环监听，Worker 验证 Access JWT 并注入源站凭据。
4. 构建 `web/dist`，使用 `cloudflare/wrangler.toml` 部署 Worker 和静态文件。配置 `ACCESS_JWT_ISSUER`、`ACCESS_JWT_AUDIENCE`、`ORIGIN_URL`，凭据通过 secret/受保护环境文件设置。
5. 配置私有桶 `qt-research-reports`；计算节点的 `QT_R2_REPORTS_BUCKET` 必须使用同一桶名。没有 R2 凭据时结果只保留本地，不能假造下载链接。
6. 验证未登录拒绝访问、登录后页面及 API、提交/完成/复现、报告下载和重启恢复，再记录正式访问地址。

当前没有完成 Cloudflare 上线验收，旧设备授权已过期。新一轮授权、容量核验及部署属于后续独立操作。详细脚本参考 `deploy/README.md`，核心范围与未完成事项参考本轮 TODO spec。
