# Codex BTC Agent 独立安全验收 — 2026-10-15

结论：**离线执行与恢复验收通过；完整运行验收尚未通过。**
本次未提交任何交易所订单，未启用真实资金交易，未读取、保存或输出密钥值。
模拟成交不是 Binance 成交证据，短时故障测试也不是连续 24 小时运行证据。

## 基线与证据边界

- 检查 main：`14ab233065d815583b39194c8e93c849db1318aa`。
- 已读取产品说明、操作指南、2026-10-13 验收记录、全部 BTC Agent 模块、
  Dockerfile、Compose 和环境模板。
- 已读取[合并记录](https://drive.google.com/file/d/1qZtlOq2Z54wxOW9n36mFnvSBd8yrnKlq/view)：
  记录确认 `0784adc` 合并、177 项测试通过，明确不代表部署或鉴权交易验收。
- 本次为 Python 3.12.14 原生进程测试；容器配置使用 Python 3.13，不能视为相同运行环境。
- 使用 `requirements-btc-agent.txt` 的顶层版本安装隔离测试环境；未将回测缓存修复作为 Agent 验收依据。

## 本次验证

| 项目 | 结果与证据 |
| --- | --- |
| Agent 离线测试 | 181 项通过：原 177 项，加 4 项恢复测试 |
| 扩展回归 | 加上当前价格/组合指标、旧 paper broker 和 live guardrail 测试，共 210 项通过；这些不算测试网成交证据 |
| 静态检查 | Ruff 通过；11 个 Agent 源模块严格 mypy 通过 |
| 订单与风控 | 原有测试复跑：限额、费用储备、冷却期、单持仓、报价时效/价差、审批否决及异常、审批后价格变化、halt、模式隔离 |
| 成交与对账 | 原有测试复跑：零成交/部分成交、费用、重复记账、未知提交结果、库存不符、dust 持久化 |
| 真实子进程异常退出 | 新测试在意图提交后、paper 成交提交后、持仓提交后分别 `os._exit`；不执行连接清理或 finally。重开数据库后检查 integrity、未提交事务回滚、持仓/现金、日限额账本对应的成交成本和订单数量 |
| 未知结果恢复 | 未成交的持久意图保持阻塞；已持久化的 paper 成交按原 client ID 记账，连续三轮对账不生成重复订单；halt 不阻止已有成交对账 |
| 在线备份恢复 | SQLite backup API 在源连接及 WAL 开启时备份含 pending 成交的状态；恢复副本成功对账并在行情/AI 不可用时执行保护性止损，源状态不被修改 |
| Docker 验收 fixture | Python fixture 在本机完成 crash → recover 两进程执行；shell 语法及 workflow YAML 解析通过。**Docker 包装层未运行** |

新增 `tests/test_btc_agent_recovery.py` 不访问外部服务。进程退出在确定性提交边界注入，
未覆盖任意 CPU 指令时刻 SIGKILL、主机断电、磁盘损坏、磁盘满或 Docker daemon 崩溃。

## Docker 静态复核与新增自动化

当前 Compose 默认 paper，worker/reviewer 分离；没有宿主端口；非 root、只读根文件系统、
capabilities 清空、禁止提权、临时目录与资源/日志上限、持久卷和进程重启策略均已配置。
worker 仅获测试网密钥，reviewer 仅获 Codex 密钥；reviewer 不挂载交易状态卷。
交易所签名请求的最终 URL 有测试网域名和路由白名单，代码不存在 Agent live 模式。

新增独立 `btc-agent-image` CI job：验证 Compose，构建 runtime/reviewer，运行离线 paper
镜像恢复脚本。脚本使用新建临时卷、`--network none`、非特权只读容器，不接触已有部署或账户；
模拟进程退出 72 后，新容器复用该卷检查备份、对账和止损，退出后清理自己的临时卷。
它不能验证 reviewer 鉴权、公开行情或交易所执行，也不能替代长时间运行验收。

```bash
PYTHONPATH=src pytest -q tests/test_btc_agent_*.py
ruff check src/qt/btc_agent tests/test_btc_agent_*.py tests/fixtures/btc_agent_image_recovery.py
mypy --follow-imports=silent src/qt/btc_agent
docker compose --env-file config/btc-agent.env.example -f docker-compose.btc-agent.yml config --quiet
docker build -f Dockerfile.btc-agent --target runtime -t qt-btc-agent:ci .
docker build -f Dockerfile.btc-agent --target reviewer -t qt-btc-agent-reviewer:ci .
bash scripts/ci_btc_agent_image_smoke.sh qt-btc-agent:ci
```

## 未验证与所需条件

| 项目 | 本次阻碍 / 后续通过标准 |
| --- | --- |
| Docker 实际构建/运行 | 本机无 docker 可执行程序；需要 Docker Engine/Compose、镜像及依赖下载网络。新增 CI 的结果须单独查看，不预先宣称通过 |
| Codex 鉴权推理 | 环境没有 `CODEX_API_KEY`；需要 reviewer 成功返回符合 schema 的鉴权审批，HTTP healthz 不算 |
| Binance Spot Testnet 买卖成交 | 环境没有 `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`；公开 `/api/v3/time` 探测也未成功（HTTPError / URLError）。需可用网络、专用账户与虚拟余额，再执行操作指南中的 probe 和 round-trip |
| 连续 24 小时 | 本次未运行；必须记录真实起止时间 ≥24 小时、轮询及故障时段、状态/订单/余额、重启次数，检查期间是否有持续 blocked 或缺失 heartbeat |
| 实际部署故障恢复 | 在独立 paper Compose 项目上执行 kill/restart、停机备份与恢复副本验证；测试网还需交易所订单与 trade history 对账证据。本次只验证原生子进程和 SQLite 恢复 |
| 健康与故障通知 | 原有 heartbeat/health 可检测 blocked/halted/陈旧状态；没有外部故障告警闭环。Docker restart 不会因 unhealthy 自动重启，reviewer liveness 不代表鉴权成功 |
| 真实成本与样本外 | 未执行；需冻结规则和样本边界，使用独立数据、费用/滑点/IOC 部分成交假设。既有两笔交易回放不足以证明优势 |

凭据只能在部署机的受保护、被 Git 忽略的环境文件中配置。不要向聊天、日志或仓库提交。
测试网验收需保留 client ID、交易所 order ID、终态、正数买卖成交量、手续费与余额一致性证据；
必须无 pending 和活跃持仓，保留并说明 dust。网络失败、AI veto 或零成交不能标为通过。

软件止损依赖有效报价、网络和执行循环，不是交易所托管止损。`halt` 会同时阻止保护性卖出；
状态不确定时保留数据库并调查，不能删除账本或更换 client ID 绕过阻塞。
恢复备份前必须停止原 worker，防止两个状态副本同时交易；测试网备份可能落后于交易所，
必须先对账，不能仅凭本地 integrity_check 宣称账户已恢复。
