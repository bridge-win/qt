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

## Direct TC production entry (2026-09-10)

The active direct entry is <https://qt.eatfear.com/>. DNSPod already had an
enabled `qt` A record pointing to `101.32.243.66` (TTL 600); it was verified
in the Tencent Cloud console without changing other DNS records.

The `qt-caddy` container uses host networking and serves `/opt/qt/web/dist`.
It now routes `/api/*` to `127.0.0.1:8877`, replacing the obsolete port 8080
upstream. Frontend requests remain same-origin; leave `VITE_QT_API_BASE` unset.
The API and native worker remain systemd services with their existing state,
resource limits, and origin credential checks.

`deploy/tc-direct.Caddyfile` is the credential-free template. The active
`/opt/qt/deploy/tc.Caddyfile` is rendered on TC, root-owned and mode 600.
It protects the entire direct site with HTTPS Basic Auth, replaces client
identity headers with the single authenticated `direct:qt` identity, and
injects the existing origin credentials only after authentication. It also
preserves the `101.32.243.66.sslip.io` Cloudflare origin. This is a single-user
entry, not a multi-user permission system. Unpublished `/artifacts/*` paths
return 404; R2 publication was not enabled by this deployment.

The login username is `qt`. The generated password is kept only in the
root-readable `/root/qt-eatfear-access.json` on TC. Retrieve it over the
existing SSH connection; never copy that file or the rendered Caddyfile
into Git. The one-time render/reload helper is retained at
`/root/qt-configure-direct.py`; inspect it before reuse, especially its backup
path. It retains the login, reads `/opt/qt/.env.workbench`, validates the
candidate with Caddy, and restores the previous configuration if reload fails.
Keep the existing Caddyfile inode when updating it because Docker bind-mounts
that file. Restarting the container uses the persisted rendered configuration.

Application source deployed: `4f69173`. Frontend production build passed;
8 edge tests and 39 focused Python tests passed. Live HTTPS checks confirmed
anonymous page/API 401, authenticated page/deep-link/API 200, API misses as
JSON 404, and unauthenticated direct-origin API 401. A weekly-DCA backtest
submitted through the new public hostname completed as
`2381330f44634c6dbcd8f13f77d891da`; reproduction
`ac30099db6ef45f08b7ef2a4239676b1` returned identical metrics. The worker was
online and live trading remained disabled. Chrome displayed the native login
prompt; interactive page rendering after user login remains unverified.

Before deployment, the queue had no running jobs. Source/config and consistent
SQLite snapshots were saved under `/root/qt-deploy-backups/20260910-eatfear/`.
For code rollback, stop the API/worker, restore `code-before.tar.gz` into
`/opt/qt`, reload Caddy, then restart both services. Do not restore the SQLite
snapshots for an ordinary code rollback: they predate subsequent user jobs.
The old Caddyfile points the direct domain at inactive port 8080, so restoring
it also restores that old direct-entry failure.

The Cloudflare configuration for `qt.xvis.cc` is a separate deployment path;
it still contains an Access audience placeholder and was not deployed here.
Do not deploy it until the real Access application audience is configured.

## TC deployment verification (2026-09-08, historical)

The web build, FastAPI, SQLite state, Parquet datasets, native research worker,
and generated reports are hosted on TC under `/opt/qt`. Cloudflare remains
the authenticated public entry point and forwards API requests to TC HTTPS.
No process on the developer's Mac is required for this path.

- Public entry: <https://qt.xvis.cc/> (Workers custom domain on the Cloudflare-hosted
  `xvis.cc` zone; `workers_dev = false`). The SPA and `/api/v3/*` share this
  hostname, so no CORS configuration exists or is needed on the FastAPI side.
  Switching the entry hostname = new Access application for that hostname +
  its AUD tag in `ACCESS_JWT_AUDIENCE` + `wrangler deploy`; nothing on the
  origin server changes.
- TC static web/origin: <https://101.32.243.66.sslip.io/>. Its API intentionally
  rejects requests without the Worker origin credentials; use the public
  entry for interactive research.
- Python 3.12.14 is installed under `/opt/qt/python`; API and worker use
  `/opt/qt/.venv-native-research`. Nautilus is 2.0.0rc4, Optuna 4.5.0,
  and TA-Lib 0.7.1.
- `qt-workbench-api` and `qt-workbench-worker` use the repository systemd
  units. Caddy serves `/opt/qt/web/dist` and proxies `/api/*` to port 8877.
- State and reports: `/opt/qt/data/workbench`; historical data:
  `/opt/qt/data/parquet`. The existing OKX BTC/USDT hourly dataset contains
  43,800 rows, ending 2026-06-04; it is historical, not a current feed.
- The legacy `btc-qt-qt-record-1` container was stopped to free about 280 MiB
  on this 2 GiB server. Its data and container remain intact and it can be
  restored with `docker start btc-qt-qt-record-1`. Reassess capacity before
  running it alongside native research. It is not part of v3 task execution.

Acceptance: TC native engine/lab/queue suite: 68 passed, 1 performance test
deselected. Worker/report regression suite: 22 passed locally. An API-submitted
weekly-DCA backtest over January 2026 completed, and reproduction returned
identical metrics. Job IDs: `a2681fca1952461a9855b51e33c5995b` and
`a3fe4c810e8a43c2a192bc7dc99a439b`. These verify execution and reproducibility,
not profitability or every migrated strategy. HTTPS returned 200 on TC and
302 to authentication on Cloudflare; a fresh logged-in browser acceptance
was not completed in this run.

Repeat the API check on TC (creates labeled research records):

```bash
cd /opt/qt
.venv-native-research/bin/python scripts/verify_tc_workbench.py
systemctl status qt-workbench-api qt-workbench-worker
journalctl -u qt-workbench-worker -n 50 --no-pager
```

Remaining deployment boundary: the pinned custom-plugin image
`qt-plugin-runtime:py312-nautilus-2.0.0rc4` was built successfully, but the
`qt` service user has not been granted Docker access. Automatic approval
rejected adding it to the Docker group because that grants broad host
control. Custom Python plugins are therefore not accepted as operational;
built-in strategies and rule-based native backtests do not require Docker.
R2 publication remains optional; reports persist on TC, and unconfigured R2
must not be presented as a working browser download. No live trading is enabled.
